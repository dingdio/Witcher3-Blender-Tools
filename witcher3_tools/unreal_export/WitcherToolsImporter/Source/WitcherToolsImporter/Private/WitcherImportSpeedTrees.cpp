#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

using namespace WitcherImportInternal;

namespace
{
bool EnsureSpeedTreeImporterModule()
{
    return FModuleManager::Get().LoadModule(FName(TEXT("SpeedTreeImporter"))) != nullptr;
}

void SetImportBool(UObject* Object, const TCHAR* PropertyName, bool bValue)
{
    if (!Object)
    {
        return;
    }
    if (FBoolProperty* Property = FindFProperty<FBoolProperty>(Object->GetClass(), FName(PropertyName)))
    {
        Property->SetPropertyValue_InContainer(Object, bValue);
    }
}

void SetImportByte(UObject* Object, const TCHAR* PropertyName, uint8 Value)
{
    if (!Object)
    {
        return;
    }
    if (FByteProperty* Property = FindFProperty<FByteProperty>(Object->GetClass(), FName(PropertyName)))
    {
        Property->SetPropertyValue_InContainer(Object, Value);
        return;
    }
    if (FEnumProperty* EnumProperty = FindFProperty<FEnumProperty>(Object->GetClass(), FName(PropertyName)))
    {
        EnumProperty->GetUnderlyingProperty()->SetIntPropertyValue(
            EnumProperty->ContainerPtrToValuePtr<void>(Object),
            static_cast<int64>(Value));
    }
}

void SetImportFloat(UObject* Object, const TCHAR* PropertyName, float Value)
{
    if (!Object)
    {
        return;
    }
    if (FFloatProperty* Property = FindFProperty<FFloatProperty>(Object->GetClass(), FName(PropertyName)))
    {
        Property->SetPropertyValue_InContainer(Object, Value);
        return;
    }
    if (FDoubleProperty* Property = FindFProperty<FDoubleProperty>(Object->GetClass(), FName(PropertyName)))
    {
        Property->SetPropertyValue_InContainer(Object, static_cast<double>(Value));
    }
}

float DesiredSpeedTreeTreeScale(const TSharedPtr<FJsonObject>& SpeedTreeEntry)
{
    TSharedPtr<FJsonObject> ImportOptionsObject;
    const TSharedPtr<FJsonObject>* ImportOptions = nullptr;
    if (SpeedTreeEntry.IsValid() && SpeedTreeEntry->TryGetObjectField(TEXT("import_options"), ImportOptions) && ImportOptions)
    {
        ImportOptionsObject = *ImportOptions;
    }
    return static_cast<float>(JsonNumber(ImportOptionsObject, TEXT("tree_scale"), 100.0));
}

bool ReadFloatProperty(UObject* Object, const TCHAR* PropertyName, float& OutValue)
{
    if (!Object)
    {
        return false;
    }
    if (FFloatProperty* Property = FindFProperty<FFloatProperty>(Object->GetClass(), FName(PropertyName)))
    {
        OutValue = Property->GetPropertyValue_InContainer(Object);
        return true;
    }
    if (FDoubleProperty* Property = FindFProperty<FDoubleProperty>(Object->GetClass(), FName(PropertyName)))
    {
        OutValue = static_cast<float>(Property->GetPropertyValue_InContainer(Object));
        return true;
    }
    return false;
}

bool ExistingSpeedTreeNeedsReimport(UStaticMesh* ExistingMesh, const TSharedPtr<FJsonObject>& SpeedTreeEntry)
{
    if (!ExistingMesh)
    {
        return true;
    }

    UAssetImportData* ImportData = ExistingMesh->GetAssetImportData();
    float ExistingTreeScale = 0.0f;
    if (!ImportData || !ReadFloatProperty(ImportData, TEXT("TreeScale"), ExistingTreeScale))
    {
        return true;
    }

    const float DesiredTreeScale = DesiredSpeedTreeTreeScale(SpeedTreeEntry);
    return !FMath::IsNearlyEqual(ExistingTreeScale, DesiredTreeScale, 0.01f);
}

UFactory* CreateSpeedTreeImportFactory()
{
    if (!EnsureSpeedTreeImporterModule())
    {
        return nullptr;
    }
    UClass* FactoryClass = LoadClass<UFactory>(nullptr, TEXT("/Script/SpeedTreeImporter.SpeedTreeImportFactory"));
    return FactoryClass ? NewObject<UFactory>(GetTransientPackage(), FactoryClass) : nullptr;
}

UObject* CreateSpeedTreeImportOptions(const TSharedPtr<FJsonObject>& SpeedTreeEntry)
{
    if (!EnsureSpeedTreeImporterModule())
    {
        return nullptr;
    }
    UClass* ImportDataClass = LoadClass<UObject>(nullptr, TEXT("/Script/SpeedTreeImporter.SpeedTreeImportData"));
    if (!ImportDataClass)
    {
        return nullptr;
    }

    UObject* Options = NewObject<UObject>(GetTransientPackage(), ImportDataClass);
    Options->LoadConfig();

    TSharedPtr<FJsonObject> ImportOptionsObject;
    const TSharedPtr<FJsonObject>* ImportOptions = nullptr;
    if (SpeedTreeEntry.IsValid() && SpeedTreeEntry->TryGetObjectField(TEXT("import_options"), ImportOptions) && ImportOptions)
    {
        ImportOptionsObject = *ImportOptions;
    }

    // SpeedTree importer enums are not exported, so set their stored byte values.
    SetImportByte(Options, TEXT("ImportGeometryType"), 0); // IGT_3D
    SetImportByte(Options, TEXT("LODType"), 0); // ILT_PaintedFoliage
    SetImportFloat(Options, TEXT("TreeScale"), DesiredSpeedTreeTreeScale(SpeedTreeEntry));
    SetImportBool(Options, TEXT("IncludeCollision"), JsonBool(ImportOptionsObject, TEXT("include_collision"), true));
    SetImportBool(Options, TEXT("MakeMaterialsCheck"), JsonBool(ImportOptionsObject, TEXT("create_materials"), true));
    SetImportBool(Options, TEXT("IncludeNormalMapCheck"), JsonBool(ImportOptionsObject, TEXT("include_normal_maps"), true));
    SetImportBool(Options, TEXT("IncludeDetailMapCheck"), JsonBool(ImportOptionsObject, TEXT("include_detail_maps"), true));
    SetImportBool(Options, TEXT("IncludeSpecularMapCheck"), JsonBool(ImportOptionsObject, TEXT("include_specular_maps"), true));
    SetImportBool(Options, TEXT("IncludeBranchSeamSmoothing"), JsonBool(ImportOptionsObject, TEXT("include_branch_seams"), true));
    SetImportBool(Options, TEXT("IncludeSpeedTreeAO"), JsonBool(ImportOptionsObject, TEXT("include_speedtree_ao"), true));
    SetImportBool(Options, TEXT("IncludeColorAdjustment"), JsonBool(ImportOptionsObject, TEXT("include_color_variation"), true));
    SetImportBool(Options, TEXT("IncludeSubsurface"), JsonBool(ImportOptionsObject, TEXT("include_subsurface"), true));
    SetImportBool(Options, TEXT("IncludeVertexProcessingCheck"), JsonBool(ImportOptionsObject, TEXT("include_vertex_processing"), true));
    SetImportBool(Options, TEXT("IncludeWindCheck"), JsonBool(ImportOptionsObject, TEXT("include_wind"), true));
    SetImportBool(Options, TEXT("IncludeSmoothLODCheck"), JsonBool(ImportOptionsObject, TEXT("include_smooth_lod"), true));
    return Options;
}

// SpeedTree-7 reads TreeScale from the import-data CDO/config instead of task options.
struct FScopedSpeedTreeTreeScale
{
    UObject* CDO = nullptr;
    float PrevTreeScale = 30.48f;

