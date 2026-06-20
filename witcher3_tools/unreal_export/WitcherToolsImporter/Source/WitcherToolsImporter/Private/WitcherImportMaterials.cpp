#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

using namespace WitcherImportInternal;

namespace
{
bool IsVolumeMaterialRel(const FString& AssetRel)
{
    FString Normalized = AssetRel.Replace(TEXT("\\"), TEXT("/")).ToLower();
    if (Normalized.EndsWith(TEXT(".w2mg")))
    {
        Normalized.LeftChopInline(5);
    }
    return Normalized == TEXT("engine/materials/defaults/volume");
}

bool IsEyeShadowMaterialRel(const FString& AssetRel)
{
    FString Normalized = AssetRel.Replace(TEXT("\\"), TEXT("/")).ToLower();
    if (Normalized.EndsWith(TEXT(".w2mg")))
    {
        Normalized.LeftChopInline(5);
    }
    return Normalized == TEXT("engine/materials/graphs/eyeshadow/pbr_eye_shadow");
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

void ApplyEyeShadowMaterial(UMaterial* Material)
{
    if (!Material)
    {
        return;
    }

    Material->PreEditChange(nullptr);
    UMaterialEditingLibrary::DeleteAllMaterialExpressions(Material);
    Material->BlendMode = BLEND_Translucent;

    auto NewExpr = [&](UClass* Cls, int32 X, int32 Y) -> UMaterialExpression*
    {
        return UMaterialEditingLibrary::CreateMaterialExpression(Material, Cls, X, Y);
    };

    UMaterialExpressionVectorParameter* BaseColor = Cast<UMaterialExpressionVectorParameter>(
        NewExpr(UMaterialExpressionVectorParameter::StaticClass(), -900, -220));
    if (BaseColor)
    {
        BaseColor->ParameterName = FName(TEXT("Base Color"));
        BaseColor->DefaultValue = FLinearColor(0.8f, 0.8f, 0.8f, 1.0f);
        UMaterialEditingLibrary::ConnectMaterialProperty(BaseColor, TEXT(""), MP_BaseColor);
    }

    UMaterialExpressionVectorParameter* Color = Cast<UMaterialExpressionVectorParameter>(
        NewExpr(UMaterialExpressionVectorParameter::StaticClass(), -900, 120));
    UMaterialExpressionScalarParameter* Gamma = Cast<UMaterialExpressionScalarParameter>(
        NewExpr(UMaterialExpressionScalarParameter::StaticClass(), -900, 340));
    UMaterialExpressionConstant3Vector* Luminance = Cast<UMaterialExpressionConstant3Vector>(
        NewExpr(UMaterialExpressionConstant3Vector::StaticClass(), -650, 500));
    UMaterialExpressionPower* GammaCorrected = Cast<UMaterialExpressionPower>(
        NewExpr(UMaterialExpressionPower::StaticClass(), -650, 120));
    UMaterialExpressionOneMinus* Inverted = Cast<UMaterialExpressionOneMinus>(
        NewExpr(UMaterialExpressionOneMinus::StaticClass(), -650, 310));
    UMaterialExpressionSubtract* Difference = Cast<UMaterialExpressionSubtract>(
        NewExpr(UMaterialExpressionSubtract::StaticClass(), -390, 180));
    UMaterialExpressionDotProduct* AlphaValue = Cast<UMaterialExpressionDotProduct>(
        NewExpr(UMaterialExpressionDotProduct::StaticClass(), -140, 180));
    UMaterialExpressionDivide* AlphaScaled = Cast<UMaterialExpressionDivide>(
        NewExpr(UMaterialExpressionDivide::StaticClass(), 100, 180));
    UMaterialExpressionSaturate* AlphaClamped = Cast<UMaterialExpressionSaturate>(
        NewExpr(UMaterialExpressionSaturate::StaticClass(), 330, 180));

    if (Color)
    {
        Color->ParameterName = FName(TEXT("Color"));
        Color->DefaultValue = FLinearColor::White;
    }
    if (Gamma)
    {
        Gamma->ParameterName = FName(TEXT("Gamma"));
        Gamma->DefaultValue = 1.0f;
    }
    if (Luminance)
    {
        Luminance->Constant = FLinearColor(0.299f, 0.587f, 0.114f, 1.0f);
    }
    if (Color && Gamma && GammaCorrected)
    {
        GammaCorrected->Base.Connect(0, Color);
        GammaCorrected->Exponent.Connect(0, Gamma);
    }
    if (Color && Inverted)
    {
        Inverted->Input.Connect(0, Color);
    }
    if (GammaCorrected && Inverted && Difference)
    {
        Difference->A.Connect(0, GammaCorrected);
        Difference->B.Connect(0, Inverted);
    }
    if (Difference && Luminance && AlphaValue)
    {
        AlphaValue->A.Connect(0, Difference);
        AlphaValue->B.Connect(0, Luminance);
    }
    if (AlphaValue && AlphaScaled)
    {
        AlphaScaled->A.Connect(0, AlphaValue);
        AlphaScaled->ConstB = 11.0f;
    }
    if (AlphaScaled && AlphaClamped)
    {
        AlphaClamped->Input.Connect(0, AlphaScaled);
        UMaterialEditingLibrary::ConnectMaterialProperty(AlphaClamped, TEXT(""), MP_Opacity);
    }

    if (UMaterialExpressionConstant* Metallic = Cast<UMaterialExpressionConstant>(
            NewExpr(UMaterialExpressionConstant::StaticClass(), -150, -260)))
    {
        Metallic->R = 0.0f;
        UMaterialEditingLibrary::ConnectMaterialProperty(Metallic, TEXT(""), MP_Metallic);
    }
    if (UMaterialExpressionConstant* Roughness = Cast<UMaterialExpressionConstant>(
            NewExpr(UMaterialExpressionConstant::StaticClass(), -150, -80)))
    {
        Roughness->R = 1.0f;
        UMaterialEditingLibrary::ConnectMaterialProperty(Roughness, TEXT(""), MP_Roughness);
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

EMaterialSamplerType SamplerTypeForParamName(const FString& ParamName)
{
    const FString Lowered = ParamName.ToLower();
    if (Lowered.Contains(TEXT("rough")) || Lowered.Contains(TEXT("mask")) || Lowered.Contains(TEXT("pattern")))
    {
        return SAMPLERTYPE_Masks;
    }
    if (Lowered.Contains(TEXT("normal")) || Lowered.Contains(TEXT("bump")))
    {
        return SAMPLERTYPE_LinearColor;
    }
    if (Lowered.Contains(TEXT("diffuse")))
    {
        return SAMPLERTYPE_Color;
    }
    return SAMPLERTYPE_LinearColor;
}
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

    // Reuse mirrored masters unless the "materials_base" policy requests a rebuild.
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
            // Non-UMaterial occupants cannot have their graph rebuilt in place.
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

    if (IsEyeShadowMaterialRel(AssetRel))
    {
        ApplyEyeShadowMaterial(Material);
        ImportedAssets.AddUnique(Material->GetPathName());
        MastersByRel.Add(AssetRel, Material);
        return Material;
    }

    Material->PreEditChange(nullptr);
    if (bRebuildExisting)
    {
        UMaterialEditingLibrary::DeleteAllMaterialExpressions(Material);
    }

    UMaterialExpression* BaseColorSource = nullptr;
    UMaterialExpression* NormalSource = nullptr;
    UMaterialExpression* RoughTextureSource = nullptr;
    UMaterialExpression* RoughScalarSource = nullptr;
    UMaterialExpressionTextureSampleParameter2D* BaseColorTextureSource = nullptr;
    UMaterialExpressionTextureSampleParameter2D* NormalTextureSource = nullptr;
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
                    // Default textures must match the sampler type or the master fails to compile.
                    TextureExpression->SamplerType = SamplerTypeForParamName(ParamName);
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
                            BaseColorTextureSource = TextureExpression;
                            BaseColorIsExact = bExactDiffuse;
                        }
                    }
                    const bool bExactNormal = LoweredName == TEXT("normal");
                    if (bExactNormal || (!NormalSource && TextureExpression->SamplerType == SAMPLERTYPE_Normal))
                    {
                        if (bExactNormal || !NormalIsExact)
                        {
                            NormalSource = TextureExpression;
                            NormalTextureSource = TextureExpression;
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

    // Only connect texture pins whose node has a valid default texture.
    auto NodeHasTexture = [](UMaterialExpression* Expression) -> bool
    {
        const UMaterialExpressionTextureBase* TextureBase = Cast<UMaterialExpressionTextureBase>(Expression);
        return TextureBase && TextureBase->Texture != nullptr;
    };

    auto NewComponentMask = [&](UMaterialExpression* Source, int32 OutputIndex, bool bR, bool bG, bool bB, bool bA, int32 X, int32 Y)
        -> UMaterialExpressionComponentMask*
    {
        if (!Source)
        {
            return nullptr;
        }
        UMaterialExpressionComponentMask* Mask = Cast<UMaterialExpressionComponentMask>(
            UMaterialEditingLibrary::CreateMaterialExpression(
                Material, UMaterialExpressionComponentMask::StaticClass(), X, Y));
        if (!Mask)
        {
            return nullptr;
        }
        Mask->R = bR;
        Mask->G = bG;
        Mask->B = bB;
        Mask->A = bA;
        Mask->Input.Connect(OutputIndex, Source);
        return Mask;
    };

    if (BaseColorSource && NodeHasTexture(BaseColorSource))
    {
        UMaterialEditingLibrary::ConnectMaterialProperty(BaseColorSource, TEXT(""), MP_BaseColor);
    }

    if (BaseColorTextureSource && NodeHasTexture(BaseColorTextureSource))
    {
        UMaterialExpressionComponentMask* AlphaMask = NewComponentMask(
            BaseColorTextureSource, 5, false, false, false, true, -150, NodeY + 100);
        if (AlphaMask)
        {
            Material->BlendMode = BLEND_Masked;
            Material->OpacityMaskClipValue = 0.3333f;
            UMaterialEditingLibrary::ConnectMaterialProperty(AlphaMask, TEXT(""), MP_OpacityMask);
            UMaterialEditingLibrary::ConnectMaterialProperty(AlphaMask, TEXT(""), MP_Opacity);
        }
    }

    UMaterialExpression* RoughFromNormalAlpha = nullptr;
    if (NormalTextureSource && NodeHasTexture(NormalTextureSource))
    {
        UMaterialExpressionComponentMask* NormalRGB = NewComponentMask(
            NormalTextureSource, 5, true, true, true, false, -150, NodeY + 350);
        if (NormalRGB)
        {
            UMaterialExpressionMultiply* NormalScaled = Cast<UMaterialExpressionMultiply>(
                UMaterialEditingLibrary::CreateMaterialExpression(
                    Material, UMaterialExpressionMultiply::StaticClass(), 50, NodeY + 350));
            UMaterialExpressionSubtract* NormalUnpacked = Cast<UMaterialExpressionSubtract>(
                UMaterialEditingLibrary::CreateMaterialExpression(
                    Material, UMaterialExpressionSubtract::StaticClass(), 250, NodeY + 350));
            if (NormalScaled && NormalUnpacked)
            {
                NormalScaled->A.Connect(0, NormalRGB);
                NormalScaled->ConstB = 2.0f;
                NormalUnpacked->A.Connect(0, NormalScaled);
                NormalUnpacked->ConstB = 1.0f;
                UMaterialEditingLibrary::ConnectMaterialProperty(NormalUnpacked, TEXT(""), MP_Normal);
            }
        }
        RoughFromNormalAlpha = NewComponentMask(
            NormalTextureSource, 5, false, false, false, true, -150, NodeY + 650);
    }
    else if (NormalSource && NodeHasTexture(NormalSource))
    {
        UMaterialEditingLibrary::ConnectMaterialProperty(NormalSource, TEXT(""), MP_Normal);
    }

    if (RoughFromNormalAlpha)
    {
        UMaterialEditingLibrary::ConnectMaterialProperty(RoughFromNormalAlpha, TEXT(""), MP_Roughness);
    }
    else if (RoughTextureSource && NodeHasTexture(RoughTextureSource))
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

    // Preserve manual chain-instance tweaks; refresh local or explicitly overwritten instances.
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
