#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

#include "Misc/PackageName.h"
#include "UObject/SavePackage.h"

using namespace WitcherImportInternal;

namespace
{
constexpr const TCHAR* SchemaName = TEXT("witcher_unreal_export.v2");

void SetStringArray(TSharedPtr<FJsonObject> Object, const FString& Field, const TArray<FString>& Values)
{
    TArray<TSharedPtr<FJsonValue>> JsonValues;
    for (const FString& Value : Values)
    {
        JsonValues.Add(MakeShared<FJsonValueString>(Value));
    }
    Object->SetArrayField(Field, JsonValues);
}

FString SerializeJson(const TSharedPtr<FJsonObject>& Object)
{
    FString Output;
    TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Output);
    FJsonSerializer::Serialize(Object.ToSharedRef(), Writer);
    return Output;
}

FString DefaultContentRootForSourceGame(const FString& SourceGame)
{
    const FString Lowered = SourceGame.ToLower();
    return (Lowered == TEXT("w2") || Lowered == TEXT("witcher2") || Lowered == TEXT("tw2"))
        ? TEXT("/Game/Witcher2")
        : TEXT("/Game/Witcher3");
}

// UE 5.7 Interchange FBX skips legacy UFbxImportUI/root-null behavior; keep
// meshes and animations on the legacy importer so their skeleton roots agree.
struct FScopedLegacyFbxImport
{
    FScopedLegacyFbxImport()
    {
        CVar = IConsoleManager::Get().FindConsoleVariable(TEXT("Interchange.FeatureFlags.Import.FBX"));
        bWasEnabled = CVar && CVar->GetBool();
        if (bWasEnabled)
        {
            CVar->Set(false, ECVF_SetByCode);
        }
    }

    ~FScopedLegacyFbxImport()
    {
        if (bWasEnabled && CVar)
        {
            CVar->Set(true, ECVF_SetByCode);
        }
    }

    IConsoleVariable* CVar = nullptr;
    bool bWasEnabled = false;
};

bool SavePackageDirect(UPackage* Package, const TCHAR* LogCategoryName)
{
    if (!Package || !Package->IsDirty())
    {
        return true;
    }

    const FString PackageName = Package->GetName();
    FString PackageFilename;
    if (!FPackageName::TryConvertLongPackageNameToFilename(
            PackageName,
            PackageFilename,
            FPackageName::GetAssetPackageExtension()))
    {
        UE_LOG(LogWitcherImportContext, Warning, TEXT("%s: could not resolve package filename for '%s'"),
            LogCategoryName, *PackageName);
        return false;
    }

    UE_LOG(LogWitcherImportContext, Display, TEXT("%s: saving %s"), LogCategoryName, *PackageName);
    FSavePackageArgs SaveArgs;
    SaveArgs.TopLevelFlags = RF_Public | RF_Standalone;
    SaveArgs.SaveFlags = SAVE_NoError;
    SaveArgs.bSlowTask = false;
    const bool bSaved = UPackage::SavePackage(Package, nullptr, *PackageFilename, SaveArgs);
    if (!bSaved)
    {
        UE_LOG(LogWitcherImportContext, Warning, TEXT("%s: failed to save %s"), LogCategoryName, *PackageName);
    }
    return bSaved;
}
}

FString FWitcherImportContext::HandleRequest(const FString& RequestJson)
{
    TSharedPtr<FJsonObject> Request;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(RequestJson);
    if (!FJsonSerializer::Deserialize(Reader, Request) || !Request.IsValid())
    {
        return ErrorResponse(TEXT("Invalid JSON request"));
    }

    if (JsonString(Request, TEXT("command")) != TEXT("import_bundle"))
    {
        return ErrorResponse(TEXT("Unsupported command"));
    }
    if (JsonString(Request, TEXT("schema")) != SchemaName)
    {
        return ErrorResponse(FString::Printf(TEXT("Unsupported schema (plugin expects %s)"), SchemaName));
    }

    const FString ManifestPath = JsonString(Request, TEXT("manifest_path"));
    FString ManifestText;
    if (!FFileHelper::LoadFileToString(ManifestText, *ManifestPath))
    {
        return ErrorResponse(FString::Printf(TEXT("Could not read manifest: %s"), *ManifestPath));
    }

    TSharedPtr<FJsonObject> ManifestObject;
    TSharedRef<TJsonReader<>> ManifestReader = TJsonReaderFactory<>::Create(ManifestText);
    if (!FJsonSerializer::Deserialize(ManifestReader, ManifestObject) || !ManifestObject.IsValid())
    {
        return ErrorResponse(TEXT("Invalid manifest JSON"));
    }

    FWitcherImportContext Context(ManifestObject);
    return Context.ImportBundle();
}

