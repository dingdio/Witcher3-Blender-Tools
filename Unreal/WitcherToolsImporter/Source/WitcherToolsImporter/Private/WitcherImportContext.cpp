#include "WitcherImportContext.h"

#include "Animation/AnimSequence.h"
#include "Animation/Skeleton.h"
#include "AssetImportTask.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetToolsModule.h"
#include "Components/HierarchicalInstancedStaticMeshComponent.h"
#include "Components/LightComponent.h"
#include "Components/PointLightComponent.h"
#include "Components/PrimitiveComponent.h"
#include "Components/SceneComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "Components/SpotLightComponent.h"
#include "Components/StaticMeshComponent.h"
#include "EngineUtils.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "Engine/SCS_Node.h"
#include "Engine/SimpleConstructionScript.h"
#include "Engine/SkeletalMesh.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/Texture.h"
#include "Engine/Texture2D.h"
#include "Engine/Texture2DArray.h"
#include "Engine/PointLight.h"
#include "Engine/SpotLight.h"
#include "Editor.h"
#include "Landscape.h"
#include "LandscapeInfo.h"
#include "LandscapeProxy.h"
#include "LandscapeImportHelper.h"
#include "Materials/MaterialExpressionAppendVector.h"
#include "Materials/MaterialExpressionComponentMask.h"
#include "Materials/MaterialExpressionCustom.h"
#include "Materials/MaterialExpressionConstant.h"
#include "Materials/MaterialExpressionConstant2Vector.h"
#include "Materials/MaterialExpressionConstant3Vector.h"
#include "Materials/MaterialExpressionConstant4Vector.h"
#include "Materials/MaterialExpressionDivide.h"
#include "Materials/MaterialExpressionLinearInterpolate.h"
#include "Materials/MaterialExpressionMax.h"
#include "Materials/MaterialExpressionMultiply.h"
#include "Materials/MaterialExpressionSaturate.h"
#include "Materials/MaterialExpressionSquareRoot.h"
#include "Materials/MaterialExpressionSubtract.h"
#include "Materials/MaterialExpressionTextureObject.h"
#include "Materials/MaterialExpressionTextureSample.h"
#include "Materials/MaterialExpressionVertexNormalWS.h"
#include "Materials/MaterialExpressionWorldPosition.h"
#include "Factories/FbxAnimSequenceImportData.h"
#include "Factories/FbxImportUI.h"
#include "Factories/FbxSkeletalMeshImportData.h"
#include "Factories/FbxStaticMeshImportData.h"
#include "Factories/MaterialFactoryNew.h"
#include "Factories/TextureFactory.h"
#include "FileHelpers.h"
#include "GameFramework/Actor.h"
#include "HAL/IConsoleManager.h"
#include "IAssetTools.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "MaterialEditingLibrary.h"
#include "Materials/Material.h"
#include "Materials/MaterialExpressionScalarParameter.h"
#include "Materials/MaterialExpressionTextureBase.h"
#include "Materials/MaterialExpressionTextureSampleParameter2D.h"
#include "Materials/MaterialExpressionVectorParameter.h"
#include "Materials/MaterialInstanceConstant.h"
#include "Materials/MaterialInterface.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "PhysicsEngine/BodySetup.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "WitcherImportedActor.h"

DEFINE_LOG_CATEGORY_STATIC(LogWitcherImportContext, Log, All);

namespace
{
constexpr const TCHAR* SchemaName = TEXT("witcher_unreal_export.v2");

FString JsonString(const TSharedPtr<FJsonObject>& Object, const FString& Field, const FString& DefaultValue = TEXT(""))
{
    if (!Object.IsValid())
    {
        return DefaultValue;
    }
    FString Value;
    return Object->TryGetStringField(Field, Value) ? Value : DefaultValue;
}

int32 JsonInt(const TSharedPtr<FJsonObject>& Object, const FString& Field, int32 DefaultValue = 0)
{
    if (!Object.IsValid())
    {
        return DefaultValue;
    }
    return Object->HasTypedField<EJson::Number>(Field) ? Object->GetIntegerField(Field) : DefaultValue;
}

bool JsonBool(const TSharedPtr<FJsonObject>& Object, const FString& Field, bool DefaultValue = false)
{
    if (!Object.IsValid())
    {
        return DefaultValue;
    }
    return Object->HasTypedField<EJson::Boolean>(Field) ? Object->GetBoolField(Field) : DefaultValue;
}

double JsonNumber(const TSharedPtr<FJsonObject>& Object, const FString& Field, double DefaultValue = 0.0)
{
    if (!Object.IsValid())
    {
        return DefaultValue;
    }
    return Object->HasTypedField<EJson::Number>(Field) ? Object->GetNumberField(Field) : DefaultValue;
}

FString HlslFloatLiteral(float Value)
{
    FString Literal = FString::SanitizeFloat(Value);
    if (!Literal.Contains(TEXT(".")) && !Literal.Contains(TEXT("e")) && !Literal.Contains(TEXT("E")))
    {
        Literal += TEXT(".0");
    }
    return Literal;
}

FString HlslFloatArray(const TArray<float>& Values)
{
    FString Result;
    for (int32 Index = 0; Index < Values.Num(); ++Index)
    {
        if (Index > 0)
        {
            Result += TEXT(",");
        }
        Result += HlslFloatLiteral(Values[Index]);
    }
    return Result;
}

FLinearColor JsonColor(const TSharedPtr<FJsonObject>& Object, const FString& Field)
{
    FLinearColor Color(1.0f, 1.0f, 1.0f, 1.0f);
    const TArray<TSharedPtr<FJsonValue>>* Values = nullptr;
    if (Object.IsValid() && Object->TryGetArrayField(Field, Values) && Values->Num() >= 3)
    {
        Color.R = static_cast<float>((*Values)[0]->AsNumber());
        Color.G = static_cast<float>((*Values)[1]->AsNumber());
        Color.B = static_cast<float>((*Values)[2]->AsNumber());
        Color.A = Values->Num() > 3 ? static_cast<float>((*Values)[3]->AsNumber()) : 1.0f;
    }
    return Color;
}

FVector JsonVector(const TSharedPtr<FJsonObject>& Object, const FString& Field, const FVector& DefaultValue)
{
    const TArray<TSharedPtr<FJsonValue>>* Values = nullptr;
    if (Object.IsValid() && Object->TryGetArrayField(Field, Values) && Values->Num() >= 3)
    {
        return FVector(
            (*Values)[0]->AsNumber(),
            (*Values)[1]->AsNumber(),
            (*Values)[2]->AsNumber());
    }
    return DefaultValue;
}

FQuat JsonQuat(const TSharedPtr<FJsonObject>& Object, const FString& Field)
{
    const TArray<TSharedPtr<FJsonValue>>* Values = nullptr;
    if (Object.IsValid() && Object->TryGetArrayField(Field, Values) && Values->Num() >= 4)
    {
        FQuat Quat(
            (*Values)[0]->AsNumber(),
            (*Values)[1]->AsNumber(),
            (*Values)[2]->AsNumber(),
            (*Values)[3]->AsNumber());
        Quat.Normalize();
        return Quat;
    }
    return FQuat::Identity;
}

/** Minimal UMaterial: a texture or flat colour into BaseColor, with a fixed
 *  roughness; optionally translucent (for the world water plane). */
UMaterialInterface* CreateSimpleMaterial(
    const FString& PackagePath,
    const FString& Name,
    UTexture* BaseColorTexture,
    const FLinearColor& FallbackColor,
    bool bTranslucent)
{
    FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
    UMaterialFactoryNew* Factory = NewObject<UMaterialFactoryNew>();
    UMaterial* Material = Cast<UMaterial>(
        AssetToolsModule.Get().CreateAsset(Name, PackagePath, UMaterial::StaticClass(), Factory));
    if (!Material)
    {
        return nullptr;
    }

    Material->PreEditChange(nullptr);
    if (bTranslucent)
    {
        Material->BlendMode = BLEND_Translucent;
    }

    UMaterialExpression* BaseColorSource = nullptr;
    if (BaseColorTexture)
    {
        UMaterialExpressionTextureSample* TextureSample = Cast<UMaterialExpressionTextureSample>(
            UMaterialEditingLibrary::CreateMaterialExpression(
                Material, UMaterialExpressionTextureSample::StaticClass(), -400, 0));
        if (TextureSample)
        {
            TextureSample->Texture = BaseColorTexture;
            TextureSample->SamplerType = SAMPLERTYPE_Color;
            BaseColorSource = TextureSample;
        }
    }
    if (!BaseColorSource)
    {
        UMaterialExpressionConstant3Vector* ColorExpr = Cast<UMaterialExpressionConstant3Vector>(
            UMaterialEditingLibrary::CreateMaterialExpression(
                Material, UMaterialExpressionConstant3Vector::StaticClass(), -400, 0));
        if (ColorExpr)
        {
            ColorExpr->Constant = FallbackColor;
            BaseColorSource = ColorExpr;
        }
    }
    if (BaseColorSource)
    {
        UMaterialEditingLibrary::ConnectMaterialProperty(BaseColorSource, TEXT(""), MP_BaseColor);
    }

    if (UMaterialExpressionConstant* Roughness = Cast<UMaterialExpressionConstant>(
            UMaterialEditingLibrary::CreateMaterialExpression(
                Material, UMaterialExpressionConstant::StaticClass(), -400, 250)))
    {
        Roughness->R = bTranslucent ? 0.06f : 0.92f;
        UMaterialEditingLibrary::ConnectMaterialProperty(Roughness, TEXT(""), MP_Roughness);
    }

    if (bTranslucent)
    {
        if (UMaterialExpressionConstant* Opacity = Cast<UMaterialExpressionConstant>(
                UMaterialEditingLibrary::CreateMaterialExpression(
                    Material, UMaterialExpressionConstant::StaticClass(), -400, 400)))
        {
            Opacity->R = 0.55f;
            UMaterialEditingLibrary::ConnectMaterialProperty(Opacity, TEXT(""), MP_Opacity);
        }
    }

    Material->PostEditChange();
    Material->MarkPackageDirty();
    UMaterialEditingLibrary::RecompileMaterial(Material);
    return Material;
}

bool IsVolumeMaterialRel(const FString& AssetRel)
{
    FString Normalized = AssetRel.Replace(TEXT("\\"), TEXT("/")).ToLower();
    if (Normalized.EndsWith(TEXT(".w2mg")))
    {
        Normalized.LeftChopInline(5);
    }
    return Normalized == TEXT("engine/materials/defaults/volume");
}

void ApplyInvisibleVolumeMaterial(UMaterial* Material)
{
    if (!Material)
    {
        return;
    }

    Material->PreEditChange(nullptr);
    UMaterialEditingLibrary::DeleteAllMaterialExpressions(Material);
    Material->BlendMode = BLEND_Translucent;

    if (UMaterialExpressionConstant3Vector* BaseColor = Cast<UMaterialExpressionConstant3Vector>(
            UMaterialEditingLibrary::CreateMaterialExpression(
                Material, UMaterialExpressionConstant3Vector::StaticClass(), -400, 0)))
    {
        BaseColor->Constant = FLinearColor::Black;
        UMaterialEditingLibrary::ConnectMaterialProperty(BaseColor, TEXT(""), MP_BaseColor);
    }

    if (UMaterialExpressionConstant* Opacity = Cast<UMaterialExpressionConstant>(
            UMaterialEditingLibrary::CreateMaterialExpression(
                Material, UMaterialExpressionConstant::StaticClass(), -400, 220)))
    {
        Opacity->R = 0.0f;
        UMaterialEditingLibrary::ConnectMaterialProperty(Opacity, TEXT(""), MP_Opacity);
    }

    Material->PostEditChange();
    Material->MarkPackageDirty();
    UMaterialEditingLibrary::RecompileMaterial(Material);
}

void ApplyInvisibleVolumeInstance(UMaterialInstanceConstant* Instance)
{
    if (!Instance)
    {
        return;
    }
    Instance->BasePropertyOverrides.bOverride_BlendMode = true;
    Instance->BasePropertyOverrides.BlendMode = BLEND_Translucent;
}

void ConfigureStaticMeshAsCollisionMesh(UStaticMesh* Mesh)
{
    if (!Mesh)
    {
        return;
    }
    if (UBodySetup* BodySetup = Mesh->GetBodySetup())
    {
        Mesh->PreEditChange(nullptr);
        BodySetup->CollisionTraceFlag = CTF_UseComplexAsSimple;
        BodySetup->InvalidatePhysicsData();
        BodySetup->CreatePhysicsMeshes();
        Mesh->PostEditChange();
        Mesh->MarkPackageDirty();
    }
}

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

FString AssetRelName(const FString& AssetRel)
{
    FString Name = AssetRel;
    int32 SlashIndex = INDEX_NONE;
    if (AssetRel.FindLastChar(TEXT('/'), SlashIndex))
    {
        Name = AssetRel.Mid(SlashIndex + 1);
    }
    return Name;
}

FString DefaultContentRootForSourceGame(const FString& SourceGame)
{
    const FString Lowered = SourceGame.ToLower();
    return (Lowered == TEXT("w2") || Lowered == TEXT("witcher2") || Lowered == TEXT("tw2"))
        ? TEXT("/Game/Witcher2")
        : TEXT("/Game/Witcher3");
}

void ConfigureImportedBaseTemplate(USkeletalMeshComponent* Template, UAnimSequence* AnimSequence)
{
    // Seed the blueprint's driver-component template with the same bounds/tick
    // setup the runtime actor applies (AWitcherImportedActor reconfigures every
    // component on construction), then attach the preview clip. Mirrors RED's
    // CAnimatedComponent: the base plays the clip; leader-pose followers copy
    // its pose.
    if (!Template)
    {
        return;
    }
    AWitcherImportedActor::ConfigureBaseComponent(Template, /*bUpdateAnimationInEditor=*/true);
    if (AnimSequence)
    {
        Template->OverrideAnimationData(AnimSequence, true, true, 0.0f, 1.0f);
    }
}

bool EnsureWitcherImportedActorParent(UBlueprint* Blueprint)
{
    if (!Blueprint || Blueprint->ParentClass == AWitcherImportedActor::StaticClass())
    {
        return false;
    }
    Blueprint->ParentClass = AWitcherImportedActor::StaticClass();
    FBlueprintEditorUtils::RefreshAllNodes(Blueprint);
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
    return true;
}

USCS_Node* CreateSkeletalMeshNode(USimpleConstructionScript* ConstructionScript, const FString& MeshRel, USkeletalMesh* Mesh)
{
    if (!ConstructionScript || !Mesh)
    {
        return nullptr;
    }

    USCS_Node* Node = ConstructionScript->CreateNode(
        USkeletalMeshComponent::StaticClass(), FName(*AssetRelName(MeshRel)));
    if (USkeletalMeshComponent* Template = Cast<USkeletalMeshComponent>(Node->ComponentTemplate))
    {
        Template->SetSkeletalMeshAsset(Mesh);
    }
    return Node;
}

EMaterialSamplerType SamplerTypeForParamName(const FString& ParamName)
{
    const FString Lowered = ParamName.ToLower();
    // Rough/mask must outrank normal: extracted "<Name>Rough" textures come
    // from a normal map's alpha channel but import as masks, not normal maps.
    if (Lowered.Contains(TEXT("rough")) || Lowered.Contains(TEXT("mask")) || Lowered.Contains(TEXT("pattern")))
    {
        return SAMPLERTYPE_Masks;
    }
    if (Lowered.Contains(TEXT("normal")) || Lowered.Contains(TEXT("bump")))
    {
        return SAMPLERTYPE_Normal;
    }
    if (Lowered.Contains(TEXT("diffuse")))
    {
        return SAMPLERTYPE_Color;
    }
    return SAMPLERTYPE_LinearColor;
}

void ApplyTextureImportSettings(UTexture* Texture, const TSharedPtr<FJsonObject>& TextureObject)
{
    if (!Texture)
    {
        return;
    }
    Texture->PreEditChange(nullptr);
    Texture->SRGB = JsonBool(TextureObject, TEXT("srgb"), false);
    const FString Compression = JsonString(TextureObject, TEXT("compression"));
    if (UTexture2D* Texture2D = Cast<UTexture2D>(Texture))
    {
        if (Compression == TEXT("normalmap"))
        {
            Texture2D->CompressionSettings = TC_Normalmap;
        }
        else if (Compression == TEXT("masks"))
        {
            Texture2D->CompressionSettings = TC_Masks;
        }
        else if (Compression == TEXT("indexmap"))
        {
            Texture2D->CompressionSettings = TC_Grayscale;
            Texture2D->MipGenSettings = TMGS_NoMipmaps;
            Texture2D->Filter = TF_Nearest;
        }
        else if (Compression == TEXT("controlmap"))
        {
            Texture2D->CompressionSettings = TC_VectorDisplacementmap;
            Texture2D->MipGenSettings = TMGS_NoMipmaps;
            Texture2D->Filter = TF_Nearest;
        }
        else
        {
            Texture2D->CompressionSettings = TC_Default;
        }
    }
    Texture->PostEditChange();
    Texture->MarkPackageDirty();
}

template <typename T>
T* LoadExistingAsset(const FString& ObjectPath)
{
    return Cast<T>(StaticLoadObject(T::StaticClass(), nullptr, *ObjectPath, nullptr, LOAD_NoWarn | LOAD_Quiet));
}

/**
 * Forces the legacy FBX importer while alive. UE 5.7 routes FBX
 * AssetImportTasks through Interchange by default, which does not honor all
 * UFbxImportUI options and lacks the legacy importer's Blender "Armature"
 * root-null strip - skeletal meshes and animations then disagree about the
 * skeleton root and animations explode the bind pose.
 */
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

