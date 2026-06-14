#include "WitcherImportContext.h"

#include "Animation/AnimSequence.h"
#include "Animation/Skeleton.h"
#include "AssetImportTask.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetToolsModule.h"
#include "Components/SkeletalMeshComponent.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "Engine/SCS_Node.h"
#include "Engine/SimpleConstructionScript.h"
#include "Engine/SkeletalMesh.h"
#include "Engine/StaticMesh.h"
#include "Engine/Texture.h"
#include "Engine/Texture2D.h"
#include "Factories/FbxAnimSequenceImportData.h"
#include "Factories/FbxImportUI.h"
#include "Factories/FbxSkeletalMeshImportData.h"
#include "Factories/FbxStaticMeshImportData.h"
#include "Factories/MaterialFactoryNew.h"
#include "Factories/TextureFactory.h"
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

    // Reuse a texture that already exists at the mirrored depot path.
    if (UTexture* Existing = LoadExistingAsset<UTexture>(ObjectPathFor(AssetRel)))
    {
        TexturesByDepot.Add(DepotRel, Existing);
        return Existing;
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
        else
        {
            Texture2D->CompressionSettings = TC_Default;
        }
    }
    Texture->PostEditChange();
    Texture->MarkPackageDirty();
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
    if (const TWeakObjectPtr<UMaterialInterface>* Cached = MastersByRel.Find(AssetRel))
    {
        return Cached->Get();
    }

    const FString SafeRel = ClassSafeAssetRel(AssetRel, UMaterialInterface::StaticClass(), TEXT("_mi"));

    // Hand-authored masters at the mirrored .w2mg path always win.
    if (UMaterialInterface* Existing = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(SafeRel)))
    {
        MastersByRel.Add(AssetRel, Existing);
        return Existing;
    }

    const FString Name = AssetRelName(SafeRel);
    FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
    UMaterialFactoryNew* Factory = NewObject<UMaterialFactoryNew>();
    UMaterial* Material = Cast<UMaterial>(
        AssetToolsModule.Get().CreateAsset(Name, PackagePathFor(SafeRel), UMaterial::StaticClass(), Factory));
    if (!Material)
    {
        AddWarning(FString::Printf(TEXT("Could not create master material '%s'"), *SafeRel));
        return nullptr;
    }

    Material->PreEditChange(nullptr);

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
    ImportedAssets.Add(Material->GetPathName());
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

    // Existing chain instances at the mirrored path are reused untouched so
    // manual tweaks survive re-export. Local (mesh-owned) instances refresh
    // their parameters each export, keeping Blender edits flowing through.
    if (UMaterialInterface* Existing = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(SafeRel)))
    {
        if (JsonBool(MaterialObject, TEXT("local"), false))
        {
            if (UMaterialInstanceConstant* ExistingInstance = Cast<UMaterialInstanceConstant>(Existing))
            {
                ExistingInstance->PreEditChange(nullptr);
                ApplyInstanceParams(ExistingInstance, MaterialObject);
                ExistingInstance->PostEditChange();
                ExistingInstance->MarkPackageDirty();
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
    if (JsonBool(MaterialObject, TEXT("enable_mask"), false))
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
        SharedSkeleton = ExistingSkeleton;
        return;
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
        UObject* Imported = ImportFbxMesh(JsonString(MeshEntry, TEXT("fbx")), AssetRel, bSkeletal,
            bSkeletal ? SharedSkeleton.Get() : nullptr);
        if (!Imported)
        {
            AddError(FString::Printf(TEXT("Failed to import mesh '%s'"), *AssetRel));
            continue;
        }
        MeshesByAssetRel.Add(AssetRel, Imported);
        if (bSkeletal && !SharedSkeleton.IsValid())
        {
            if (USkeletalMesh* SkeletalMesh = Cast<USkeletalMesh>(Imported))
            {
                SharedSkeleton = SkeletalMesh->GetSkeleton();
            }
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
    }
    if (Options->StaticMeshImportData)
    {
        Options->StaticMeshImportData->bCombineMeshes = true;
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
        const TArray<USCS_Node*> ExistingRootNodes = ConstructionScript->GetRootNodes();
        for (USCS_Node* Node : ExistingRootNodes)
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

        ConstructionScript->ValidateSceneRootNodes();
        return BaseTemplate;
    };

    if (UBlueprint* ExistingBlueprint = LoadExistingAsset<UBlueprint>(ObjectPathFor(AssetRel)))
    {
        const bool bReparented = EnsureWitcherImportedActorParent(ExistingBlueprint);
        if (USkeletalMeshComponent* BaseTemplate = RebuildBlueprintComponents(ExistingBlueprint))
        {
            // RebuildBlueprintComponents already recreated and configured every
            // follower; only the driver still needs its base/anim setup.
            ConfigureImportedBaseTemplate(BaseTemplate, AnimSequence);
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

FString FWitcherImportContext::BuildResponse(bool bSuccess) const
{
    TSharedPtr<FJsonObject> Response = MakeShared<FJsonObject>();
    Response->SetBoolField(TEXT("success"), bSuccess);
    SetStringArray(Response, TEXT("imported_assets"), ImportedAssets);
    SetStringArray(Response, TEXT("warnings"), Warnings);
    SetStringArray(Response, TEXT("errors"), Errors);
    return SerializeJson(Response);
}