FString FWitcherImportContext::ErrorResponse(const FString& ErrorMessage)
{
    TSharedPtr<FJsonObject> Response = MakeShared<FJsonObject>();
    Response->SetBoolField(TEXT("success"), false);
    SetStringArray(Response, TEXT("imported_assets"), {});
    SetStringArray(Response, TEXT("warnings"), {});
    SetStringArray(Response, TEXT("errors"), {ErrorMessage});
    return SerializeJson(Response);
}

FWitcherImportContext::FWitcherImportContext(const TSharedPtr<FJsonObject>& InManifest)
    : Manifest(InManifest)
{
    BundleRoot = JsonString(Manifest, TEXT("bundle_root"));
    ContentRoot = JsonString(
        Manifest,
        TEXT("content_root"),
        DefaultContentRootForSourceGame(JsonString(Manifest, TEXT("source_game"), TEXT("w3"))));
    while (ContentRoot.EndsWith(TEXT("/")))
    {
        ContentRoot.LeftChopInline(1);
    }
    AssetName = JsonString(Manifest, TEXT("asset_name"), TEXT("WitcherAsset"));

    const TSharedPtr<FJsonObject>* OverwritePtr = nullptr;
    if (Manifest.IsValid() && Manifest->TryGetObjectField(TEXT("overwrite"), OverwritePtr) && OverwritePtr)
    {
        OverwriteObject = *OverwritePtr;
    }
    BuildManifestAssetPathReservations();
}

bool FWitcherImportContext::ShouldOverwrite(const FString& Category) const
{
    return JsonBool(OverwriteObject, Category, false);
}

FString FWitcherImportContext::ImportBundle()
{
    if (JsonString(Manifest, TEXT("schema")) != SchemaName)
    {
        AddError(FString::Printf(TEXT("Manifest schema is not supported (plugin expects %s)"), SchemaName));
        return BuildResponse(false);
    }

    FScopedLegacyFbxImport LegacyFbxScope;

    FSlateNotificationManager::Get().SetAllowNotifications(false);
    ON_SCOPE_EXIT { FSlateNotificationManager::Get().SetAllowNotifications(true); };

    FScopedSlowTask SlowTask(12.0f, FText::FromString(TEXT("Importing Witcher bundle")));
    SlowTask.MakeDialog(false);

    const double BundleStart = FPlatformTime::Seconds();
    auto TimePhase = [&](const TCHAR* Name, float Weight, const TCHAR* Label, TFunctionRef<void()> Phase)
    {
        SlowTask.EnterProgressFrame(Weight, FText::FromString(Label));
        const double PhaseStart = FPlatformTime::Seconds();
        Phase();
        const double Seconds = FPlatformTime::Seconds() - PhaseStart;
        PhaseTimings.Emplace(FString(Name), Seconds);
        UE_LOG(LogWitcherImportContext, Display, TEXT("Witcher import phase '%s': %.3fs"), Name, Seconds);
    };

    TimePhase(TEXT("textures"), 1.0f, TEXT("Importing textures"), [&] { ImportTextures(); });
    TimePhase(TEXT("masters"), 1.0f, TEXT("Importing master materials"), [&] { ImportMasters(); });
    TimePhase(TEXT("materials"), 1.0f, TEXT("Importing material instances"), [&] { ImportMaterials(); });
    TimePhase(TEXT("rig"), 1.0f, TEXT("Importing rig"), [&] { ImportRig(); });
    TimePhase(TEXT("meshes"), 2.0f, TEXT("Importing meshes"), [&] { ImportMeshes(); });
    TimePhase(TEXT("speedtrees"), 1.0f, TEXT("Importing SpeedTree assets"), [&] { ImportSpeedTrees(); });
    TimePhase(TEXT("animations"), 1.0f, TEXT("Importing animations"), [&] { ImportAnimations(); });
    TimePhase(TEXT("blueprint"), 1.0f, TEXT("Building blueprint"), [&] { ImportBlueprint(); });
    TimePhase(TEXT("retarget_setup"), 0.5f, TEXT("Repairing retarget setup"), [&] { ImportRetargetSetup(); });
    TimePhase(TEXT("terrain"), 0.5f, TEXT("Importing terrain"), [&] { ImportTerrain(); });
    TimePhase(TEXT("placements"), 0.5f, TEXT("Placing layer actors"), [&] { ImportPlacements(); });
    TimePhase(TEXT("foliage"), 0.5f, TEXT("Placing foliage"), [&] { ImportFoliage(); });
    TimePhase(TEXT("save"), 1.0f, TEXT("Saving assets"), [&] { SaveImportedPackages(); });

    TotalImportSeconds = FPlatformTime::Seconds() - BundleStart;
    UE_LOG(LogWitcherImportContext, Display, TEXT("Witcher import total: %.3fs (%d assets)"),
        TotalImportSeconds, ImportedAssets.Num());

    return BuildResponse(Errors.Num() == 0);
}