    ImportTextures();
    ImportMasters();
    ImportMaterials();
    ImportRig();
    ImportMeshes();
    ImportAnimations();
    ImportBlueprint();
    ImportTerrain();
    ImportPlacements();
    SaveImportedPackages();

    return BuildResponse(Errors.Num() == 0);
}

void FWitcherImportContext::ImportTextures()
{
    const TArray<TSharedPtr<FJsonValue>>* Textures = nullptr;
    if (!Manifest->TryGetArrayField(TEXT("textures"), Textures))
    {
        return;
    }
    for (const TSharedPtr<FJsonValue>& Value : *Textures)
    {
        ImportTexture(Value->AsObject());
    }
}

UTexture* FWitcherImportContext::ImportTexture(const TSharedPtr<FJsonObject>& TextureObject)
{
    if (!TextureObject.IsValid())
    {
        return nullptr;
    }
    const FString DepotRel = JsonString(TextureObject, TEXT("depot_path"));
    if (DepotRel.IsEmpty())
    {
        return nullptr;
    }
    if (const TWeakObjectPtr<UTexture>* Cached = TexturesByDepot.Find(DepotRel))
    {
        return Cached->Get();
    }

    const FString AssetRel = ClassSafeAssetRel(DepotRel, UTexture::StaticClass(), TEXT("_tex"));

    // Reuse a texture that already exists at the mirrored depot path, unless the
    // overwrite policy says to re-import it (the import below replaces in place).
    if (UTexture* Existing = LoadExistingAsset<UTexture>(ObjectPathFor(AssetRel)))
    {
        if (!ShouldOverwrite(TEXT("textures")))
        {
            TexturesByDepot.Add(DepotRel, Existing);
            return Existing;
        }
    }

    const FString SourceFile = ResolveBundleFile(JsonString(TextureObject, TEXT("file")));
    if (!FPaths::FileExists(SourceFile))
    {
        AddWarning(FString::Printf(TEXT("Texture file missing: %s"), *SourceFile));
        return nullptr;
    }

    UAssetImportTask* Task = NewObject<UAssetImportTask>();
    Task->Filename = SourceFile;
    Task->DestinationPath = PackagePathFor(AssetRel);
    Task->DestinationName = AssetRelName(AssetRel);
    Task->bAutomated = true;
    Task->bSave = false;
    Task->bReplaceExisting = true;
    Task->Factory = NewObject<UTextureFactory>();

    FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
    TArray<UAssetImportTask*> Tasks;
    Tasks.Add(Task);
    AssetToolsModule.Get().ImportAssetTasks(Tasks);

    UTexture* Texture = nullptr;
    if (Task->ImportedObjectPaths.Num() > 0)
    {
        Texture = Cast<UTexture>(StaticLoadObject(UTexture::StaticClass(), nullptr, *Task->ImportedObjectPaths[0]));
    }
    if (!Texture)
    {
        AddWarning(FString::Printf(TEXT("Failed to import texture: %s"), *SourceFile));
        return nullptr;
    }

    ApplyTextureImportSettings(Texture, TextureObject);
    ImportedAssets.Add(Texture->GetPathName());
    TexturesByDepot.Add(DepotRel, Texture);
    return Texture;
}

UTexture* FWitcherImportContext::FindTexture(const FString& DepotRel)
{
    if (DepotRel.IsEmpty())
    {
        return nullptr;
    }
    if (const TWeakObjectPtr<UTexture>* Cached = TexturesByDepot.Find(DepotRel))
    {
        return Cached->Get();
    }
    UTexture* Existing = LoadExistingAsset<UTexture>(ObjectPathFor(DepotRel));
    if (!Existing)
    {
        // Textures whose depot stem was occupied by another class import as
        // a "_tex" sibling (see ClassSafeAssetRel).
        Existing = LoadExistingAsset<UTexture>(ObjectPathFor(DepotRel + TEXT("_tex")));
    }
    if (Existing)
    {
        TexturesByDepot.Add(DepotRel, Existing);
    }
    return Existing;
}

void FWitcherImportContext::ImportMasters()
{
    const TArray<TSharedPtr<FJsonValue>>* Masters = nullptr;
    if (!Manifest->TryGetArrayField(TEXT("masters"), Masters))
    {
        return;
    }
    for (const TSharedPtr<FJsonValue>& Value : *Masters)
    {
        EnsureMasterMaterial(Value->AsObject());
    }
}

UMaterialInterface* FWitcherImportContext::EnsureMasterMaterial(const TSharedPtr<FJsonObject>& MasterObject)
{
    if (!MasterObject.IsValid())
    {
        return nullptr;
    }
    FString AssetRel = JsonString(MasterObject, TEXT("depot_path"));
    AssetRel = AssetRel.Replace(TEXT("\\"), TEXT("/"));
    if (AssetRel.EndsWith(TEXT(".w2mg")))
    {
        AssetRel.LeftChopInline(5);
    }
    if (AssetRel.IsEmpty())
    {
        return nullptr;
    }
    const bool bVolumeMaterial = JsonBool(MasterObject, TEXT("volume"), false) || IsVolumeMaterialRel(AssetRel);
    if (const TWeakObjectPtr<UMaterialInterface>* Cached = MastersByRel.Find(AssetRel))
    {
        UMaterialInterface* CachedMaterial = Cached->Get();
        if (bVolumeMaterial)
        {
            ApplyInvisibleVolumeMaterial(Cast<UMaterial>(CachedMaterial));
        }
        return CachedMaterial;
    }

    const FString SafeRel = ClassSafeAssetRel(AssetRel, UMaterialInterface::StaticClass(), TEXT("_mi"));

    // Hand-authored / previously generated masters at the mirrored .w2mg path are
    // reused as-is. With the "materials_base" overwrite policy on, the generated
    // graph is instead rebuilt in place on the existing asset (hand-authored
    // masters are only touched when the user explicitly opts into this).
    UMaterial* Material = nullptr;
    bool bRebuildExisting = false;
    if (UMaterialInterface* Existing = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(SafeRel)))
    {
        if (!ShouldOverwrite(TEXT("materials_base")))
        {
            if (bVolumeMaterial)
            {
                ApplyInvisibleVolumeMaterial(Cast<UMaterial>(Existing));
            }
            MastersByRel.Add(AssetRel, Existing);
            return Existing;
        }
        Material = Cast<UMaterial>(Existing);
        if (!Material)
        {
            // Path is occupied by something other than a plain UMaterial (e.g. an
            // MI); its graph can't be rebuilt in place, so reuse it as-is.
            MastersByRel.Add(AssetRel, Existing);
            return Existing;
        }
        bRebuildExisting = true;
    }

    if (!Material)
    {
        const FString Name = AssetRelName(SafeRel);
        FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
        UMaterialFactoryNew* Factory = NewObject<UMaterialFactoryNew>();
        Material = Cast<UMaterial>(
            AssetToolsModule.Get().CreateAsset(Name, PackagePathFor(SafeRel), UMaterial::StaticClass(), Factory));
    }
    if (!Material)
    {
        AddWarning(FString::Printf(TEXT("Could not create master material '%s'"), *SafeRel));
        return nullptr;
    }

    if (bVolumeMaterial)
    {
        ApplyInvisibleVolumeMaterial(Material);
        ImportedAssets.AddUnique(Material->GetPathName());
        MastersByRel.Add(AssetRel, Material);
        return Material;
    }

    Material->PreEditChange(nullptr);
    if (bRebuildExisting)
    {
        // Clear the previously generated graph before rebuilding it in place.
        UMaterialEditingLibrary::DeleteAllMaterialExpressions(Material);
    }

    UMaterialExpression* BaseColorSource = nullptr;
    UMaterialExpression* NormalSource = nullptr;
    UMaterialExpression* RoughTextureSource = nullptr;
    UMaterialExpression* RoughScalarSource = nullptr;
    bool BaseColorIsExact = false;
    bool NormalIsExact = false;
    bool RoughIsExact = false;

    int32 NodeY = 0;
    const TArray<TSharedPtr<FJsonValue>>* Params = nullptr;
    if (MasterObject->TryGetArrayField(TEXT("params"), Params))
    {
        for (const TSharedPtr<FJsonValue>& Value : *Params)
        {
            const TSharedPtr<FJsonObject> Param = Value->AsObject();
            const FString ParamName = JsonString(Param, TEXT("name"));
            const FString Kind = JsonString(Param, TEXT("kind"));
            if (ParamName.IsEmpty())
            {
                continue;
            }

            UMaterialExpression* Expression = nullptr;
            if (Kind == TEXT("texture"))
            {
                UMaterialExpressionTextureSampleParameter2D* TextureExpression =
                    Cast<UMaterialExpressionTextureSampleParameter2D>(UMaterialEditingLibrary::CreateMaterialExpression(
                        Material, UMaterialExpressionTextureSampleParameter2D::StaticClass(), -500, NodeY));
                if (TextureExpression)
                {
                    TextureExpression->ParameterName = FName(*ParamName);
                    // The sampler follows the param's semantic (instance
                    // overrides are imported with matching settings); the
                    // node's texture must agree with the sampler or the
                    // master fails to compile, so incompatible defaults are
                    // replaced with engine fallbacks or left empty.
                    TextureExpression->SamplerType = SamplerTypeForParamName(ParamName);
                    // UE seeds new texture samples with DefaultTexture, which
                    // is a Color sampler texture. Clear it so mask/normal
                    // params do not compile against an incompatible implicit
                    // default when no matching Witcher texture exists.
                    TextureExpression->Texture = nullptr;
                    UTexture* DefaultTexture = FindTexture(JsonString(Param, TEXT("depot")));
                    if (DefaultTexture &&
                        UMaterialExpressionTextureBase::GetSamplerTypeForTexture(DefaultTexture) != TextureExpression->SamplerType)
                    {
                        DefaultTexture = nullptr;
                    }
                    if (!DefaultTexture && TextureExpression->SamplerType == SAMPLERTYPE_Color)
                    {
                        DefaultTexture = LoadExistingAsset<UTexture>(TEXT("/Engine/EngineResources/DefaultTexture.DefaultTexture"));
                    }
                    if (!DefaultTexture && TextureExpression->SamplerType == SAMPLERTYPE_Normal)
                    {
                        DefaultTexture = LoadExistingAsset<UTexture>(TEXT("/Engine/EngineMaterials/DefaultNormal.DefaultNormal"));
                    }
                    if (DefaultTexture)
                    {
                        TextureExpression->Texture = DefaultTexture;
                    }
                    const FString LoweredName = ParamName.ToLower();
                    const bool bExactDiffuse = LoweredName == TEXT("diffuse");
                    if (bExactDiffuse || (!BaseColorSource && LoweredName.Contains(TEXT("diffuse"))))
                    {
                        if (bExactDiffuse || !BaseColorIsExact)
                        {
                            BaseColorSource = TextureExpression;
                            BaseColorIsExact = bExactDiffuse;
                        }
                    }
                    const bool bExactNormal = LoweredName == TEXT("normal");
                    if (bExactNormal || (!NormalSource && TextureExpression->SamplerType == SAMPLERTYPE_Normal))
                    {
                        if (bExactNormal || !NormalIsExact)
                        {
                            NormalSource = TextureExpression;
                            NormalIsExact = bExactNormal;
                        }
                    }
                    const bool bExactRough = LoweredName == TEXT("normalrough");
                    if (bExactRough || (!RoughTextureSource && LoweredName.Contains(TEXT("rough"))))
                    {
                        if (bExactRough || !RoughIsExact)
                        {
                            RoughTextureSource = TextureExpression;
                            RoughIsExact = bExactRough;
                        }
                    }
                }
                Expression = TextureExpression;
                NodeY += 300;
            }
            else if (Kind == TEXT("scalar"))
            {
                UMaterialExpressionScalarParameter* ScalarExpression =
                    Cast<UMaterialExpressionScalarParameter>(UMaterialEditingLibrary::CreateMaterialExpression(
                        Material, UMaterialExpressionScalarParameter::StaticClass(), -500, NodeY));
                if (ScalarExpression)
                {
                    ScalarExpression->ParameterName = FName(*ParamName);
                    ScalarExpression->DefaultValue = static_cast<float>(JsonNumber(Param, TEXT("value"), 0.0));
                    if (!RoughScalarSource && ParamName.ToLower().Contains(TEXT("rough")))
                    {
                        RoughScalarSource = ScalarExpression;
                    }
                }
                Expression = ScalarExpression;
                NodeY += 150;
            }
            else if (Kind == TEXT("vector"))
            {
                UMaterialExpressionVectorParameter* VectorExpression =
                    Cast<UMaterialExpressionVectorParameter>(UMaterialEditingLibrary::CreateMaterialExpression(
                        Material, UMaterialExpressionVectorParameter::StaticClass(), -500, NodeY));
                if (VectorExpression)
                {
                    VectorExpression->ParameterName = FName(*ParamName);
                    VectorExpression->DefaultValue = JsonColor(Param, TEXT("value"));
                }
                Expression = VectorExpression;
                NodeY += 200;
            }

            if (!Expression)
            {
                AddWarning(FString::Printf(TEXT("Master '%s': could not create parameter '%s'"), *AssetRel, *ParamName));
            }
        }
    }

    // A texture parameter node without a texture fails compilation when
    // connected, so only wire pins whose node holds a valid default.
    auto NodeHasTexture = [](UMaterialExpression* Expression) -> bool
    {
        const UMaterialExpressionTextureBase* TextureBase = Cast<UMaterialExpressionTextureBase>(Expression);
        return TextureBase && TextureBase->Texture != nullptr;
    };

    if (BaseColorSource && NodeHasTexture(BaseColorSource))
    {
        UMaterialEditingLibrary::ConnectMaterialProperty(BaseColorSource, TEXT(""), MP_BaseColor);
    }
    if (NormalSource && NodeHasTexture(NormalSource))
    {
        UMaterialEditingLibrary::ConnectMaterialProperty(NormalSource, TEXT(""), MP_Normal);
    }
    if (RoughTextureSource && NodeHasTexture(RoughTextureSource))
    {
        UMaterialEditingLibrary::ConnectMaterialProperty(RoughTextureSource, TEXT(""), MP_Roughness);
    }
    else if (RoughScalarSource)
    {
        UMaterialEditingLibrary::ConnectMaterialProperty(RoughScalarSource, TEXT(""), MP_Roughness);
    }

    Material->PostEditChange();
    Material->MarkPackageDirty();
    UMaterialEditingLibrary::RecompileMaterial(Material);
    ImportedAssets.AddUnique(Material->GetPathName());
    MastersByRel.Add(AssetRel, Material);
    return Material;
}