    explicit FScopedSpeedTreeTreeScale(float TreeScale)
    {
        UClass* ImportDataClass = EnsureSpeedTreeImporterModule()
            ? LoadClass<UObject>(nullptr, TEXT("/Script/SpeedTreeImporter.SpeedTreeImportData"))
            : nullptr;
        CDO = ImportDataClass ? ImportDataClass->GetDefaultObject() : nullptr;
        if (!CDO)
        {
            return;
        }
        ReadFloatProperty(CDO, TEXT("TreeScale"), PrevTreeScale);
        SetImportFloat(CDO, TEXT("TreeScale"), TreeScale);
        CDO->SaveConfig();
    }

    ~FScopedSpeedTreeTreeScale()
    {
        if (CDO)
        {
            SetImportFloat(CDO, TEXT("TreeScale"), PrevTreeScale);
            CDO->SaveConfig();
        }
    }
};

void ConfigureSpeedTreeLods(UStaticMesh* Mesh, const TSharedPtr<FJsonObject>& SpeedTreeEntry)
{
    if (!Mesh)
    {
        return;
    }

    TArray<double> ConfiguredScreenSizes;
    const TSharedPtr<FJsonObject>* ImportOptions = nullptr;
    const TArray<TSharedPtr<FJsonValue>>* ScreenSizeValues = nullptr;
    if (SpeedTreeEntry.IsValid()
        && SpeedTreeEntry->TryGetObjectField(TEXT("import_options"), ImportOptions)
        && ImportOptions
        && (*ImportOptions)->TryGetArrayField(TEXT("lod_screen_sizes"), ScreenSizeValues)
        && ScreenSizeValues)
    {
        for (const TSharedPtr<FJsonValue>& Value : *ScreenSizeValues)
        {
            const double ScreenSize = Value.IsValid() ? Value->AsNumber() : 0.0;
            if (ScreenSize > 0.0)
            {
                ConfiguredScreenSizes.Add(ScreenSize);
            }
        }
    }

    Mesh->SetAutoComputeLODScreenSize(false);
    Mesh->SetRequiresLODDistanceConversion(false);

    const int32 LODCount = Mesh->GetNumSourceModels();
    for (int32 LODIndex = 0; LODIndex < LODCount; ++LODIndex)
    {
        const double ScreenSize = ConfiguredScreenSizes.IsValidIndex(LODIndex)
            ? ConfiguredScreenSizes[LODIndex]
            : FMath::Max(0.0025, 0.04 / FMath::Pow(2.0, FMath::Max(0, LODIndex - 1)));
        Mesh->GetSourceModel(LODIndex).ScreenSize.Default = static_cast<float>(ScreenSize);
    }
    Mesh->MarkPackageDirty();
}

bool HasSimpleCollision(const UStaticMesh* Mesh)
{
    const UBodySetup* BodySetup = Mesh ? Mesh->GetBodySetup() : nullptr;
    return BodySetup && BodySetup->AggGeom.GetElementCount() > 0;
}

bool PackageIsAtOrUnder(const FString& PackageName, const FString& PackagePath)
{
    if (PackageName.IsEmpty() || PackagePath.IsEmpty())
    {
        return false;
    }
    const FString Prefix = PackagePath.EndsWith(TEXT("/")) ? PackagePath : PackagePath + TEXT("/");
    return PackageName == PackagePath || PackageName.StartsWith(Prefix);
}

TSet<FString> DirtyPackageNamesUnder(const FString& PackagePath)
{
    TSet<FString> Names;
    for (TObjectIterator<UPackage> It; It; ++It)
    {
        UPackage* Package = *It;
        if (!Package || !Package->IsDirty())
        {
            continue;
        }
        const FString PackageName = Package->GetName();
        if (PackageIsAtOrUnder(PackageName, PackagePath))
        {
            Names.Add(PackageName);
        }
    }
    return Names;
}

void AddNewDirtyPackagesUnder(const FString& PackagePath, const TSet<FString>& PreviousDirtyPackages, TArray<FString>& ImportedAssets)
{
    for (TObjectIterator<UPackage> It; It; ++It)
    {
        UPackage* Package = *It;
        if (!Package || !Package->IsDirty())
        {
            continue;
        }
        const FString PackageName = Package->GetName();
        if (!PackageIsAtOrUnder(PackageName, PackagePath))
        {
            continue;
        }
        if (!PreviousDirtyPackages.Contains(PackageName))
        {
            ImportedAssets.AddUnique(PackageName);
        }
    }
}

void EnsureSpeedTreeFallbackCollision(UStaticMesh* Mesh, const TSharedPtr<FJsonObject>& SpeedTreeEntry)
{
    if (!Mesh || HasSimpleCollision(Mesh))
    {
        return;
    }

    TSharedPtr<FJsonObject> ImportOptionsObject;
    const TSharedPtr<FJsonObject>* ImportOptions = nullptr;
    if (SpeedTreeEntry.IsValid() && SpeedTreeEntry->TryGetObjectField(TEXT("import_options"), ImportOptions) && ImportOptions)
    {
        ImportOptionsObject = *ImportOptions;
    }
    if (!JsonBool(ImportOptionsObject, TEXT("fallback_trunk_collision"), false))
    {
        return;
    }

    const FBox Bounds = Mesh->GetBoundingBox();
    if (!Bounds.IsValid)
    {
        return;
    }

    const FVector Extent = Bounds.GetExtent();
    const float Height = Extent.Z * 2.0f;
    const float RadiusSource = FMath::Min(Extent.X, Extent.Y);
    if (Height <= 100.0f || RadiusSource <= 1.0f)
    {
        return;
    }

    Mesh->CreateBodySetup();
    UBodySetup* BodySetup = Mesh->GetBodySetup();
    if (!BodySetup)
    {
        return;
    }

    const float Radius = FMath::Clamp(RadiusSource * 0.18f, 12.0f, 120.0f);
    const float CapsuleLength = FMath::Max(1.0f, Height - Radius * 2.0f);

    FKSphylElem SphylElem;
    SphylElem.Radius = Radius;
    SphylElem.Length = CapsuleLength;
    SphylElem.Center = FVector(Bounds.GetCenter().X, Bounds.GetCenter().Y, Bounds.Min.Z + Height * 0.5f);
    BodySetup->AggGeom.SphylElems.Add(SphylElem);
    BodySetup->CollisionTraceFlag = CTF_UseDefault;
    BodySetup->ClearPhysicsMeshes();
    BodySetup->InvalidatePhysicsData();
    BodySetup->CreatePhysicsMeshes();

    Mesh->MarkPackageDirty();
    UE_LOG(LogWitcherImportContext, Display, TEXT("Added fallback trunk collision to SpeedTree '%s'."), *Mesh->GetName());
}

FString NormalizedDepotFragment(const FString& Value)
{
    FString Normalized = Value.Replace(TEXT("\\"), TEXT("/")).ToLower();
    while (Normalized.Contains(TEXT("//")))
    {
        Normalized.ReplaceInline(TEXT("//"), TEXT("/"));
    }
    return Normalized;
}

bool IsGrassSpeedTreeEntry(const FString& AssetRel, const TSharedPtr<FJsonObject>& SpeedTreeEntry)
{
    const FString Combined =
        NormalizedDepotFragment(AssetRel)
        + TEXT("/")
        + NormalizedDepotFragment(JsonString(SpeedTreeEntry, TEXT("depot_path")))
        + TEXT("/")
        + NormalizedDepotFragment(JsonString(SpeedTreeEntry, TEXT("file")));
    return Combined.Contains(TEXT("environment/vegetation/grass/"));
}

bool IsSpeedTreeCardMaterialName(const FString& MaterialName)
{
    const FString Lower = MaterialName.ToLower();
    return Lower.Contains(TEXT("_fronds"))
        || Lower.Contains(TEXT("_leaves"))
        || Lower.Contains(TEXT("_facingleaves"));
}

ESpeedTreeGeometryType GuessSpeedTreeGeometryType(const FString& MaterialName)
{
    const FString Lower = MaterialName.ToLower();
    if (Lower.Contains(TEXT("_facingleaves")))
    {
        return STG_FacingLeaf;
    }
    if (Lower.Contains(TEXT("_leaves")))
    {
        return STG_Leaf;
    }
    return STG_Frond;
}

UMaterialExpressionSpeedTree* FindSpeedTreeExpression(UMaterial* Material)
{
    if (!Material)
    {
        return nullptr;
    }
    for (UMaterialExpression* Expression : Material->GetExpressions())
    {
        if (UMaterialExpressionSpeedTree* SpeedTreeExpression = Cast<UMaterialExpressionSpeedTree>(Expression))
        {
            return SpeedTreeExpression;
        }
    }
    return nullptr;
}

UMaterialExpressionTextureSample* FindLikelyDiffuseTextureSample(UMaterial* Material, UMaterialEditorOnlyData* EditorOnly)
{
    if (!Material)
    {
        return nullptr;
    }
    if (EditorOnly)
    {
        if (UMaterialExpressionTextureSample* BaseTexture =
            Cast<UMaterialExpressionTextureSample>(EditorOnly->BaseColor.Expression))
        {
            return BaseTexture;
        }
    }

    UMaterialExpressionTextureSample* FirstColorSample = nullptr;
    for (UMaterialExpression* Expression : Material->GetExpressions())
    {
        UMaterialExpressionTextureSample* TextureSample = Cast<UMaterialExpressionTextureSample>(Expression);
        if (!TextureSample || !TextureSample->Texture)
        {
            continue;
        }

        const FString TextureName = TextureSample->Texture->GetName().ToLower();
        const bool bLooksLikePackedNormalOrSpec =
            TextureName.Contains(TEXT("_n_"))
            || TextureName.Contains(TEXT("_n_dds"))
            || TextureName.EndsWith(TEXT("_n"))
            || TextureName.Contains(TEXT("_s_"))
            || TextureName.Contains(TEXT("_s_dds"))
            || TextureName.EndsWith(TEXT("_s"));
        if (!bLooksLikePackedNormalOrSpec)
        {
            return TextureSample;
        }
        if (!FirstColorSample && TextureSample->SamplerType == SAMPLERTYPE_Color)
        {
            FirstColorSample = TextureSample;
        }
    }
    return FirstColorSample;
}

bool EnsureMaterialOpacityMask(UMaterial* Material, UMaterialEditorOnlyData* EditorOnly)
{
    if (!Material || !EditorOnly)
    {
        return false;
    }
    if (EditorOnly->OpacityMask.Expression)
    {
        return true;
    }

    UMaterialExpressionTextureSample* DiffuseTexture = FindLikelyDiffuseTextureSample(Material, EditorOnly);
    if (!DiffuseTexture)
    {
        return false;
    }

    UMaterialExpressionComponentMask* AlphaMask = Cast<UMaterialExpressionComponentMask>(
        UMaterialEditingLibrary::CreateMaterialExpression(
            Material, UMaterialExpressionComponentMask::StaticClass(), -150, 120));
    if (!AlphaMask)
    {
        return false;
    }
    AlphaMask->R = false;
    AlphaMask->G = false;
    AlphaMask->B = false;
    AlphaMask->A = true;
    AlphaMask->Input.Connect(5, DiffuseTexture);
    EditorOnly->OpacityMask.Expression = AlphaMask;
    return true;
}

bool PatchGrassSpeedTreeMaterial(UMaterial* Material)
{
    if (!Material || !IsSpeedTreeCardMaterialName(Material->GetName()))
    {
        return false;
    }

    UMaterialEditorOnlyData* EditorOnly = Material->GetEditorOnlyData();
    if (!EditorOnly)
    {
        return false;
    }

    const bool bHadMask = EditorOnly->OpacityMask.Expression != nullptr;
    if (!bHadMask && !FindLikelyDiffuseTextureSample(Material, EditorOnly))
    {
        return false;
    }

    bool bChanged = false;
    Material->PreEditChange(nullptr);

    if (!bHadMask)
    {
        if (!EnsureMaterialOpacityMask(Material, EditorOnly))
        {
            Material->PostEditChange();
            return false;
        }
        bChanged = true;
    }

    if (Material->BlendMode != BLEND_Masked)
    {
        Material->BlendMode = BLEND_Masked;
        Material->OpacityMaskClipValue = 0.3333f;
        Material->SetCastShadowAsMasked(true);
        bChanged = true;
    }
    if (!Material->TwoSided)
    {
        Material->TwoSided = true;
        bChanged = true;
    }

    UMaterialExpressionSpeedTree* SpeedTreeExpression = FindSpeedTreeExpression(Material);
    if (!SpeedTreeExpression)
    {
        SpeedTreeExpression = Cast<UMaterialExpressionSpeedTree>(
            UMaterialEditingLibrary::CreateMaterialExpression(
                Material, UMaterialExpressionSpeedTree::StaticClass(), 150, 520));
        bChanged = SpeedTreeExpression != nullptr || bChanged;
    }
    if (SpeedTreeExpression)
    {
        const ESpeedTreeGeometryType GeometryType = GuessSpeedTreeGeometryType(Material->GetName());
        if (SpeedTreeExpression->GeometryType != GeometryType)
        {
            SpeedTreeExpression->GeometryType = GeometryType;
            bChanged = true;
        }
        if (SpeedTreeExpression->WindType == STW_None)
        {
            SpeedTreeExpression->WindType = STW_Best;
            bChanged = true;
        }
        if (SpeedTreeExpression->LODType != STLOD_Smooth)
        {
            SpeedTreeExpression->LODType = STLOD_Smooth;
            bChanged = true;
        }
        if (!EditorOnly->WorldPositionOffset.Expression)
        {
            EditorOnly->WorldPositionOffset.Expression = SpeedTreeExpression;
            bChanged = true;
        }
    }

    if (!bChanged)
    {
        Material->PostEditChange();
        return false;
    }

    Material->PostEditChange();
    Material->MarkPackageDirty();
    UMaterialEditingLibrary::RecompileMaterial(Material);
    return true;
}

int32 PatchGrassSpeedTreeMaterials(
    UStaticMesh* Mesh,
    const FString& AssetRel,
    const TSharedPtr<FJsonObject>& SpeedTreeEntry,
    TArray<FString>& ImportedAssets)
{
    if (!Mesh || !IsGrassSpeedTreeEntry(AssetRel, SpeedTreeEntry))
    {
        return 0;
    }

    int32 PatchedCount = 0;
    for (FStaticMaterial& StaticMaterial : Mesh->GetStaticMaterials())
    {
        UMaterial* Material = Cast<UMaterial>(StaticMaterial.MaterialInterface.Get());
        if (!Material)
        {
            continue;
        }
        if (PatchGrassSpeedTreeMaterial(Material))
        {
            ++PatchedCount;
            ImportedAssets.AddUnique(Material->GetPathName());
        }
    }

    if (PatchedCount > 0)
    {
        UE_LOG(LogWitcherImportContext, Display,
            TEXT("Patched %d grass SpeedTree material(s) for '%s' with masked opacity and SpeedTree wind."),
            PatchedCount, *AssetRel);
    }
    return PatchedCount;
}
}