FString FWitcherImportContext::ResolveBundleFile(const FString& RelativePath) const
{
    return FPaths::ConvertRelativePathToFull(FPaths::Combine(BundleRoot, RelativePath));
}

FString FWitcherImportContext::PackagePathFor(const FString& AssetRel) const
{
    FString Folder = AssetRel;
    int32 SlashIndex = INDEX_NONE;
    if (AssetRel.FindLastChar(TEXT('/'), SlashIndex))
    {
        Folder = AssetRel.Left(SlashIndex);
    }
    else
    {
        Folder.Empty();
    }
    return Folder.IsEmpty() ? ContentRoot : FString::Printf(TEXT("%s/%s"), *ContentRoot, *Folder);
}

FString FWitcherImportContext::ObjectPathFor(const FString& AssetRel) const
{
    return FString::Printf(TEXT("%s/%s.%s"), *ContentRoot, *AssetRel, *AssetRelName(AssetRel));
}

UObject* FWitcherImportContext::LoadAnyAsset(const FString& AssetRel) const
{
    return StaticLoadObject(UObject::StaticClass(), nullptr, *ObjectPathFor(AssetRel), nullptr, LOAD_NoWarn | LOAD_Quiet);
}

void FWitcherImportContext::BuildManifestAssetPathReservations()
{
    TSet<FString> TexturePaths;
    TSet<FString> MeshPaths;

    const TArray<TSharedPtr<FJsonValue>>* Textures = nullptr;
    if (Manifest.IsValid() && Manifest->TryGetArrayField(TEXT("textures"), Textures))
    {
        for (const TSharedPtr<FJsonValue>& Value : *Textures)
        {
            const TSharedPtr<FJsonObject> TextureObject = Value->AsObject();
            FString DepotRel = JsonString(TextureObject, TEXT("depot_path"));
            DepotRel = DepotRel.Replace(TEXT("\\"), TEXT("/"));
            if (!DepotRel.IsEmpty())
            {
                TexturePaths.Add(DepotRel);
            }
        }
    }

    const TArray<TSharedPtr<FJsonValue>>* Meshes = nullptr;
    if (Manifest.IsValid() && Manifest->TryGetArrayField(TEXT("meshes"), Meshes))
    {
        for (const TSharedPtr<FJsonValue>& Value : *Meshes)
        {
            const TSharedPtr<FJsonObject> MeshObject = Value->AsObject();
            FString AssetRel = JsonString(MeshObject, TEXT("asset_path"));
            AssetRel = AssetRel.Replace(TEXT("\\"), TEXT("/"));
            if (!AssetRel.IsEmpty())
            {
                MeshPaths.Add(AssetRel);
            }
        }
    }

    for (const FString& DepotRel : TexturePaths)
    {
        FString SafeRel = DepotRel;
        if (MeshPaths.Contains(DepotRel))
        {
            SafeRel += TEXT("_tex");
            AddWarning(FString::Printf(
                TEXT("Manifest path collision: '%s' is both a texture and mesh; importing texture as '%s'"),
                *DepotRel,
                *SafeRel));
        }
        TextureAssetRelByDepot.Add(DepotRel, SafeRel);
    }

    for (const FString& MeshRel : MeshPaths)
    {
        MeshAssetRelBySource.Add(MeshRel, MeshRel);
    }
}