void FWitcherImportContext::ImportMaterials()
{
    const TArray<TSharedPtr<FJsonValue>>* Materials = nullptr;
    if (!Manifest->TryGetArrayField(TEXT("materials"), Materials))
    {
        return;
    }
    for (const TSharedPtr<FJsonValue>& Value : *Materials)
    {
        ImportMaterialInstance(Value->AsObject());
    }
}

UMaterialInterface* FWitcherImportContext::ResolveParent(const TSharedPtr<FJsonObject>& MaterialObject)
{
    // Materials displaced by a same-stem asset of another class live at a
    // "_mi" sibling path (see ClassSafeAssetRel), so check both.
    auto LoadMaterialAt = [this](const FString& AssetRel) -> UMaterialInterface*
    {
        if (UMaterialInterface* Existing = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(AssetRel)))
        {
            return Existing;
        }
        return LoadExistingAsset<UMaterialInterface>(ObjectPathFor(AssetRel + TEXT("_mi")));
    };

    const FString ParentMaterialId = JsonString(MaterialObject, TEXT("parent_material"));
    if (!ParentMaterialId.IsEmpty())
    {
        if (const TWeakObjectPtr<UMaterialInterface>* Found = MaterialsById.Find(ParentMaterialId))
        {
            return Found->Get();
        }
        if (UMaterialInterface* Existing = LoadMaterialAt(ParentMaterialId))
        {
            return Existing;
        }
    }

    const FString ParentMasterRel = JsonString(MaterialObject, TEXT("parent_master"));
    if (!ParentMasterRel.IsEmpty())
    {
        if (const TWeakObjectPtr<UMaterialInterface>* Found = MastersByRel.Find(ParentMasterRel))
        {
            return Found->Get();
        }
        if (UMaterialInterface* Existing = LoadMaterialAt(ParentMasterRel))
        {
            return Existing;
        }
    }
    return nullptr;
}

UMaterialInterface* FWitcherImportContext::ImportMaterialInstance(const TSharedPtr<FJsonObject>& MaterialObject)
{
    if (!MaterialObject.IsValid())
    {
        return nullptr;
    }
    const FString MaterialId = JsonString(MaterialObject, TEXT("id"));
    const FString AssetRel = JsonString(MaterialObject, TEXT("asset_path"), MaterialId);
    if (AssetRel.IsEmpty())
    {
        return nullptr;
    }

    const FString SafeRel = ClassSafeAssetRel(AssetRel, UMaterialInterface::StaticClass(), TEXT("_mi"));
    const bool bVolumeMaterial = JsonBool(MaterialObject, TEXT("volume"), false);

    // Existing chain instances at the mirrored path are reused untouched so
    // manual tweaks survive re-export. Local (mesh-owned) instances refresh
    // their parameters each export, keeping Blender edits flowing through. With
    // the "material_instances" overwrite policy on, every existing instance is
    // fully refreshed (parent + blend overrides + params), not just local ones.
    if (UMaterialInterface* Existing = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(SafeRel)))
    {
        const bool bOverwrite = ShouldOverwrite(TEXT("material_instances"));
        const bool bLocal = JsonBool(MaterialObject, TEXT("local"), false);
        if (bOverwrite || bLocal || bVolumeMaterial)
        {
            if (UMaterialInstanceConstant* ExistingInstance = Cast<UMaterialInstanceConstant>(Existing))
            {
                ExistingInstance->PreEditChange(nullptr);
                if (bOverwrite)
                {
                    if (UMaterialInterface* Parent = ResolveParent(MaterialObject))
                    {
                        ExistingInstance->SetParentEditorOnly(Parent);
                    }
                }
                if (bVolumeMaterial)
                {
                    ApplyInvisibleVolumeInstance(ExistingInstance);
                }
                else if (bOverwrite && JsonBool(MaterialObject, TEXT("enable_mask"), false))
                {
                    ExistingInstance->BasePropertyOverrides.bOverride_BlendMode = true;
                    ExistingInstance->BasePropertyOverrides.BlendMode = BLEND_Masked;
                }
                if (bOverwrite || bLocal)
                {
                    ApplyInstanceParams(ExistingInstance, MaterialObject);
                }
                ExistingInstance->PostEditChange();
                ExistingInstance->MarkPackageDirty();
                ImportedAssets.AddUnique(ExistingInstance->GetPathName());
            }
        }
        MaterialsById.Add(MaterialId, Existing);
        return Existing;
    }

    UMaterialInterface* ParentMaterial = ResolveParent(MaterialObject);
    if (!ParentMaterial)
    {
        AddWarning(FString::Printf(TEXT("Material '%s' has no resolvable parent; skipping"), *SafeRel));
        return nullptr;
    }

    const FString Name = AssetRelName(SafeRel);
    const FString PackageName = FString::Printf(TEXT("%s/%s"), *ContentRoot, *SafeRel);
    UPackage* Package = CreatePackage(*PackageName);
    UMaterialInstanceConstant* MaterialInstance = NewObject<UMaterialInstanceConstant>(Package, *Name, RF_Public | RF_Standalone);
    if (!MaterialInstance)
    {
        AddWarning(FString::Printf(TEXT("Could not create material instance '%s'"), *SafeRel));
        return nullptr;
    }
    FAssetRegistryModule::AssetCreated(MaterialInstance);

    MaterialInstance->PreEditChange(nullptr);
    MaterialInstance->SetParentEditorOnly(ParentMaterial);
    if (bVolumeMaterial)
    {
        ApplyInvisibleVolumeInstance(MaterialInstance);
    }
    else if (JsonBool(MaterialObject, TEXT("enable_mask"), false))
    {
        MaterialInstance->BasePropertyOverrides.bOverride_BlendMode = true;
        MaterialInstance->BasePropertyOverrides.BlendMode = BLEND_Masked;
    }
    ApplyInstanceParams(MaterialInstance, MaterialObject);
    MaterialInstance->PostEditChange();
    MaterialInstance->MarkPackageDirty();

    ImportedAssets.Add(MaterialInstance->GetPathName());
    MaterialsById.Add(MaterialId, MaterialInstance);
    return MaterialInstance;
}

void FWitcherImportContext::ApplyInstanceParams(UMaterialInstanceConstant* Instance, const TSharedPtr<FJsonObject>& MaterialObject)
{
    const TArray<TSharedPtr<FJsonValue>>* Params = nullptr;
    if (!MaterialObject->TryGetArrayField(TEXT("params"), Params))
    {
        return;
    }
    for (const TSharedPtr<FJsonValue>& Value : *Params)
    {
        const TSharedPtr<FJsonObject> Param = Value->AsObject();
        const FString Name = JsonString(Param, TEXT("name"));
        const FString Kind = JsonString(Param, TEXT("kind"));
        if (Name.IsEmpty())
        {
            continue;
        }
        if (Kind == TEXT("texture"))
        {
            if (UTexture* Texture = FindTexture(JsonString(Param, TEXT("depot"))))
            {
                Instance->SetTextureParameterValueEditorOnly(FMaterialParameterInfo(*Name), Texture);
            }
            else
            {
                AddWarning(FString::Printf(TEXT("%s: texture '%s' was not imported"),
                    *Instance->GetName(), *JsonString(Param, TEXT("depot"))));
            }
        }
        else if (Kind == TEXT("scalar"))
        {
            Instance->SetScalarParameterValueEditorOnly(FMaterialParameterInfo(*Name),
                static_cast<float>(JsonNumber(Param, TEXT("value"), 0.0)));
        }
        else if (Kind == TEXT("vector"))
        {
            Instance->SetVectorParameterValueEditorOnly(FMaterialParameterInfo(*Name), JsonColor(Param, TEXT("value")));
        }
    }
}

void FWitcherImportContext::ImportRig()
{
    const TSharedPtr<FJsonObject>* RigObject = nullptr;
    if (!Manifest->TryGetObjectField(TEXT("rig"), RigObject))
    {
        return;
    }

    const FString AssetRel = JsonString(*RigObject, TEXT("asset_path"));
    const FString SkeletonPath = ObjectPathFor(AssetRel + TEXT("_Skeleton"));
    if (USkeleton* ExistingSkeleton = LoadExistingAsset<USkeleton>(SkeletonPath))
    {
        if (!ShouldOverwrite(TEXT("skeletons")))
        {
            SharedSkeleton = ExistingSkeleton;
            return;
        }
    }

    UObject* Imported = ImportFbxMesh(JsonString(*RigObject, TEXT("fbx")), AssetRel, true, nullptr);
    if (USkeletalMesh* SkeletalMesh = Cast<USkeletalMesh>(Imported))
    {
        SharedSkeleton = SkeletalMesh->GetSkeleton();
    }
    if (!SharedSkeleton.IsValid())
    {
        AddWarning(FString::Printf(TEXT("Rig '%s' did not produce a skeleton; meshes will create their own"), *AssetRel));
    }
}

void FWitcherImportContext::ImportMeshes()
{
    const TArray<TSharedPtr<FJsonValue>>* Meshes = nullptr;
    if (!Manifest->TryGetArrayField(TEXT("meshes"), Meshes))
    {
        return;
    }
    for (const TSharedPtr<FJsonValue>& Value : *Meshes)
    {
        const TSharedPtr<FJsonObject> MeshEntry = Value->AsObject();
        if (!MeshEntry.IsValid())
        {
            continue;
        }
        const FString AssetRel = JsonString(MeshEntry, TEXT("asset_path"));
        const bool bSkeletal = JsonString(MeshEntry, TEXT("kind")) == TEXT("skeletal");
        const bool bCollisionMesh = JsonBool(MeshEntry, TEXT("collision"), false);
        const bool bOwnSkeleton = JsonBool(MeshEntry, TEXT("own_skeleton"), false);
        USkeleton* MeshSkeleton = (bSkeletal && !bOwnSkeleton) ? SharedSkeleton.Get() : nullptr;

        if (!ShouldOverwrite(TEXT("meshes")))
        {
            UObject* ExistingMesh = bSkeletal
                ? static_cast<UObject*>(LoadExistingAsset<USkeletalMesh>(ObjectPathFor(AssetRel)))
                : static_cast<UObject*>(LoadExistingAsset<UStaticMesh>(ObjectPathFor(AssetRel)));
            if (ExistingMesh)
            {
                MeshesByAssetRel.Add(AssetRel, ExistingMesh);
                if (bSkeletal && !bOwnSkeleton && !SharedSkeleton.IsValid())
                {
                    if (USkeletalMesh* SkeletalMesh = Cast<USkeletalMesh>(ExistingMesh))
                    {
                        SharedSkeleton = SkeletalMesh->GetSkeleton();
                    }
                }
                continue;
            }
        }

        UObject* Imported = ImportFbxMesh(JsonString(MeshEntry, TEXT("fbx")), AssetRel, bSkeletal,
            MeshSkeleton);
        if (!Imported)
        {
            AddError(FString::Printf(TEXT("Failed to import mesh '%s'"), *AssetRel));
            continue;
        }
        MeshesByAssetRel.Add(AssetRel, Imported);
        if (bSkeletal && !bOwnSkeleton && !SharedSkeleton.IsValid())
        {
            if (USkeletalMesh* SkeletalMesh = Cast<USkeletalMesh>(Imported))
            {
                SharedSkeleton = SkeletalMesh->GetSkeleton();
            }
        }
        if (bCollisionMesh)
        {
            ConfigureStaticMeshAsCollisionMesh(Cast<UStaticMesh>(Imported));
            continue;
        }
        AssignMaterialsToMesh(Imported, MeshEntry);
    }
}