void FWitcherImportContext::ImportSpeedTrees()
{
    const TArray<TSharedPtr<FJsonValue>>* SpeedTrees = nullptr;
    if (!Manifest->TryGetArrayField(TEXT("speedtrees"), SpeedTrees))
    {
        return;
    }
    for (const TSharedPtr<FJsonValue>& Value : *SpeedTrees)
    {
        const TSharedPtr<FJsonObject> SpeedTreeEntry = Value->AsObject();
        if (!SpeedTreeEntry.IsValid())
        {
            continue;
        }

        const FString AssetRel = JsonString(SpeedTreeEntry, TEXT("asset_path"));
        if (AssetRel.IsEmpty())
        {
            AddWarning(TEXT("SpeedTree entry has no asset_path."));
            continue;
        }

        const bool bForceImport = JsonBool(SpeedTreeEntry, TEXT("force_import"), true);
        if (!bForceImport && !ShouldOverwrite(TEXT("meshes")))
        {
            if (UObject* Existing = LoadAnyAsset(AssetRel))
            {
                if (UStaticMesh* ExistingMesh = Cast<UStaticMesh>(Existing))
                {
                    if (!ExistingSpeedTreeNeedsReimport(ExistingMesh, SpeedTreeEntry))
                    {
                        PatchGrassSpeedTreeMaterials(ExistingMesh, AssetRel, SpeedTreeEntry, ImportedAssets);
                        continue;
                    }
                    UE_LOG(LogWitcherImportContext, Display,
                        TEXT("Reimporting SpeedTree '%s' because its import scale is stale."), *AssetRel);
                }
                else
                {
                    continue;
                }
            }
        }

        const FString SourceFile = ResolveBundleFile(JsonString(SpeedTreeEntry, TEXT("file")));
        if (!FPaths::FileExists(SourceFile))
        {
            AddError(FString::Printf(TEXT("SpeedTree .srt file does not exist: %s"), *SourceFile));
            continue;
        }

        UFactory* SpeedTreeFactory = CreateSpeedTreeImportFactory();
        UObject* SpeedTreeOptions = CreateSpeedTreeImportOptions(SpeedTreeEntry);
        if (!SpeedTreeFactory || !SpeedTreeOptions)
        {
            AddError(TEXT("Failed to create Unreal SpeedTree import options. Enable Unreal's SpeedTree importer plugin."));
            continue;
        }

        FScopedSpeedTreeTreeScale ScopedTreeScale(DesiredSpeedTreeTreeScale(SpeedTreeEntry));

        const FString DestinationPath = PackagePathFor(AssetRel);
        const FString DestinationName = AssetRelName(AssetRel);
        UAssetImportTask* Task = NewObject<UAssetImportTask>();
        Task->AddToRoot();
        ON_SCOPE_EXIT { Task->RemoveFromRoot(); };
        Task->Filename = SourceFile;
        Task->DestinationPath = DestinationPath;
        Task->DestinationName = DestinationName;
        Task->Factory = SpeedTreeFactory;
        Task->Options = SpeedTreeOptions;
        Task->bAutomated = true;
        Task->bSave = false;
        Task->bReplaceExisting = true;

        FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
        TArray<UAssetImportTask*> Tasks;
        Tasks.Add(Task);
        const TSet<FString> DirtyPackagesBeforeImport = DirtyPackageNamesUnder(DestinationPath);
        AssetToolsModule.Get().ImportAssetTasks(Tasks);
        const TArray<FString> ImportedObjectPaths = Task->ImportedObjectPaths;

        UObject* MainAsset = nullptr;
        for (const FString& Path : ImportedObjectPaths)
        {
            UObject* Imported = StaticLoadObject(UObject::StaticClass(), nullptr, *Path);
            if (!Imported)
            {
                continue;
            }
            ImportedAssets.AddUnique(Path);
            if (Imported->GetPathName() == ObjectPathFor(AssetRel))
            {
                MainAsset = Imported;
            }
            else if (!MainAsset && Imported->IsA<UStaticMesh>())
            {
                MainAsset = Imported;
            }
            else if (!MainAsset)
            {
                MainAsset = Imported;
            }
        }

        if (!MainAsset)
        {
            MainAsset = LoadAnyAsset(AssetRel);
            if (MainAsset)
            {
                ImportedAssets.AddUnique(MainAsset->GetPathName());
            }
        }

        if (!MainAsset)
        {
            AddError(FString::Printf(
                TEXT("Failed to import SpeedTree .srt '%s'. Enable Unreal's SpeedTree importer plugin and verify the staged textures are beside the .srt."),
                *SourceFile));
            continue;
        }

        if (UStaticMesh* StaticMesh = Cast<UStaticMesh>(MainAsset))
        {
            ConfigureSpeedTreeLods(StaticMesh, SpeedTreeEntry);
            EnsureSpeedTreeFallbackCollision(StaticMesh, SpeedTreeEntry);
            PatchGrassSpeedTreeMaterials(StaticMesh, AssetRel, SpeedTreeEntry, ImportedAssets);
        }
        AddNewDirtyPackagesUnder(DestinationPath, DirtyPackagesBeforeImport, ImportedAssets);

        if (MainAsset->GetPathName() != ObjectPathFor(AssetRel))
        {
            AddWarning(FString::Printf(
                TEXT("SpeedTree '%s' imported as '%s' instead of the mirrored depot path."),
                *AssetRel, *MainAsset->GetPathName()));
        }
    }
}