FString FWitcherImportContext::ClassSafeAssetRel(const FString& AssetRel, UClass* DesiredClass, const FString& Suffix)
{
    // Same-stem assets of different classes collide after extension stripping.
    UObject* Existing = LoadAnyAsset(AssetRel);
    if (!Existing || Existing->IsA(DesiredClass))
    {
        return AssetRel;
    }
    const FString Adjusted = AssetRel + Suffix;
    AddWarning(FString::Printf(TEXT("'%s' already holds a %s; importing as '%s'"),
        *AssetRel, *Existing->GetClass()->GetName(), *Adjusted));
    return Adjusted;
}

void FWitcherImportContext::AddWarning(const FString& Warning)
{
    Warnings.Add(Warning);
    UE_LOG(LogWitcherImportContext, Warning, TEXT("%s"), *Warning);
}

void FWitcherImportContext::AddError(const FString& Error)
{
    Errors.Add(Error);
    UE_LOG(LogWitcherImportContext, Error, TEXT("%s"), *Error);
}

void FWitcherImportContext::SaveImportedPackages()
{
    TSet<UPackage*> Packages;
    const FString ContentRootPrefix = ContentRoot + TEXT("/");

    auto ConsiderDirty = [&](UObject* Object)
    {
        if (!Object || Object->IsA<AActor>())
        {
            return;
        }
        UPackage* Package = Object->GetOutermost();
        if (Package && Package->IsDirty() && Package->GetName().StartsWith(ContentRootPrefix))
        {
            Packages.Add(Package);
        }
    };

    for (const FString& AssetPath : ImportedAssets)
    {
        if (AssetPath.StartsWith(ContentRootPrefix))
        {
            const FString PackageName = FPackageName::ObjectPathToPackageName(AssetPath);
            if (UPackage* Package = FindPackage(nullptr, *PackageName))
            {
                if (Package->IsDirty())
                {
                    Packages.Add(Package);
                }
                continue;
            }
            ConsiderDirty(FindObject<UObject>(nullptr, *AssetPath));
        }
    }

    for (const TPair<FString, TWeakObjectPtr<UTexture>>& Pair : TexturesByDepot)
    {
        ConsiderDirty(Pair.Value.Get());
    }
    for (const TPair<FString, TWeakObjectPtr<UMaterialInterface>>& Pair : MastersByRel)
    {
        ConsiderDirty(Pair.Value.Get());
    }
    for (const TPair<FString, TWeakObjectPtr<UMaterialInterface>>& Pair : MaterialsById)
    {
        ConsiderDirty(Pair.Value.Get());
    }
    for (const TPair<FString, TWeakObjectPtr<UObject>>& Pair : MeshesByAssetRel)
    {
        ConsiderDirty(Pair.Value.Get());
    }
    ConsiderDirty(SharedSkeleton.Get());

    if (Packages.IsEmpty())
    {
        return;
    }

    bool bAllSaved = true;
    UE_LOG(LogWitcherImportContext, Display, TEXT("Saving %d imported asset package(s)"), Packages.Num());
    for (UPackage* Package : Packages)
    {
        bAllSaved = SavePackageDirect(Package, TEXT("Imported asset save")) && bAllSaved;
    }
    if (!bAllSaved)
    {
        AddWarning(TEXT("One or more imported asset packages could not be saved."));
    }
}

FString FWitcherImportContext::BuildResponse(bool bSuccess) const
{
    TSharedPtr<FJsonObject> Response = MakeShared<FJsonObject>();
    Response->SetBoolField(TEXT("success"), bSuccess);
    SetStringArray(Response, TEXT("imported_assets"), ImportedAssets);
    SetStringArray(Response, TEXT("warnings"), Warnings);
    SetStringArray(Response, TEXT("errors"), Errors);

    Response->SetNumberField(TEXT("imported_asset_count"), ImportedAssets.Num());
    Response->SetNumberField(TEXT("total_seconds"), TotalImportSeconds);
    TSharedPtr<FJsonObject> Timings = MakeShared<FJsonObject>();
    for (const TPair<FString, double>& Phase : PhaseTimings)
    {
        Timings->SetNumberField(Phase.Key, Phase.Value);
    }
    Response->SetObjectField(TEXT("timings"), Timings);
    return SerializeJson(Response);
}