UObject* FWitcherImportContext::ImportFbxMesh(const FString& BundleRelativeFbx, const FString& AssetRel, bool bSkeletal, USkeleton* Skeleton)
{
    const FString FbxPath = ResolveBundleFile(BundleRelativeFbx);
    if (!FPaths::FileExists(FbxPath))
    {
        AddWarning(FString::Printf(TEXT("FBX file does not exist: %s"), *FbxPath));
        return nullptr;
    }

    UAssetImportTask* Task = NewObject<UAssetImportTask>();
    Task->Filename = FbxPath;
    Task->DestinationPath = PackagePathFor(AssetRel);
    Task->DestinationName = AssetRelName(AssetRel);
    Task->bAutomated = true;
    Task->bSave = false;
    Task->bReplaceExisting = true;

    UFbxImportUI* Options = NewObject<UFbxImportUI>();
    Options->bImportMaterials = false;
    Options->bImportTextures = false;
    Options->bImportAnimations = false;
    Options->bCreatePhysicsAsset = false;
    Options->bAutomatedImportShouldDetectType = false;
    Options->MeshTypeToImport = bSkeletal ? FBXIT_SkeletalMesh : FBXIT_StaticMesh;
    Options->bImportAsSkeletal = bSkeletal;
    if (Skeleton)
    {
        Options->Skeleton = Skeleton;
    }
    if (Options->SkeletalMeshImportData)
    {
        Options->SkeletalMeshImportData->bImportMeshesInBoneHierarchy = true;
        // Honor the custom split normals + tangent basis the Blender exporter
        Options->SkeletalMeshImportData->NormalImportMethod = FBXNIM_ImportNormalsAndTangents;
        Options->SkeletalMeshImportData->NormalGenerationMethod = EFBXNormalGenerationMethod::MikkTSpace;
    }
    if (Options->StaticMeshImportData)
    {
        Options->StaticMeshImportData->bCombineMeshes = true;
        Options->StaticMeshImportData->bAutoGenerateCollision = false;
        Options->StaticMeshImportData->NormalImportMethod = FBXNIM_ImportNormalsAndTangents;
        Options->StaticMeshImportData->NormalGenerationMethod = EFBXNormalGenerationMethod::MikkTSpace;
    }
    Task->Options = Options;

    FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
    TArray<UAssetImportTask*> Tasks;
    Tasks.Add(Task);
    AssetToolsModule.Get().ImportAssetTasks(Tasks);

    UObject* MainAsset = nullptr;
    for (const FString& Path : Task->ImportedObjectPaths)
    {
        UObject* Imported = StaticLoadObject(UObject::StaticClass(), nullptr, *Path);
        if (!Imported)
        {
            continue;
        }
        ImportedAssets.Add(Path);
        if (!MainAsset && (Imported->IsA<USkeletalMesh>() || Imported->IsA<UStaticMesh>()))
        {
            MainAsset = Imported;
        }
    }

    if (MainAsset && MainAsset->GetPathName() != ObjectPathFor(AssetRel))
    {
        AddWarning(FString::Printf(
            TEXT("Mesh '%s' imported as '%s' instead of replacing the existing asset; the original may be open in an editor or locked."),
            *AssetRel, *MainAsset->GetPathName()));
    }
    return MainAsset;
}

void FWitcherImportContext::ImportAnimations()
{
    const TArray<TSharedPtr<FJsonValue>>* Animations = nullptr;
    if (!Manifest->TryGetArrayField(TEXT("animations"), Animations) || Animations->Num() == 0)
    {
        return;
    }
    if (!SharedSkeleton.IsValid())
    {
        AddWarning(TEXT("Animations skipped: no shared skeleton was imported."));
        return;
    }

    for (const TSharedPtr<FJsonValue>& Value : *Animations)
    {
        const TSharedPtr<FJsonObject> AnimationObject = Value->AsObject();
        if (!AnimationObject.IsValid())
        {
            continue;
        }
        UAnimSequence* Imported = ImportAnimation(AnimationObject);
        if (!Imported)
        {
            AddError(FString::Printf(TEXT("Failed to import animation '%s'"),
                *JsonString(AnimationObject, TEXT("asset_path"))));
        }
    }
}

UAnimSequence* FWitcherImportContext::ImportAnimation(const TSharedPtr<FJsonObject>& AnimationObject)
{
    if (!AnimationObject.IsValid() || !SharedSkeleton.IsValid())
    {
        return nullptr;
    }

    const FString RawAssetRel = JsonString(AnimationObject, TEXT("asset_path"));
    if (RawAssetRel.IsEmpty())
    {
        return nullptr;
    }
    const FString AssetRel = ClassSafeAssetRel(RawAssetRel, UAnimSequence::StaticClass(), TEXT("_anim"));

    // Reuse an existing animation
    if (!ShouldOverwrite(TEXT("animations")))
    {
        if (UAnimSequence* Existing = FindAnimSequence(RawAssetRel))
        {
            return Existing;
        }
    }

    const FString FbxPath = ResolveBundleFile(JsonString(AnimationObject, TEXT("fbx")));
    if (!FPaths::FileExists(FbxPath))
    {
        AddWarning(FString::Printf(TEXT("Animation FBX file does not exist: %s"), *FbxPath));
        return nullptr;
    }

    UAssetImportTask* Task = NewObject<UAssetImportTask>();
    Task->Filename = FbxPath;
    Task->DestinationPath = PackagePathFor(AssetRel);
    Task->DestinationName = AssetRelName(AssetRel);
    Task->bAutomated = true;
    Task->bSave = false;
    Task->bReplaceExisting = true;

    UFbxImportUI* Options = NewObject<UFbxImportUI>();
    Options->bImportMaterials = false;
    Options->bImportTextures = false;
    Options->bImportAnimations = true;
    Options->bCreatePhysicsAsset = false;
    Options->bAutomatedImportShouldDetectType = false;
    Options->MeshTypeToImport = FBXIT_Animation;
    Options->bImportAsSkeletal = true;
    Options->Skeleton = SharedSkeleton.Get();
    Options->bImportMesh = false;
    if (Options->AnimSequenceImportData)
    {
        // Blender FBXs carry the m->cm factor on the Armature root null.
        // Skeletal mesh import folds that into the root bone (ref pose root
        // scale=100), but the anim importer divides the parent transform out
        // of the root track (scale=1) -- the skeleton then collapses 100x
        // when a clip plays. ImportUniformScale is multiplied onto the root
        // track only (SkeletalMeshEdit.cpp), restoring the same structure
        // the skeleton has.
        Options->AnimSequenceImportData->ImportUniformScale = 100.0f;
    }
    Task->Options = Options;

    FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
    TArray<UAssetImportTask*> Tasks;
    Tasks.Add(Task);
    AssetToolsModule.Get().ImportAssetTasks(Tasks);

    UAnimSequence* AnimSequence = nullptr;
    for (const FString& Path : Task->ImportedObjectPaths)
    {
        UObject* Imported = StaticLoadObject(UObject::StaticClass(), nullptr, *Path);
        if (!Imported)
        {
            continue;
        }
        ImportedAssets.Add(Path);
        if (!AnimSequence)
        {
            AnimSequence = Cast<UAnimSequence>(Imported);
        }
    }
    return AnimSequence;
}

UAnimSequence* FWitcherImportContext::FindAnimSequence(const FString& AssetRel)
{
    if (AssetRel.IsEmpty())
    {
        return nullptr;
    }
    if (UAnimSequence* Existing = LoadExistingAsset<UAnimSequence>(ObjectPathFor(AssetRel)))
    {
        return Existing;
    }
    // Animations whose depot stem was occupied by another class import as an
    // "_anim" sibling (see ClassSafeAssetRel in ImportAnimation).
    return LoadExistingAsset<UAnimSequence>(ObjectPathFor(AssetRel + TEXT("_anim")));
}

void FWitcherImportContext::AssignMaterialsToMesh(UObject* MeshObject, const TSharedPtr<FJsonObject>& MeshEntry)
{
    const TArray<TSharedPtr<FJsonValue>>* Slots = nullptr;
    if (!MeshEntry->TryGetArrayField(TEXT("slots"), Slots))
    {
        return;
    }

    TMap<int32, UMaterialInterface*> MaterialsBySlot;
    for (const TSharedPtr<FJsonValue>& Value : *Slots)
    {
        const TSharedPtr<FJsonObject> Slot = Value->AsObject();
        const FString MaterialId = JsonString(Slot, TEXT("material_id"));
        UMaterialInterface* Material = nullptr;
        if (const TWeakObjectPtr<UMaterialInterface>* Found = MaterialsById.Find(MaterialId))
        {
            Material = Found->Get();
        }
        if (!Material && !MaterialId.IsEmpty())
        {
            Material = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(MaterialId));
            if (!Material)
            {
                Material = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(MaterialId + TEXT("_mi")));
            }
        }
        if (Material)
        {
            MaterialsBySlot.Add(JsonInt(Slot, TEXT("slot_index"), 0), Material);
        }
        else if (!MaterialId.IsEmpty())
        {
            AddWarning(FString::Printf(TEXT("%s: material '%s' was not created"),
                *MeshObject->GetName(), *MaterialId));
        }
    }

    if (UStaticMesh* StaticMesh = Cast<UStaticMesh>(MeshObject))
    {
        const int32 Count = StaticMesh->GetStaticMaterials().Num();
        for (const TPair<int32, UMaterialInterface*>& Pair : MaterialsBySlot)
        {
            if (Pair.Key >= 0 && Pair.Key < Count)
            {
                StaticMesh->SetMaterial(Pair.Key, Pair.Value);
            }
        }
        StaticMesh->MarkPackageDirty();
    }
    else if (USkeletalMesh* SkeletalMesh = Cast<USkeletalMesh>(MeshObject))
    {
        TArray<FSkeletalMaterial>& SkeletalMaterials = SkeletalMesh->GetMaterials();
        for (const TPair<int32, UMaterialInterface*>& Pair : MaterialsBySlot)
        {
            if (Pair.Key >= 0 && Pair.Key < SkeletalMaterials.Num())
            {
                SkeletalMaterials[Pair.Key].MaterialInterface = Pair.Value;
            }
        }
        SkeletalMesh->MarkPackageDirty();
    }
}

void FWitcherImportContext::ImportBlueprint()
{
    const TSharedPtr<FJsonObject>* BlueprintObject = nullptr;
    if (!Manifest->TryGetObjectField(TEXT("blueprint"), BlueprintObject))
    {
        return;
    }

    const FString AssetRel = JsonString(*BlueprintObject, TEXT("asset_path"));
    if (AssetRel.IsEmpty())
    {
        return;
    }
    const FString AnimationRel = JsonString(*BlueprintObject, TEXT("animation_asset_path"));
    UAnimSequence* AnimSequence = AnimationRel.IsEmpty() ? nullptr : FindAnimSequence(AnimationRel);
    if (!AnimationRel.IsEmpty() && !AnimSequence)
    {
        AddWarning(FString::Printf(TEXT("Blueprint '%s': animation '%s' not found"), *AssetRel, *AnimationRel));
    }
    const FString Name = AssetRelName(AssetRel);

    auto RebuildBlueprintComponents = [&](UBlueprint* Blueprint) -> USkeletalMeshComponent*
    {
        if (!Blueprint || !Blueprint->SimpleConstructionScript)
        {
            return nullptr;
        }

        USimpleConstructionScript* ConstructionScript = Blueprint->SimpleConstructionScript;
        // Remove ALL existing nodes (not just roots) so a rebuild starts from a clean slate.
        const TArray<USCS_Node*> ExistingNodes = ConstructionScript->GetAllNodes();
        for (USCS_Node* Node : ExistingNodes)
        {
            if (Node)
            {
                ConstructionScript->RemoveNode(Node, false);
            }
        }

        USCS_Node* RootNode = nullptr;
        USkeletalMeshComponent* BaseTemplate = nullptr;
        const FString BaseMeshRel = JsonString(*BlueprintObject, TEXT("base_mesh_asset_path"));
        if (!BaseMeshRel.IsEmpty())
        {
            USkeletalMesh* BaseMesh = LoadExistingAsset<USkeletalMesh>(ObjectPathFor(BaseMeshRel));
            if (BaseMesh)
            {
                RootNode = CreateSkeletalMeshNode(ConstructionScript, BaseMeshRel, BaseMesh);
                if (RootNode)
                {
                    BaseTemplate = Cast<USkeletalMeshComponent>(RootNode->ComponentTemplate);
                    ConstructionScript->AddNode(RootNode);
                }
            }
            else
            {
                AddWarning(FString::Printf(TEXT("Blueprint '%s': base mesh '%s' not found"), *Name, *BaseMeshRel));
            }
        }

        const TArray<TSharedPtr<FJsonValue>>* Parts = nullptr;
        if ((*BlueprintObject)->TryGetArrayField(TEXT("mesh_asset_paths"), Parts))
        {
            for (const TSharedPtr<FJsonValue>& Value : *Parts)
            {
                const FString PartRel = Value->AsString();
                USkeletalMesh* PartMesh = LoadExistingAsset<USkeletalMesh>(ObjectPathFor(PartRel));
                if (!PartMesh)
                {
                    AddWarning(FString::Printf(TEXT("Blueprint '%s': part mesh '%s' not found"), *Name, *PartRel));
                    continue;
                }

                USCS_Node* Node = CreateSkeletalMeshNode(ConstructionScript, PartRel, PartMesh);
                if (!Node)
                {
                    continue;
                }
                if (USkeletalMeshComponent* Template = Cast<USkeletalMeshComponent>(Node->ComponentTemplate))
                {
                    if (BaseTemplate)
                    {
                        AWitcherImportedActor::ConfigureFollowerComponent(
                            Template, BaseTemplate, /*bUpdateAnimationInEditor=*/true);
                    }
                }
                if (!RootNode)
                {
                    ConstructionScript->AddNode(Node);
                    RootNode = Node;
                    BaseTemplate = Cast<USkeletalMeshComponent>(Node->ComponentTemplate);
                }
                else
                {
                    RootNode->AddChildNode(Node);
                }
            }
        }

        const TArray<TSharedPtr<FJsonValue>>* Attachments = nullptr;
        if ((*BlueprintObject)->TryGetArrayField(TEXT("attachments"), Attachments))
        {
            for (const TSharedPtr<FJsonValue>& Value : *Attachments)
            {
                const TSharedPtr<FJsonObject> AttachObj = Value->AsObject();
                if (!AttachObj.IsValid())
                {
                    continue;
                }
                const FString AttachRel = JsonString(AttachObj, TEXT("asset_path"));
                const FString AttachBone = JsonString(AttachObj, TEXT("attach_to_bone"));
                USkeletalMesh* AttachMesh = LoadExistingAsset<USkeletalMesh>(ObjectPathFor(AttachRel));
                if (!AttachMesh)
                {
                    AddWarning(FString::Printf(TEXT("Blueprint '%s': attachment mesh '%s' not found"), *Name, *AttachRel));
                    continue;
                }
                if (!RootNode)
                {
                    AddWarning(FString::Printf(TEXT("Blueprint '%s': attachment '%s' has no base mesh to attach to"), *Name, *AttachRel));
                    continue;
                }
                USCS_Node* Node = CreateSkeletalMeshNode(ConstructionScript, AttachRel, AttachMesh);
                if (!Node)
                {
                    continue;
                }
                if (USkeletalMeshComponent* Template = Cast<USkeletalMeshComponent>(Node->ComponentTemplate))
                {
                    Template->SetRelativeTransform(FTransform::Identity);
                    Template->SetAbsolute(false, false, true);
                    Template->VisibilityBasedAnimTickOption = EVisibilityBasedAnimTickOption::AlwaysTickPoseAndRefreshBones;
                }
                Node->AttachToName = AttachBone.IsEmpty() ? NAME_None : FName(*AttachBone);
                RootNode->AddChildNode(Node);
            }
        }

        ConstructionScript->ValidateSceneRootNodes();
        return BaseTemplate;
    };

    if (UBlueprint* ExistingBlueprint = LoadExistingAsset<UBlueprint>(ObjectPathFor(AssetRel)))
    {
        if (!ShouldOverwrite(TEXT("blueprints")))
        {
            return;
        }
        const bool bReparented = EnsureWitcherImportedActorParent(ExistingBlueprint);
        if (USkeletalMeshComponent* BaseTemplate = RebuildBlueprintComponents(ExistingBlueprint))
        {
            // RebuildBlueprintComponents already recreated and configured every
            // follower; only the driver still needs its base/anim setup.
            ConfigureImportedBaseTemplate(BaseTemplate, AnimSequence);
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(ExistingBlueprint);
            FKismetEditorUtilities::CompileBlueprint(ExistingBlueprint);
            ExistingBlueprint->MarkPackageDirty();
            ImportedAssets.AddUnique(ExistingBlueprint->GetPathName());
            AddWarning(FString::Printf(TEXT("Blueprint '%s' already exists; rebuilt its skeletal components%s"),
                *AssetRel, bReparented ? TEXT(" and runtime actor parent") : TEXT("")));
            return;
        }
        AddWarning(FString::Printf(TEXT("Blueprint '%s' already exists; could not rebuild skeletal components"), *AssetRel));
        return;
    }

    const FString PackageName = FString::Printf(TEXT("%s/%s"), *ContentRoot, *AssetRel);
    UPackage* Package = CreatePackage(*PackageName);
    UBlueprint* Blueprint = FKismetEditorUtilities::CreateBlueprint(
        AWitcherImportedActor::StaticClass(), Package, FName(*Name), BPTYPE_Normal,
        UBlueprint::StaticClass(), UBlueprintGeneratedClass::StaticClass());
    if (!Blueprint)
    {
        AddWarning(FString::Printf(TEXT("Could not create blueprint '%s'"), *AssetRel));
        return;
    }
    FAssetRegistryModule::AssetCreated(Blueprint);

    USkeletalMeshComponent* BaseTemplate = RebuildBlueprintComponents(Blueprint);
    ConfigureImportedBaseTemplate(BaseTemplate, AnimSequence);

    FKismetEditorUtilities::CompileBlueprint(Blueprint);
    Blueprint->MarkPackageDirty();
    ImportedAssets.Add(Blueprint->GetPathName());
}

UTexture2DArray* FWitcherImportContext::BuildTerrainTextureArray(
    const TArray<UTexture2D*>& Slices, const FString& AssetRel, bool bNormal)
{
    if (Slices.Num() == 0)
    {
        return nullptr;
    }
    UTexture2DArray* Array = LoadExistingAsset<UTexture2DArray>(ObjectPathFor(AssetRel));
    const bool bUpdatingExistingArray = Array != nullptr;
    if (bUpdatingExistingArray && !ShouldOverwrite(TEXT("textures")))
    {
        return Array;
    }
    if (!Array)
    {
        const FString PackageName = FString::Printf(TEXT("%s/%s"), *ContentRoot, *AssetRel);
        UPackage* Package = CreatePackage(*PackageName);
        Array = NewObject<UTexture2DArray>(Package, *AssetRelName(AssetRel), RF_Public | RF_Standalone);
    }
    if (!Array)
    {
        return nullptr;
    }
    Array->PreEditChange(nullptr);
    Array->SourceTextures.Reset();
    for (UTexture2D* Slice : Slices)
    {
        if (Slice)
        {
            Array->SourceTextures.Add(Slice);
        }
    }
    if (bNormal)
    {
        Array->SRGB = false;
        Array->CompressionSettings = TC_Normalmap;
    }
    else
    {
        Array->SRGB = true;
        Array->CompressionSettings = TC_Default;
    }
    // Build the array source from the per-slice 2D textures (all slices must
    // share dimensions + format, which terrain atlas slices do).
    Array->UpdateSourceFromSourceTextures(true);
    Array->PostEditChange();
    if (!bUpdatingExistingArray)
    {
        FAssetRegistryModule::AssetCreated(Array);
    }
    Array->MarkPackageDirty();
    ImportedAssets.Add(Array->GetPathName());
    return Array;
}

UMaterialInterface* FWitcherImportContext::BuildTerrainBlendMaterial(
    const TSharedPtr<FJsonObject>& Terrain, const FString& AssetRel)
{
    const TArray<TSharedPtr<FJsonValue>>* Layers = nullptr;
    if (!Terrain->TryGetArrayField(TEXT("layers"), Layers) || Layers->Num() == 0)
    {
        return nullptr;
    }

    // Gather per-layer diffuse/normal textures in slice order.
    TArray<UTexture2D*> DiffuseSlices;
    TArray<UTexture2D*> NormalSlices;
    DiffuseSlices.SetNumZeroed(Layers->Num());
    NormalSlices.SetNumZeroed(Layers->Num());
    const int32 ParamCount = FMath::Max(32, Layers->Num());
    TArray<float> BlendSharpness;
    TArray<float> SlopeBaseDampening;
    TArray<float> SlopeNormalDampening;
    TArray<float> Falloff;
    TArray<float> Specularity;
    TArray<float> SpecularityBase;
    TArray<float> SpecularityScale;
    BlendSharpness.Init(0.1f, ParamCount);
    SlopeBaseDampening.Init(0.0f, ParamCount);
    SlopeNormalDampening.Init(0.5f, ParamCount);
    Falloff.Init(0.0f, ParamCount);
    Specularity.Init(0.0f, ParamCount);
    SpecularityBase.Init(0.5f, ParamCount);
    SpecularityScale.Init(0.0f, ParamCount);
    bool bAnyNormal = false;
    for (const TSharedPtr<FJsonValue>& Value : *Layers)
    {
        const TSharedPtr<FJsonObject> Layer = Value->AsObject();
        if (!Layer.IsValid())
        {
            continue;
        }
        const int32 Index = JsonInt(Layer, TEXT("index"), -1);
        if (Index < 0 || Index >= DiffuseSlices.Num())
        {
            continue;
        }
        DiffuseSlices[Index] = Cast<UTexture2D>(FindTexture(JsonString(Layer, TEXT("diffuse"))));
        UTexture2D* NormalTex = Cast<UTexture2D>(FindTexture(JsonString(Layer, TEXT("normal"))));
        NormalSlices[Index] = NormalTex;
        bAnyNormal = bAnyNormal || (NormalTex != nullptr);
        if (Index < ParamCount)
        {
            BlendSharpness[Index] = static_cast<float>(
                JsonNumber(Layer, TEXT("blend_sharpness"), BlendSharpness[Index]));
            SlopeBaseDampening[Index] = static_cast<float>(
                JsonNumber(Layer, TEXT("slope_base_dampening"), SlopeBaseDampening[Index]));
            SlopeNormalDampening[Index] = static_cast<float>(
                JsonNumber(Layer, TEXT("slope_normal_dampening"), SlopeNormalDampening[Index]));
            Falloff[Index] = static_cast<float>(
                JsonNumber(Layer, TEXT("falloff"), Falloff[Index]));
            Specularity[Index] = static_cast<float>(
                JsonNumber(Layer, TEXT("specularity"), Specularity[Index]));
            SpecularityBase[Index] = static_cast<float>(
                JsonNumber(Layer, TEXT("specularity_base"), SpecularityBase[Index]));
            SpecularityScale[Index] = static_cast<float>(
                JsonNumber(Layer, TEXT("specularity_scale"), SpecularityScale[Index]));
        }
    }
    const FString BlendSharpnessCode = HlslFloatArray(BlendSharpness);
    const FString SlopeBaseDampeningCode = HlslFloatArray(SlopeBaseDampening);
    const FString SlopeNormalDampeningCode = HlslFloatArray(SlopeNormalDampening);
    const FString FalloffCode = HlslFloatArray(Falloff);
    const FString SpecularityCode = HlslFloatArray(Specularity);
    const FString SpecularityBaseCode = HlslFloatArray(SpecularityBase);
    const FString SpecularityScaleCode = HlslFloatArray(SpecularityScale);

    // Texture arrays need every slice valid + uniform; fill gaps with the first
    // good slice so a single missing layer can't abort the whole material.
    UTexture2D* FallbackDiffuse = nullptr;
    for (UTexture2D* Slice : DiffuseSlices) { if (Slice) { FallbackDiffuse = Slice; break; } }
    if (!FallbackDiffuse)
    {
        AddWarning(TEXT("Terrain blend material: no diffuse layer textures resolved; using tint."));
        return nullptr;
    }
    for (UTexture2D*& Slice : DiffuseSlices) { if (!Slice) { Slice = FallbackDiffuse; } }

    UTexture2DArray* DiffuseArray = BuildTerrainTextureArray(DiffuseSlices, AssetRel + TEXT("_diffuse_array"), false);
    if (!DiffuseArray)
    {
        return nullptr;
    }
    UTexture2DArray* NormalArray = nullptr;
    if (bAnyNormal)
    {
        UTexture2D* FallbackNormal = nullptr;
        for (UTexture2D* Slice : NormalSlices) { if (Slice) { FallbackNormal = Slice; break; } }
        for (UTexture2D*& Slice : NormalSlices) { if (!Slice) { Slice = FallbackNormal; } }
        NormalArray = BuildTerrainTextureArray(NormalSlices, AssetRel + TEXT("_normal_array"), true);
    }
    const bool bHasNormalArray = NormalArray != nullptr;

    // Packed control map (RGBA8: overlay/bkgrnd/slope/uvScale indices) + tint.
    const FString ControlDepot = JsonString(Terrain, TEXT("control"));
    UTexture* ControlTex = ControlDepot.IsEmpty() ? nullptr : FindTexture(ControlDepot);
    UTexture* TintTex = FindTexture(JsonString(Terrain, TEXT("base_color_texture")));
    if (!ControlTex)
    {
        AddWarning(TEXT("Terrain blend material: control map missing; using tint."));
        return nullptr;
    }

    // Landscape world AABB so the control + tint maps span the terrain once,
    // locked to world space exactly like the heightmap.
    const TSharedPtr<FJsonObject>* TransformPtr = nullptr;
    Terrain->TryGetObjectField(TEXT("transform"), TransformPtr);
    const TSharedPtr<FJsonObject> TransformObj = TransformPtr ? *TransformPtr : nullptr;
    const FVector Location = JsonVector(TransformObj, TEXT("location"), FVector::ZeroVector);
    const float SizeCm = static_cast<float>(JsonNumber(Terrain, TEXT("terrain_size"), 1.0) * 100.0);

    UMaterial* Material = LoadExistingAsset<UMaterial>(ObjectPathFor(AssetRel));
    const bool bUpdatingExistingMaterial = Material != nullptr;
    if (bUpdatingExistingMaterial && !ShouldOverwrite(TEXT("materials_base")))
    {
        return Material;
    }
    if (!Material)
    {
        FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
        UMaterialFactoryNew* Factory = NewObject<UMaterialFactoryNew>();
        Material = Cast<UMaterial>(AssetToolsModule.Get().CreateAsset(
            AssetRelName(AssetRel), PackagePathFor(AssetRel), UMaterial::StaticClass(), Factory));
    }
    if (!Material)
    {
        return nullptr;
    }
    Material->PreEditChange(nullptr);
    if (bUpdatingExistingMaterial)
    {
        UMaterialEditingLibrary::DeleteAllMaterialExpressions(Material);
    }
    Material->bTangentSpaceNormal = false;

    auto NewExpr = [&](UClass* Cls, int32 X, int32 Y) -> UMaterialExpression*
    {
        return UMaterialEditingLibrary::CreateMaterialExpression(Material, Cls, X, Y);
    };

    // World XY -> uv01 = (worldXY - corner) / size, for the control + tint maps.
    UMaterialExpression* WorldPos = NewExpr(UMaterialExpressionWorldPosition::StaticClass(), -1700, 0);
    UMaterialExpressionComponentMask* WorldXY = Cast<UMaterialExpressionComponentMask>(
        NewExpr(UMaterialExpressionComponentMask::StaticClass(), -1500, 0));
    WorldXY->R = true; WorldXY->G = true; WorldXY->B = false; WorldXY->A = false;
    WorldXY->Input.Connect(0, WorldPos);
    UMaterialExpressionConstant2Vector* Corner = Cast<UMaterialExpressionConstant2Vector>(
        NewExpr(UMaterialExpressionConstant2Vector::StaticClass(), -1500, 250));
    Corner->R = static_cast<float>(Location.X);
    Corner->G = static_cast<float>(Location.Y);
    UMaterialExpressionSubtract* Centered = Cast<UMaterialExpressionSubtract>(
        NewExpr(UMaterialExpressionSubtract::StaticClass(), -1300, 0));
    Centered->A.Connect(0, WorldXY);
    Centered->B.Connect(0, Corner);
    UMaterialExpressionDivide* UV01 = Cast<UMaterialExpressionDivide>(
        NewExpr(UMaterialExpressionDivide::StaticClass(), -1100, 0));
    UV01->A.Connect(0, Centered);
    UV01->ConstB = SizeCm;

    UMaterialExpression* WorldPos2 = NewExpr(UMaterialExpressionWorldPosition::StaticClass(), -1100, 250);
    UMaterialExpression* SurfaceNormal = NewExpr(UMaterialExpressionVertexNormalWS::StaticClass(), -1100, 450);

    auto TexObj = [&](UTexture* Tex, int32 Y) -> UMaterialExpression*
    {
        UMaterialExpressionTextureObject* Obj = Cast<UMaterialExpressionTextureObject>(
            NewExpr(UMaterialExpressionTextureObject::StaticClass(), -800, Y));
        Obj->Texture = Tex;
        return Obj;
    };
    UMaterialExpression* DiffuseObj = TexObj(DiffuseArray, 600);
    UMaterialExpression* NormalObj = TexObj(bHasNormalArray ? (UTexture*)NormalArray : (UTexture*)DiffuseArray, 800);
    UMaterialExpression* ControlObj = TexObj(ControlTex, 1000);
    UMaterialExpression* TintObj = TexObj(TintTex ? TintTex : ControlTex, 1200);
    auto ScalarParam = [&](const TCHAR* Name, float DefaultValue, int32 Y) -> UMaterialExpression*
    {
        UMaterialExpressionScalarParameter* Param = Cast<UMaterialExpressionScalarParameter>(
            NewExpr(UMaterialExpressionScalarParameter::StaticClass(), -800, Y));
        Param->ParameterName = FName(Name);
        Param->DefaultValue = DefaultValue;
        return Param;
    };
    UMaterialExpression* DebugMode = ScalarParam(TEXT("TerrainDebugMode"), 0.0f, 1400);
    UMaterialExpression* NormalStrength = ScalarParam(TEXT("TerrainNormalStrength"), 1.0f, 1550);
    UMaterialExpression* TextureOffsetCmX = ScalarParam(TEXT("TerrainTextureOffsetCmX"), 0.0f, 1700);
    UMaterialExpression* TextureOffsetCmY = ScalarParam(TEXT("TerrainTextureOffsetCmY"), 0.0f, 1850);
    UMaterialExpression* ControlOffsetCmX = ScalarParam(TEXT("TerrainControlOffsetCmX"), 0.0f, 2000);
    UMaterialExpression* ControlOffsetCmY = ScalarParam(TEXT("TerrainControlOffsetCmY"), 0.0f, 2150);
    UMaterialExpressionConstant* TerrainSizeCm = Cast<UMaterialExpressionConstant>(
        NewExpr(UMaterialExpressionConstant::StaticClass(), -800, 2300));
    TerrainSizeCm->R = SizeCm;

    // Custom HLSL node implementing the W3 terrain material blend: decode the
    // packed control texel, sample the OVERLAY (flat top-down, fixed scale) and
    // BACKGROUND (triplanar, per-texel UV scale) atlas slices, blend by surface
    // slope, then apply the tint as a per-channel overlay/screen blend.
    UMaterialExpressionCustom* Custom = Cast<UMaterialExpressionCustom>(
        NewExpr(UMaterialExpressionCustom::StaticClass(), 0, 300));
    Custom->OutputType = CMOT_Float3;
    FCustomOutput NormalOut;
    NormalOut.OutputName = FName(TEXT("OutNormal"));
    NormalOut.OutputType = CMOT_Float3;
    Custom->AdditionalOutputs.Add(NormalOut);
    FCustomOutput RoughnessOut;
    RoughnessOut.OutputName = FName(TEXT("OutRoughness"));
    RoughnessOut.OutputType = CMOT_Float1;
    Custom->AdditionalOutputs.Add(RoughnessOut);
    FCustomOutput SpecularOut;
    SpecularOut.OutputName = FName(TEXT("OutSpecular"));
    SpecularOut.OutputType = CMOT_Float1;
    Custom->AdditionalOutputs.Add(SpecularOut);

    Custom->Inputs.Empty();
    auto AddInput = [&](const TCHAR* Name, UMaterialExpression* Src)
    {
        FCustomInput In;
        In.InputName = FName(Name);
        In.Input.Connect(0, Src);
        Custom->Inputs.Add(In);
    };
    AddInput(TEXT("WorldPos"), WorldPos2);
    AddInput(TEXT("VertexNormal"), SurfaceNormal);
    AddInput(TEXT("UV01"), UV01);
    AddInput(TEXT("DiffuseArray"), DiffuseObj);
    AddInput(TEXT("NormalArray"), NormalObj);
    AddInput(TEXT("ControlTex"), ControlObj);
    AddInput(TEXT("TintTex"), TintObj);
    AddInput(TEXT("DebugMode"), DebugMode);
    AddInput(TEXT("NormalStrength"), NormalStrength);
    AddInput(TEXT("TextureOffsetCmX"), TextureOffsetCmX);
    AddInput(TEXT("TextureOffsetCmY"), TextureOffsetCmY);
    AddInput(TEXT("ControlOffsetCmX"), ControlOffsetCmX);
    AddInput(TEXT("ControlOffsetCmY"), ControlOffsetCmY);
    AddInput(TEXT("TerrainSizeCm"), TerrainSizeCm);

    FString TerrainShaderCode = FString(R"HLSL(
// SampleLevel/Load only (no screen derivatives) so the node is valid in every
// shader stage, including ray/path-tracing closest-hit.
#define W3_NORMAL_X(RAW) ((RAW).r * 2.0 - 1.0)
#define W3_NORMAL_Y(RAW) ((1.0 - (RAW).g) * 2.0 - 1.0)
#define W3_DECODE_NORMAL(RAW) normalize(float3(W3_NORMAL_X(RAW), W3_NORMAL_Y(RAW), sqrt(saturate(1.0 - W3_NORMAL_X(RAW) * W3_NORMAL_X(RAW) - W3_NORMAL_Y(RAW) * W3_NORMAL_Y(RAW)))))

float3 wpM = WorldPos / 100.0;
float3 N = normalize(VertexNormal);
float3 up = float3(0.0, 0.0, 1.0);
float normalStrength = max(NormalStrength, 0.0);
float2 controlUV = UV01 + float2(ControlOffsetCmX, ControlOffsetCmY) / max(TerrainSizeCm, 1.0);

float scaleMap[8] = {0.333,0.166,0.05,0.025,0.0125,0.0075,0.00375,0.0};
float thrMap[8]   = {0.0,0.125,0.25,0.375,0.5,0.625,0.75,0.98};
float blendSharpMap[__LAYER_PARAM_COUNT__] = {__BLEND_SHARPNESS__};
float slopeBaseDampMap[__LAYER_PARAM_COUNT__] = {__SLOPE_BASE_DAMPENING__};
float slopeNormalDampMap[__LAYER_PARAM_COUNT__] = {__SLOPE_NORMAL_DAMPENING__};
float falloffMap[__LAYER_PARAM_COUNT__] = {__FALLOFF__};
float specularityMap[__LAYER_PARAM_COUNT__] = {__SPECULARITY__};
float specularityBaseMap[__LAYER_PARAM_COUNT__] = {__SPECULARITY_BASE__};
float specularityScaleMap[__LAYER_PARAM_COUNT__] = {__SPECULARITY_SCALE__};
float hasNormals = __HAS_NORMALS__;

// Atlas tiling is sampled in the W3 world frame. Keep the Y flip that matches
// current REDkit orientation tests.
float3 textureWorldM = wpM + float3(TextureOffsetCmX, TextureOffsetCmY, 0.0) / 100.0;
float3 texPos = float3(textureWorldM.x, -textureWorldM.y, textureWorldM.z);
float2 ovUV = texPos.xy * 0.333;   // OVERLAY: flat top-down, fixed scale.

// Triplanar weights are derived from the landscape normal. The 0.576 threshold
// comes from the reference terrain shader and tightens the blend around axes.
float3 an = abs(N);
float3 tw = max(an - 0.576, float3(0.0,0.0,0.0));
tw /= max(tw.x + tw.y + tw.z, 1e-4);

// 4-tap bilinear control interpolation. RED interpolates the chosen overlay,
// background, threshold, and material params, then makes one slope decision.
uint cw, ch;
ControlTex.GetDimensions(cw, ch);
float2 cdim = float2(cw, ch);
// RED converts terrain-space position to texel space as UV * resolution and
// floors it. Our packed map is flipped vertically to live in UE's Y-mirrored
// frame, so the matching continuous texel coordinate is X: UV*N, Y: UV*N-1.
float2 controlCpos = controlUV * cdim + float2(0.0, -1.0);
float2 cpos = controlCpos;
float2 cf = frac(cpos);
int2 c0 = int2(floor(cpos));
int2 cmax = int2((int)cw - 1, (int)ch - 1);

float wts[4];
wts[0] = (1.0-cf.x)*(1.0-cf.y);
wts[1] = cf.x*(1.0-cf.y);
wts[2] = (1.0-cf.x)*cf.y;
wts[3] = cf.x*cf.y;
int2 offs[4];
offs[0]=int2(0,0); offs[1]=int2(1,0); offs[2]=int2(0,1); offs[3]=int2(1,1);

float3 overlayDiff = float3(0,0,0);
float3 backgroundDiff = float3(0,0,0);
float3 overlayNorm = float3(0,0,0);
float3 backgroundNorm = float3(0,0,0);
float overlayRough = 0.0;
float backgroundRough = 0.0;
float overlaySpecularity = 0.0;
float backgroundSpecularity = 0.0;
float overlaySpecBase = 0.0;
float backgroundSpecBase = 0.0;
float overlaySpecScale = 0.0;
float backgroundSpecScale = 0.0;
float overlayFalloff = 0.0;
float backgroundFalloff = 0.0;
float slopeThreshold = 0.0;
float blendSharpness = 0.0;
float slopeBaseDampening = 0.0;
float slopeNormalDampening = 0.0;
float4 controlDebug = float4(0,0,0,0);

[unroll]
for (int i = 0; i < 4; i++)
{
    if (wts[i] < 1e-4) { continue; }
    int2 coord = clamp(c0 + offs[i], int2(0,0), cmax);
    float4 cc = ControlTex.Load(int3(coord, 0));
    int ov = min(max((int)round(cc.r * 255.0) - 1, 0), __LAYER_COUNT__ - 1);
    int bg = min(max((int)round(cc.g * 255.0) - 1, 0), __LAYER_COUNT__ - 1);
    int sl = min(max((int)round(cc.b * 255.0), 0), 7);
    int uvi = min(max((int)round(cc.a * 255.0), 0), 7);
    float w = wts[i];
    float bkScale = scaleMap[uvi];

    float3 ovD = DiffuseArray.SampleLevel(DiffuseArraySampler, float3(ovUV, (float)ov), 0.0).rgb;

    float2 uvTop = texPos.xy * bkScale;
    float2 uvSY  = texPos.xz * bkScale;
    float2 uvSX  = texPos.yz * bkScale;
    float3 bgD = tw.z * DiffuseArray.SampleLevel(DiffuseArraySampler, float3(uvTop, (float)bg), 0.0).rgb
               + tw.y * DiffuseArray.SampleLevel(DiffuseArraySampler, float3(uvSY,  (float)bg), 0.0).rgb
               + tw.x * DiffuseArray.SampleLevel(DiffuseArraySampler, float3(uvSX,  (float)bg), 0.0).rgb;

    float3 ovNw = up;
    float3 bgNw = up;
    float ovR = 0.9;
    float bgR = 0.9;
    if (hasNormals > 0.5)
    {
        float4 ovRaw = NormalArray.SampleLevel(NormalArraySampler, float3(ovUV, (float)ov), 0.0);
        float3 ovNt = W3_DECODE_NORMAL(ovRaw);
        ovNw = normalize(float3(ovNt.x, ovNt.y, max(ovNt.z, 1e-3)));
        ovR = ovRaw.a;

        float4 rawTop = NormalArray.SampleLevel(NormalArraySampler, float3(uvTop, (float)bg), 0.0);
        float4 rawSY  = NormalArray.SampleLevel(NormalArraySampler, float3(uvSY,  (float)bg), 0.0);
        float4 rawSX  = NormalArray.SampleLevel(NormalArraySampler, float3(uvSX,  (float)bg), 0.0);
        float3 nTop = W3_DECODE_NORMAL(rawTop);
        float3 nSY  = W3_DECODE_NORMAL(rawSY);
        float3 nSX  = W3_DECODE_NORMAL(rawSX);
        float3 bgTopN = normalize(float3(nTop.x, nTop.y, max(nTop.z, 1e-3)));
        float3 bgSYN  = normalize(float3(nSY.x,  max(nSY.z, 1e-3), nSY.y));
        float3 bgSXN  = normalize(float3(max(nSX.z, 1e-3), nSX.x,  nSX.y));
        bgNw = normalize(tw.z * bgTopN + tw.y * bgSYN + tw.x * bgSXN);
        bgR = tw.z * rawTop.a + tw.y * rawSY.a + tw.x * rawSX.a;
    }

    overlayDiff += w * ovD;
    backgroundDiff += w * bgD;
    overlayNorm += w * ovNw;
    backgroundNorm += w * bgNw;
    overlayRough += w * ovR;
    backgroundRough += w * bgR;
    overlaySpecularity += w * specularityMap[ov];
    backgroundSpecularity += w * specularityMap[bg];
    overlaySpecBase += w * specularityBaseMap[ov];
    backgroundSpecBase += w * specularityBaseMap[bg];
    overlaySpecScale += w * specularityScaleMap[ov];
    backgroundSpecScale += w * specularityScaleMap[bg];
    overlayFalloff += w * falloffMap[ov];
    backgroundFalloff += w * falloffMap[bg];
    slopeThreshold += w * thrMap[sl];
    blendSharpness += w * blendSharpMap[ov];
    slopeBaseDampening += w * slopeBaseDampMap[bg];
    slopeNormalDampening += w * slopeNormalDampMap[bg];
    controlDebug += w * float4(((float)ov + 1.0) / 31.0, ((float)bg + 1.0) / 31.0, (float)sl / 7.0, (float)uvi / 7.0);
}

overlayNorm = normalize(overlayNorm);
backgroundNorm = normalize(backgroundNorm);
float3 slopeBackgroundNorm = backgroundNorm;
float3 overlayNormDebug = overlayNorm;
float3 backgroundNormDebug = backgroundNorm;
overlayNorm = normalize(float3(overlayNorm.xy * normalStrength, max(overlayNorm.z, 1e-3)));
float3 finalBackgroundNorm = normalize(float3(backgroundNorm.xy * normalStrength, max(backgroundNorm.z, 1e-3)));
finalBackgroundNorm = normalize(lerp(up, finalBackgroundNorm, saturate(slopeNormalDampening)));

// Reference-style slope blend: bias the background normal toward up on flatter
// terrain, then use tan(slope) against the bilinearly interpolated threshold.
float vertexFlatness = saturate(dot(N, up));
float3 flattenedBackground = lerp(slopeBackgroundNorm, up, vertexFlatness);
float3 biasedBackground = normalize(lerp(slopeBackgroundNorm, flattenedBackground, saturate(slopeBaseDampening)));
float slopeValue = saturate((abs(biasedBackground.x) + abs(biasedBackground.y)) / max(abs(biasedBackground.z), 1e-3));
float surfaceSlopeBlend = saturate((slopeValue - slopeThreshold) / max(blendSharpness, 1e-3));

float3 diff = lerp(overlayDiff, backgroundDiff, surfaceSlopeBlend);
float3 worldNormal = normalize(lerp(overlayNorm, finalBackgroundNorm, surfaceSlopeBlend));
float roughness = saturate(lerp(overlayRough, backgroundRough, surfaceSlopeBlend));
float specularity = lerp(overlaySpecularity, backgroundSpecularity, surfaceSlopeBlend);
float specBase = lerp(overlaySpecBase, backgroundSpecBase, surfaceSlopeBlend);
float specScale = lerp(overlaySpecScale, backgroundSpecScale, surfaceSlopeBlend);
float falloff = lerp(overlayFalloff, backgroundFalloff, surfaceSlopeBlend);
float specular = saturate(specularity);
if (hasNormals < 0.5)
{
    worldNormal = N;
    roughness = saturate(specBase + specScale);
}
OutNormal = worldNormal;
OutRoughness = roughness;
OutSpecular = specular;

// Tint: per-channel overlay/screen blend (gives the large-scale colour).
float2 colorUV = saturate((controlCpos + 0.5) / max(cdim, float2(1.0, 1.0)));
float3 tint = TintTex.SampleLevel(TintTexSampler, colorUV, 0.0).rgb;
float3 darken = 2.0 * tint * diff;
float3 screen = 1.0 - 2.0 * (1.0 - tint) * (1.0 - diff);
float3 tinted = lerp(darken, screen, step(0.5, tint));

int dbg = (int)floor(DebugMode + 0.5);
if (dbg == 1) { return overlayDiff; }
if (dbg == 2) { return backgroundDiff; }
if (dbg == 3) { return float3(surfaceSlopeBlend, surfaceSlopeBlend, surfaceSlopeBlend); }
if (dbg == 4) { return controlDebug.rgb; }
if (dbg == 5) { return worldNormal * 0.5 + 0.5; }
if (dbg == 6) { return tint; }
if (dbg == 7) { return float3(slopeThreshold, blendSharpness, slopeBaseDampening); }
if (dbg == 8) { return float3(frac(ovUV.x), frac(ovUV.y), 0.0); }
if (dbg == 9) { return float3(frac(controlCpos.x), frac(controlCpos.y), 0.0); }
if (dbg == 10) { return overlayNormDebug * 0.5 + 0.5; }
if (dbg == 11) { return backgroundNormDebug * 0.5 + 0.5; }
if (dbg == 12) { return float3(hasNormals, saturate(normalStrength / 4.0), slopeNormalDampening); }
if (dbg == 13) { return float3(roughness, specular, saturate(falloff)); }
return tinted;
)HLSL");
    TerrainShaderCode.ReplaceInline(TEXT("__LAYER_COUNT__"), *FString::FromInt(FMath::Max(1, Layers->Num())));
    TerrainShaderCode.ReplaceInline(TEXT("__LAYER_PARAM_COUNT__"), *FString::FromInt(ParamCount));
    TerrainShaderCode.ReplaceInline(TEXT("__BLEND_SHARPNESS__"), *BlendSharpnessCode);
    TerrainShaderCode.ReplaceInline(TEXT("__SLOPE_BASE_DAMPENING__"), *SlopeBaseDampeningCode);
    TerrainShaderCode.ReplaceInline(TEXT("__SLOPE_NORMAL_DAMPENING__"), *SlopeNormalDampeningCode);
    TerrainShaderCode.ReplaceInline(TEXT("__FALLOFF__"), *FalloffCode);
    TerrainShaderCode.ReplaceInline(TEXT("__SPECULARITY__"), *SpecularityCode);
    TerrainShaderCode.ReplaceInline(TEXT("__SPECULARITY_BASE__"), *SpecularityBaseCode);
    TerrainShaderCode.ReplaceInline(TEXT("__SPECULARITY_SCALE__"), *SpecularityScaleCode);
    TerrainShaderCode.ReplaceInline(TEXT("__HAS_NORMALS__"), bHasNormalArray ? TEXT("1.0") : TEXT("0.0"));
    Custom->Code = TerrainShaderCode;

    // Rebuild explicitly so the named outputs exist before we connect them.
    Custom->RebuildOutputs();

    auto ConnectCustomOutput = [&](EMaterialProperty Property, int32 OutputIndex)
    {
        if (FExpressionInput* Input = Material->GetExpressionInputForProperty(Property))
        {
            Input->Connect(OutputIndex, Custom);
        }
    };
    ConnectCustomOutput(MP_BaseColor, 0);
    ConnectCustomOutput(MP_Normal, 1);
    ConnectCustomOutput(MP_Roughness, 2);
    ConnectCustomOutput(MP_Specular, 3);

    Material->PostEditChange();
    Material->MarkPackageDirty();
    UMaterialEditingLibrary::RecompileMaterial(Material);
    return Material;
}

void FWitcherImportContext::ImportTerrain()
{
    const TSharedPtr<FJsonObject>* TerrainPtr = nullptr;
    if (!Manifest->TryGetObjectField(TEXT("terrain"), TerrainPtr) || !TerrainPtr)
    {
        return;
    }
    const TSharedPtr<FJsonObject> Terrain = *TerrainPtr;

    UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
    if (!World)
    {
        AddError(TEXT("Terrain import: no editor world available."));
        return;
    }

    // --- heightmap (raw little-endian R16) ---
    const FString R16Path = ResolveBundleFile(JsonString(Terrain, TEXT("heightmap_r16")));
    TArray<uint8> RawBytes;
    if (!FFileHelper::LoadFileToArray(RawBytes, *R16Path))
    {
        AddError(FString::Printf(TEXT("Terrain heightmap R16 not found: %s"), *R16Path));
        return;
    }
    const int32 Resolution = JsonInt(Terrain, TEXT("resolution"));
    const int32 ExpectedSamples = Resolution * Resolution;
    if (Resolution <= 1 || RawBytes.Num() < ExpectedSamples * 2)
    {
        AddError(FString::Printf(TEXT("Terrain heightmap size mismatch: resolution=%d bytes=%d"),
            Resolution, RawBytes.Num()));
        return;
    }
    TArray<uint16> Heights;
    Heights.SetNumUninitialized(ExpectedSamples);
    FMemory::Memcpy(Heights.GetData(), RawBytes.GetData(), ExpectedSamples * sizeof(uint16));

    const int32 MinX = JsonInt(Terrain, TEXT("min_x"), 0);
    const int32 MinY = JsonInt(Terrain, TEXT("min_y"), 0);
    const int32 MaxX = JsonInt(Terrain, TEXT("max_x"), Resolution - 1);
    const int32 MaxY = JsonInt(Terrain, TEXT("max_y"), Resolution - 1);
    const int32 NumSubsections = JsonInt(Terrain, TEXT("num_subsections"), 1);
    const int32 SubsectionSizeQuads = JsonInt(Terrain, TEXT("subsection_size_quads"), 63);

    // --- transform (centimetres, from the W3->UE convention in terrain_unreal.py) ---
    const TSharedPtr<FJsonObject>* TransformPtr = nullptr;
    Terrain->TryGetObjectField(TEXT("transform"), TransformPtr);
    const TSharedPtr<FJsonObject> TransformObj = TransformPtr ? *TransformPtr : nullptr;
    const FVector Location = JsonVector(TransformObj, TEXT("location"), FVector::ZeroVector);
    const FVector Scale = JsonVector(TransformObj, TEXT("scale"), FVector(100.0, 100.0, 100.0));

    // --- landscape material (tint base colour; Phase 4 swaps in weight blends) ---
    const FString TerrainAssetRel = JsonString(Terrain, TEXT("asset_path"), TEXT("witcher_terrain"));
    UMaterialInterface* TerrainMaterial = nullptr;
    {
        const FString MaterialRel = TerrainAssetRel + TEXT("_terrain_m");
        const TArray<TSharedPtr<FJsonValue>>* Layers = nullptr;
        if (Terrain->TryGetArrayField(TEXT("layers"), Layers) && Layers->Num() > 0)
        {
            TerrainMaterial = BuildTerrainBlendMaterial(Terrain, MaterialRel);
        }
        if (!TerrainMaterial)
        {
            TerrainMaterial = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(MaterialRel));
        }
        if (!TerrainMaterial)
        {
            UTexture* TintTexture = FindTexture(JsonString(Terrain, TEXT("base_color_texture")));
            TerrainMaterial = CreateSimpleMaterial(
                PackagePathFor(MaterialRel), AssetRelName(MaterialRel),
                TintTexture, FLinearColor(0.18f, 0.22f, 0.12f), /*bTranslucent=*/false);
        }
        if (TerrainMaterial)
        {
            ImportedAssets.Add(TerrainMaterial->GetPathName());
        }
    }

    // --- spawn + import the landscape ---
    FActorSpawnParameters SpawnParams;
    SpawnParams.ObjectFlags = RF_Transactional;
    ALandscape* Landscape = World->SpawnActor<ALandscape>();
    if (!Landscape)
    {
        AddError(TEXT("Terrain import: failed to spawn ALandscape."));
        return;
    }
    Landscape->SetActorTransform(FTransform(FQuat::Identity, Location, Scale));
    if (TerrainMaterial)
    {
        Landscape->LandscapeMaterial = TerrainMaterial;
    }

    TMap<FGuid, TArray<uint16>> HeightDataPerLayer;
    HeightDataPerLayer.Add(FGuid(), MoveTemp(Heights));
    TMap<FGuid, TArray<FLandscapeImportLayerInfo>> MaterialLayerDataPerLayer;
    MaterialLayerDataPerLayer.Add(FGuid(), TArray<FLandscapeImportLayerInfo>());

    Landscape->Import(
        FGuid::NewGuid(), MinX, MinY, MaxX, MaxY,
        NumSubsections, SubsectionSizeQuads,
        HeightDataPerLayer, nullptr,
        MaterialLayerDataPerLayer, ELandscapeImportAlphamapType::Additive,
        MakeArrayView(static_cast<const FLandscapeLayer*>(nullptr), 0));

    if (ULandscapeInfo* LandscapeInfo = Landscape->GetLandscapeInfo())
    {
        LandscapeInfo->UpdateLayerInfoMap(Landscape);
    }
    Landscape->PostEditChange();
    Landscape->SetActorLabel(JsonString(Terrain, TEXT("name"), TEXT("WitcherTerrain")));
    ImportedAssets.Add(Landscape->GetPathName());

    // --- world water plane (W3 water sits at world Z=0) ---
    const TSharedPtr<FJsonObject>* WaterPtr = nullptr;
    if (Terrain->TryGetObjectField(TEXT("water"), WaterPtr) && WaterPtr)
    {
        const TSharedPtr<FJsonObject> Water = *WaterPtr;
        UStaticMesh* PlaneMesh = LoadExistingAsset<UStaticMesh>(TEXT("/Engine/BasicShapes/Plane.Plane"));
        if (PlaneMesh)
        {
            const double WaterZ = JsonNumber(Water, TEXT("z"), 0.0);
            const double SizeCm = JsonNumber(Water, TEXT("size_cm"), (MaxX - MinX) * Scale.X);
            AStaticMeshActor* WaterActor = World->SpawnActor<AStaticMeshActor>(
                FVector(0.0, 0.0, WaterZ), FRotator::ZeroRotator);
            if (WaterActor && WaterActor->GetStaticMeshComponent())
            {
                UStaticMeshComponent* WaterComponent = WaterActor->GetStaticMeshComponent();
                WaterComponent->SetMobility(EComponentMobility::Movable);
                WaterComponent->SetStaticMesh(PlaneMesh);
                // The engine plane is 100 uu (1 m) square, so scale = size in metres.
                const double PlaneScale = SizeCm / 100.0;
                WaterActor->SetActorScale3D(FVector(PlaneScale, PlaneScale, 1.0));

                const FString WaterMaterialRel = TerrainAssetRel + TEXT("_water_m");
                UMaterialInterface* WaterMaterial = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(WaterMaterialRel));
                if (!WaterMaterial)
                {
                    WaterMaterial = CreateSimpleMaterial(
                        PackagePathFor(WaterMaterialRel), AssetRelName(WaterMaterialRel),
                        nullptr, FLinearColor(0.02f, 0.16f, 0.24f), /*bTranslucent=*/true);
                    if (WaterMaterial)
                    {
                        ImportedAssets.Add(WaterMaterial->GetPathName());
                    }
                }
                if (WaterMaterial)
                {
                    WaterComponent->SetMaterial(0, WaterMaterial);
                }
                WaterActor->SetActorLabel(JsonString(Terrain, TEXT("name"), TEXT("WitcherTerrain")) + TEXT("_Water"));
                ImportedAssets.Add(WaterActor->GetPathName());
            }
        }
        else
        {
            AddWarning(TEXT("Water plane skipped: /Engine/BasicShapes/Plane not found."));
        }
    }
}

UStaticMesh* FWitcherImportContext::FindPlacementMesh(const FString& AssetRel)
{
    UStaticMesh* Mesh = Cast<UStaticMesh>(MeshesByAssetRel.FindRef(AssetRel).Get());
    if (!Mesh)
    {
        Mesh = LoadExistingAsset<UStaticMesh>(ObjectPathFor(AssetRel));
    }
    return Mesh;
}

void FWitcherImportContext::ImportPlacements()
{
    const TSharedPtr<FJsonObject>* PlacementsPtr = nullptr;
    if (!Manifest->TryGetObjectField(TEXT("placements"), PlacementsPtr) || !PlacementsPtr)
    {
        return;
    }
    const TArray<TSharedPtr<FJsonValue>>* Layers = nullptr;
    if (!(*PlacementsPtr)->TryGetArrayField(TEXT("layers"), Layers))
    {
        return;
    }

    UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
    if (!World)
    {
        AddError(TEXT("Placement import: no editor world available."));
        return;
    }

    for (const TSharedPtr<FJsonValue>& LayerValue : *Layers)
    {
        const TSharedPtr<FJsonObject> Layer = LayerValue->AsObject();
        if (!Layer.IsValid())
        {
            continue;
        }
        const FString LayerId = JsonString(Layer, TEXT("layer_id"), TEXT("placements"));
        const FString Folder = JsonString(Layer, TEXT("folder"));

        bool bLayerMeshesReady = true;
        auto ValidateRequiredMesh = [&](const TSharedPtr<FJsonObject>& Entry, const TCHAR* Kind) -> bool
        {
            if (!Entry.IsValid())
            {
                return true;
            }
            const FString AssetRel = JsonString(Entry, TEXT("asset_path"));
            const FString EntryName = JsonString(Entry, TEXT("name"), FString(Kind));
            if (AssetRel.IsEmpty())
            {
                AddWarning(FString::Printf(
                    TEXT("Placement %s '%s' has no mesh asset path (layer '%s'); layer left unchanged."),
                    Kind,
                    *EntryName,
                    *LayerId));
                return false;
            }
            if (!FindPlacementMesh(AssetRel))
            {
                AddWarning(FString::Printf(
                    TEXT("Placement %s '%s' references missing mesh '%s' (layer '%s'); layer left unchanged."),
                    Kind,
                    *EntryName,
                    *AssetRel,
                    *LayerId));
                return false;
            }
            return true;
        };

        const TArray<TSharedPtr<FJsonValue>>* PreflightActors = nullptr;
        if (Layer->TryGetArrayField(TEXT("actors"), PreflightActors))
        {
            for (const TSharedPtr<FJsonValue>& ActorValue : *PreflightActors)
            {
                bLayerMeshesReady = ValidateRequiredMesh(ActorValue->AsObject(), TEXT("actor")) && bLayerMeshesReady;
            }
        }
        const TArray<TSharedPtr<FJsonValue>>* PreflightInstancers = nullptr;
        if (Layer->TryGetArrayField(TEXT("instancers"), PreflightInstancers))
        {
            for (const TSharedPtr<FJsonValue>& InstancerValue : *PreflightInstancers)
            {
                bLayerMeshesReady = ValidateRequiredMesh(InstancerValue->AsObject(), TEXT("instancer")) && bLayerMeshesReady;
            }
        }
        if (!bLayerMeshesReady)
        {
            continue;
        }

        // Tag every actor from this layer so a re-send cleanly replaces them
        // (incremental map fill: new layers add actors, re-sent layers never
        // duplicate).
        const FName LayerTag(*(FString(TEXT("WitcherLayer:")) + LayerId));
        for (TActorIterator<AActor> It(World); It; ++It)
        {
            if (It->Tags.Contains(LayerTag))
            {
                World->DestroyActor(*It);
            }
        }

        auto PlaceActorCommon = [&](AActor* Actor, const FString& ActorName)
        {
            if (!Actor)
            {
                return;
            }
            Actor->Tags.Add(LayerTag);
            Actor->SetActorLabel(ActorName);
            if (!Folder.IsEmpty())
            {
                Actor->SetFolderPath(FName(*Folder));
            }
            ImportedAssets.Add(Actor->GetPathName());
        };

        auto ConfigureVisualPlacement = [](UPrimitiveComponent* Component)
        {
            if (!Component)
            {
                return;
            }
            Component->SetCollisionEnabled(ECollisionEnabled::NoCollision);
        };

        auto ConfigureHiddenCollision = [](UPrimitiveComponent* Component)
        {
            if (!Component)
            {
                return;
            }
            Component->SetVisibility(false, true);
            Component->SetHiddenInGame(true, true);
            Component->SetCastShadow(false);
            Component->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
            Component->SetCollisionProfileName(FName(TEXT("BlockAll")));
        };

        auto ConfigureLightCommon = [](ULightComponent* Component, const TSharedPtr<FJsonObject>& LightEntry)
        {
            if (!Component || !LightEntry.IsValid())
            {
                return;
            }
            Component->SetMobility(EComponentMobility::Movable);
            Component->SetIntensity(static_cast<float>(JsonNumber(LightEntry, TEXT("intensity"), 0.0)));
            Component->SetLightColor(JsonColor(LightEntry, TEXT("color")));
        };

        auto ConfigurePointLight = [&](UPointLightComponent* Component, const TSharedPtr<FJsonObject>& LightEntry)
        {
            ConfigureLightCommon(Component, LightEntry);
            if (!Component || !LightEntry.IsValid())
            {
                return;
            }
            const float AttenuationRadius = static_cast<float>(JsonNumber(LightEntry, TEXT("attenuation_radius"), 0.0));
            if (AttenuationRadius > 0.0f)
            {
                Component->SetAttenuationRadius(AttenuationRadius);
            }
            const float SourceRadius = static_cast<float>(JsonNumber(LightEntry, TEXT("source_radius"), 0.0));
            if (SourceRadius > 0.0f)
            {
                Component->SetSourceRadius(SourceRadius);
            }
        };

        int32 ActorCount = 0;
        int32 CollisionActorCount = 0;
        const TArray<TSharedPtr<FJsonValue>>* Actors = nullptr;
        if (Layer->TryGetArrayField(TEXT("actors"), Actors))
        {
            for (const TSharedPtr<FJsonValue>& ActorValue : *Actors)
            {
                const TSharedPtr<FJsonObject> ActorEntry = ActorValue->AsObject();
                if (!ActorEntry.IsValid())
                {
                    continue;
                }
                const FString AssetRel = JsonString(ActorEntry, TEXT("asset_path"));
                UStaticMesh* Mesh = FindPlacementMesh(AssetRel);
                if (!Mesh)
                {
                    AddWarning(FString::Printf(TEXT("Placement mesh not found for '%s' (layer '%s')."), *AssetRel, *LayerId));
                    continue;
                }
                const TSharedPtr<FJsonObject>* TransformPtr = nullptr;
                ActorEntry->TryGetObjectField(TEXT("transform"), TransformPtr);
                const TSharedPtr<FJsonObject> Transform = TransformPtr ? *TransformPtr : nullptr;
                const FVector Location = JsonVector(Transform, TEXT("location"), FVector::ZeroVector);
                const FQuat Rotation = JsonQuat(Transform, TEXT("rotation"));
                const FVector ScaleVec = JsonVector(Transform, TEXT("scale"), FVector::OneVector);

                AStaticMeshActor* MeshActor = World->SpawnActor<AStaticMeshActor>();
                if (!MeshActor)
                {
                    continue;
                }
                if (UStaticMeshComponent* Component = MeshActor->GetStaticMeshComponent())
                {
                    Component->SetMobility(EComponentMobility::Movable);
                    Component->SetStaticMesh(Mesh);
                    ConfigureVisualPlacement(Component);
                }
                MeshActor->SetActorTransform(FTransform(Rotation, Location, ScaleVec));
                const FString ActorName = JsonString(ActorEntry, TEXT("name"), TEXT("Placement"));
                PlaceActorCommon(MeshActor, ActorName);
                ++ActorCount;

                const FString CollisionAssetRel = JsonString(ActorEntry, TEXT("collision_asset_path"));
                if (!CollisionAssetRel.IsEmpty())
                {
                    UStaticMesh* CollisionMesh = FindPlacementMesh(CollisionAssetRel);
                    if (!CollisionMesh)
                    {
                        AddWarning(FString::Printf(
                            TEXT("Placement collision mesh not found for '%s' (layer '%s')."),
                            *CollisionAssetRel,
                            *LayerId));
                        continue;
                    }
                    AStaticMeshActor* CollisionActor = World->SpawnActor<AStaticMeshActor>();
                    if (!CollisionActor)
                    {
                        continue;
                    }
                    if (UStaticMeshComponent* CollisionComponent = CollisionActor->GetStaticMeshComponent())
                    {
                        CollisionComponent->SetMobility(EComponentMobility::Movable);
                        CollisionComponent->SetStaticMesh(CollisionMesh);
                        ConfigureHiddenCollision(CollisionComponent);
                    }
                    CollisionActor->SetActorTransform(FTransform(Rotation, Location, ScaleVec));
                    CollisionActor->SetActorHiddenInGame(true);
                    PlaceActorCommon(CollisionActor, ActorName + TEXT("_Collision"));
                    ++CollisionActorCount;
                }
            }
        }

        // Sector instancers (dense CSectorData layouts) -> one HISM actor each;
        // their individual placements are not separate Blender objects.
        int32 InstanceCount = 0;
        const TArray<TSharedPtr<FJsonValue>>* Instancers = nullptr;
        if (Layer->TryGetArrayField(TEXT("instancers"), Instancers))
        {
            for (const TSharedPtr<FJsonValue>& InstancerValue : *Instancers)
            {
                const TSharedPtr<FJsonObject> InstancerEntry = InstancerValue->AsObject();
                if (!InstancerEntry.IsValid())
                {
                    continue;
                }
                const FString AssetRel = JsonString(InstancerEntry, TEXT("asset_path"));
                UStaticMesh* Mesh = FindPlacementMesh(AssetRel);
                if (!Mesh)
                {
                    AddWarning(FString::Printf(TEXT("Instancer mesh not found for '%s' (layer '%s')."), *AssetRel, *LayerId));
                    continue;
                }
                const TArray<TSharedPtr<FJsonValue>>* Instances = nullptr;
                if (!InstancerEntry->TryGetArrayField(TEXT("instances"), Instances) || Instances->Num() == 0)
                {
                    continue;
                }
                UStaticMesh* CollisionMesh = nullptr;
                const FString CollisionAssetRel = JsonString(InstancerEntry, TEXT("collision_asset_path"));
                if (!CollisionAssetRel.IsEmpty())
                {
                    CollisionMesh = FindPlacementMesh(CollisionAssetRel);
                    if (!CollisionMesh)
                    {
                        AddWarning(FString::Printf(
                            TEXT("Instancer collision mesh not found for '%s' (layer '%s')."),
                            *CollisionAssetRel,
                            *LayerId));
                    }
                }

                AActor* Container = World->SpawnActor<AActor>();
                if (!Container)
                {
                    continue;
                }
                USceneComponent* Root = NewObject<USceneComponent>(Container, TEXT("Root"));
                Container->SetRootComponent(Root);
                Root->SetMobility(EComponentMobility::Movable);
                Container->AddInstanceComponent(Root);
                Root->RegisterComponent();
                UHierarchicalInstancedStaticMeshComponent* Hism =
                    NewObject<UHierarchicalInstancedStaticMeshComponent>(Container);
                Hism->SetupAttachment(Root);
                Hism->SetMobility(EComponentMobility::Movable);
                Hism->SetStaticMesh(Mesh);
                ConfigureVisualPlacement(Hism);
                Container->AddInstanceComponent(Hism);
                Hism->RegisterComponent();

                AActor* CollisionContainer = nullptr;
                UHierarchicalInstancedStaticMeshComponent* CollisionHism = nullptr;
                if (CollisionMesh)
                {
                    CollisionContainer = World->SpawnActor<AActor>();
                    if (CollisionContainer)
                    {
                        USceneComponent* CollisionRoot = NewObject<USceneComponent>(CollisionContainer, TEXT("Root"));
                        CollisionContainer->SetRootComponent(CollisionRoot);
                        CollisionRoot->SetMobility(EComponentMobility::Movable);
                        CollisionContainer->AddInstanceComponent(CollisionRoot);
                        CollisionRoot->RegisterComponent();

                        CollisionHism = NewObject<UHierarchicalInstancedStaticMeshComponent>(CollisionContainer);
                        CollisionHism->SetupAttachment(CollisionRoot);
                        CollisionHism->SetMobility(EComponentMobility::Movable);
                        CollisionHism->SetStaticMesh(CollisionMesh);
                        ConfigureHiddenCollision(CollisionHism);
                        CollisionContainer->AddInstanceComponent(CollisionHism);
                        CollisionHism->RegisterComponent();
                        CollisionContainer->SetActorHiddenInGame(true);
                    }
                }

                for (const TSharedPtr<FJsonValue>& InstanceValue : *Instances)
                {
                    const TSharedPtr<FJsonObject> Instance = InstanceValue->AsObject();
                    if (!Instance.IsValid())
                    {
                        continue;
                    }
                    const FVector Location = JsonVector(Instance, TEXT("location"), FVector::ZeroVector);
                    const FQuat Rotation = JsonQuat(Instance, TEXT("rotation"));
                    const FVector ScaleVec = JsonVector(Instance, TEXT("scale"), FVector::OneVector);
                    const FTransform InstanceTransform(Rotation, Location, ScaleVec);
                    Hism->AddInstance(InstanceTransform, /*bWorldSpace=*/true);
                    if (CollisionHism)
                    {
                        CollisionHism->AddInstance(InstanceTransform, /*bWorldSpace=*/true);
                    }
                    ++InstanceCount;
                }
                const FString InstancerName = JsonString(InstancerEntry, TEXT("name"), TEXT("Instancer"));
                PlaceActorCommon(Container, InstancerName);
                if (CollisionContainer)
                {
                    PlaceActorCommon(CollisionContainer, InstancerName + TEXT("_Collision"));
                    ++CollisionActorCount;
                }
            }
        }

        int32 LightCount = 0;
        const TArray<TSharedPtr<FJsonValue>>* Lights = nullptr;
        if (Layer->TryGetArrayField(TEXT("lights"), Lights))
        {
            for (const TSharedPtr<FJsonValue>& LightValue : *Lights)
            {
                const TSharedPtr<FJsonObject> LightEntry = LightValue->AsObject();
                if (!LightEntry.IsValid())
                {
                    continue;
                }
                const TSharedPtr<FJsonObject>* TransformPtr = nullptr;
                LightEntry->TryGetObjectField(TEXT("transform"), TransformPtr);
                const TSharedPtr<FJsonObject> Transform = TransformPtr ? *TransformPtr : nullptr;
                const FVector Location = JsonVector(Transform, TEXT("location"), FVector::ZeroVector);
                const FString LightType = JsonString(LightEntry, TEXT("type"), TEXT("point")).ToLower();
                const FString LightName = JsonString(LightEntry, TEXT("name"), LightType == TEXT("spot") ? TEXT("SpotLight") : TEXT("PointLight"));

                if (LightType == TEXT("spot"))
                {
                    ASpotLight* SpotActor = World->SpawnActor<ASpotLight>();
                    if (!SpotActor)
                    {
                        continue;
                    }
                    FVector Direction = JsonVector(LightEntry, TEXT("direction"), FVector::ForwardVector);
                    if (!Direction.Normalize())
                    {
                        Direction = FVector::ForwardVector;
                    }
                    SpotActor->SetActorLocation(Location);
                    SpotActor->SetActorRotation(Direction.Rotation());
                    if (USpotLightComponent* Component = SpotActor->FindComponentByClass<USpotLightComponent>())
                    {
                        ConfigurePointLight(Component, LightEntry);
                        const float OuterCone = static_cast<float>(JsonNumber(LightEntry, TEXT("outer_cone_angle"), 44.0));
                        const float InnerCone = static_cast<float>(JsonNumber(LightEntry, TEXT("inner_cone_angle"), 0.0));
                        Component->SetOuterConeAngle(FMath::Max(0.0f, OuterCone));
                        Component->SetInnerConeAngle(FMath::Clamp(InnerCone, 0.0f, FMath::Max(0.0f, OuterCone)));
                    }
                    PlaceActorCommon(SpotActor, LightName);
                    ++LightCount;
                }
                else
                {
                    APointLight* PointActor = World->SpawnActor<APointLight>();
                    if (!PointActor)
                    {
                        continue;
                    }
                    PointActor->SetActorLocation(Location);
                    if (UPointLightComponent* Component = PointActor->FindComponentByClass<UPointLightComponent>())
                    {
                        ConfigurePointLight(Component, LightEntry);
                    }
                    PlaceActorCommon(PointActor, LightName);
                    ++LightCount;
                }
            }
        }

        UE_LOG(LogWitcherImportContext, Log,
            TEXT("Layer '%s': %d actor(s), %d collision actor(s), %d instanced placement(s), %d light(s)"),
            *LayerId,
            ActorCount,
            CollisionActorCount,
            InstanceCount,
            LightCount);
    }
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

FString FWitcherImportContext::ClassSafeAssetRel(const FString& AssetRel, UClass* DesiredClass, const FString& Suffix)
{
    // REDkit folders can hold e.g. foo.w2mi next to foo.xbm; without
    // extensions both map to the same Unreal object path, and replacing an
    // object with a different class is fatal. Shift the newcomer to a
    // suffixed sibling instead.
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
            ConsiderDirty(StaticLoadObject(UObject::StaticClass(), nullptr, *AssetPath));
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

    TArray<UPackage*> PackageArray = Packages.Array();
    UE_LOG(LogWitcherImportContext, Display, TEXT("Saving %d imported asset package(s)"), PackageArray.Num());
    if (!UEditorLoadingAndSavingUtils::SavePackages(PackageArray, false))
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
    return SerializeJson(Response);
}
