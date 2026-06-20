#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

using namespace WitcherImportInternal;

namespace
{
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
            TextureSample->SamplerType = BaseColorTexture->SRGB ? SAMPLERTYPE_Color : SAMPLERTYPE_LinearColor;
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

void EnsureLandscapeVisibilityMask(UMaterialInterface* MaterialInterface)
{
    UMaterial* Material = Cast<UMaterial>(MaterialInterface);
    if (!Material)
    {
        return;
    }
    if (UMaterialExpressionLandscapeVisibilityMask* Mask =
            Cast<UMaterialExpressionLandscapeVisibilityMask>(
                UMaterialEditingLibrary::CreateMaterialExpression(
                    Material, UMaterialExpressionLandscapeVisibilityMask::StaticClass(), -400, 550)))
    {
        Material->PreEditChange(nullptr);
        Material->BlendMode = BLEND_Masked;
        Material->OpacityMaskClipValue = 0.3333f;
        UMaterialEditingLibrary::ConnectMaterialProperty(Mask, TEXT(""), MP_OpacityMask);
        Material->PostEditChange();
        Material->MarkPackageDirty();
        UMaterialEditingLibrary::RecompileMaterial(Material);
    }
}
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
    // Texture array slices must share dimensions and source format.
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


namespace
{
FString TerrainBlendShaderTemplate()
{
    return FString(R"HLSL(
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

float3 textureWorldM = wpM + float3(TextureOffsetCmX, TextureOffsetCmY, 0.0) / 100.0;
float3 texPos = float3(textureWorldM.x, -textureWorldM.y, textureWorldM.z);
float2 ovUV = texPos.xy * 0.333;

float3 an = abs(N);
float3 tw = max(an - 0.576, float3(0.0,0.0,0.0));
tw /= max(tw.x + tw.y + tw.z, 1e-4);

uint cw, ch;
ControlTex.GetDimensions(cw, ch);
float2 cdim = float2(cw, ch);
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
}
}

bool FWitcherImportContext::GatherTerrainBlendInputs(
    const TSharedPtr<FJsonObject>& Terrain, const FString& AssetRel, FTerrainBlendInputs& Out)
{
    const TArray<TSharedPtr<FJsonValue>>* Layers = nullptr;
    if (!Terrain->TryGetArrayField(TEXT("layers"), Layers) || Layers->Num() == 0)
    {
        return false;
    }

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
    int32 MissingDiffuseCount = 0;
    int32 MissingNormalCount = 0;
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
        const FString DiffuseDepot = JsonString(Layer, TEXT("diffuse"));
        DiffuseSlices[Index] = Cast<UTexture2D>(FindTexture(DiffuseDepot));
        if (DiffuseDepot.IsEmpty() || !DiffuseSlices[Index])
        {
            MissingDiffuseCount++;
        }
        const FString NormalDepot = JsonString(Layer, TEXT("normal"));
        UTexture2D* NormalTex = Cast<UTexture2D>(FindTexture(NormalDepot));
        if (!NormalDepot.IsEmpty() && !NormalTex)
        {
            MissingNormalCount++;
        }
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
    if (MissingDiffuseCount > 0)
    {
        AddError(FString::Printf(
            TEXT("Terrain blend material: %d diffuse layer texture(s) missing; aborting."),
            MissingDiffuseCount));
        return false;
    }
    if (MissingNormalCount > 0)
    {
        AddError(FString::Printf(
            TEXT("Terrain blend material: %d normal layer texture(s) missing; aborting."),
            MissingNormalCount));
        return false;
    }
    const FString BlendSharpnessCode = HlslFloatArray(BlendSharpness);
    const FString SlopeBaseDampeningCode = HlslFloatArray(SlopeBaseDampening);
    const FString SlopeNormalDampeningCode = HlslFloatArray(SlopeNormalDampening);
    const FString FalloffCode = HlslFloatArray(Falloff);
    const FString SpecularityCode = HlslFloatArray(Specularity);
    const FString SpecularityBaseCode = HlslFloatArray(SpecularityBase);
    const FString SpecularityScaleCode = HlslFloatArray(SpecularityScale);

    // Texture arrays need every diffuse slice valid; normals are optional.
    UTexture2D* FallbackDiffuse = nullptr;
    for (UTexture2D* Slice : DiffuseSlices) { if (Slice) { FallbackDiffuse = Slice; break; } }
    if (!FallbackDiffuse)
    {
        AddError(TEXT("Terrain blend material: no diffuse layer textures resolved; aborting."));
        return false;
    }
    for (UTexture2D*& Slice : DiffuseSlices) { if (!Slice) { Slice = FallbackDiffuse; } }

    UTexture2DArray* DiffuseArray = BuildTerrainTextureArray(DiffuseSlices, AssetRel + TEXT("_diffuse_array"), false);
    if (!DiffuseArray)
    {
        AddError(TEXT("Terrain blend material: failed to build diffuse texture array."));
        return false;
    }
    UTexture2DArray* NormalArray = nullptr;
    if (bAnyNormal)
    {
        UTexture2D* FallbackNormal = nullptr;
        for (UTexture2D* Slice : NormalSlices) { if (Slice) { FallbackNormal = Slice; break; } }
        for (UTexture2D*& Slice : NormalSlices) { if (!Slice) { Slice = FallbackNormal; } }
        NormalArray = BuildTerrainTextureArray(NormalSlices, AssetRel + TEXT("_normal_array"), true);
        if (!NormalArray)
        {
            AddError(TEXT("Terrain blend material: failed to build normal texture array."));
            return false;
        }
    }
    const bool bHasNormalArray = NormalArray != nullptr;

    const FString ControlDepot = JsonString(Terrain, TEXT("control"));
    UTexture* ControlTex = ControlDepot.IsEmpty() ? nullptr : FindTexture(ControlDepot);
    UTexture* TintTex = FindTexture(JsonString(Terrain, TEXT("base_color_texture")));
    if (!ControlTex)
    {
        AddError(TEXT("Terrain blend material: control map missing; aborting."));
        return false;
    }

    // Lock control and tint maps to the landscape world AABB.
    const TSharedPtr<FJsonObject>* TransformPtr = nullptr;
    Terrain->TryGetObjectField(TEXT("transform"), TransformPtr);
    const TSharedPtr<FJsonObject> TransformObj = TransformPtr ? *TransformPtr : nullptr;
    const FVector Location = JsonVector(TransformObj, TEXT("location"), FVector::ZeroVector);
    const float SizeCm = static_cast<float>(JsonNumber(Terrain, TEXT("terrain_size"), 1.0) * 100.0);
    Out.DiffuseArray = DiffuseArray;
    Out.NormalArray = NormalArray;
    Out.bHasNormalArray = bHasNormalArray;
    Out.ControlTex = ControlTex;
    Out.TintTex = TintTex;
    Out.SizeCm = SizeCm;
    Out.Location = Location;
    Out.LayerCount = Layers->Num();
    Out.ParamCount = ParamCount;
    Out.BlendSharpnessCode = BlendSharpnessCode;
    Out.SlopeBaseDampeningCode = SlopeBaseDampeningCode;
    Out.SlopeNormalDampeningCode = SlopeNormalDampeningCode;
    Out.FalloffCode = FalloffCode;
    Out.SpecularityCode = SpecularityCode;
    Out.SpecularityBaseCode = SpecularityBaseCode;
    Out.SpecularityScaleCode = SpecularityScaleCode;
    return true;
}

UMaterialInterface* FWitcherImportContext::BuildTerrainBlendMaterial(
    const TSharedPtr<FJsonObject>& Terrain, const FString& AssetRel)
{
    FTerrainBlendInputs Inputs;
    if (!GatherTerrainBlendInputs(Terrain, AssetRel, Inputs))
    {
        return nullptr;
    }

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
    Corner->R = static_cast<float>(Inputs.Location.X);
    Corner->G = static_cast<float>(Inputs.Location.Y);
    UMaterialExpressionSubtract* Centered = Cast<UMaterialExpressionSubtract>(
        NewExpr(UMaterialExpressionSubtract::StaticClass(), -1300, 0));
    Centered->A.Connect(0, WorldXY);
    Centered->B.Connect(0, Corner);
    UMaterialExpressionDivide* UV01 = Cast<UMaterialExpressionDivide>(
        NewExpr(UMaterialExpressionDivide::StaticClass(), -1100, 0));
    UV01->A.Connect(0, Centered);
    UV01->ConstB = Inputs.SizeCm;

    UMaterialExpression* WorldPos2 = NewExpr(UMaterialExpressionWorldPosition::StaticClass(), -1100, 250);
    UMaterialExpression* SurfaceNormal = NewExpr(UMaterialExpressionVertexNormalWS::StaticClass(), -1100, 450);

    auto TexObj = [&](UTexture* Tex, int32 Y) -> UMaterialExpression*
    {
        UMaterialExpressionTextureObject* Obj = Cast<UMaterialExpressionTextureObject>(
            NewExpr(UMaterialExpressionTextureObject::StaticClass(), -800, Y));
        Obj->Texture = Tex;
        return Obj;
    };
    UMaterialExpression* DiffuseObj = TexObj(Inputs.DiffuseArray, 600);
    UMaterialExpression* NormalObj = TexObj(Inputs.bHasNormalArray ? (UTexture*)Inputs.NormalArray : (UTexture*)Inputs.DiffuseArray, 800);
    UMaterialExpression* ControlObj = TexObj(Inputs.ControlTex, 1000);
    UMaterialExpression* TintObj = TexObj(Inputs.TintTex ? Inputs.TintTex : Inputs.ControlTex, 1200);
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
    TerrainSizeCm->R = Inputs.SizeCm;

    // W3 terrain blend: packed control texel, overlay/background atlas slices,
    // slope blend, then per-channel tint.
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

    FString TerrainShaderCode = TerrainBlendShaderTemplate();
    TerrainShaderCode.ReplaceInline(TEXT("__LAYER_COUNT__"), *FString::FromInt(FMath::Max(1, Inputs.LayerCount)));
    TerrainShaderCode.ReplaceInline(TEXT("__LAYER_PARAM_COUNT__"), *FString::FromInt(Inputs.ParamCount));
    TerrainShaderCode.ReplaceInline(TEXT("__BLEND_SHARPNESS__"), *Inputs.BlendSharpnessCode);
    TerrainShaderCode.ReplaceInline(TEXT("__SLOPE_BASE_DAMPENING__"), *Inputs.SlopeBaseDampeningCode);
    TerrainShaderCode.ReplaceInline(TEXT("__SLOPE_NORMAL_DAMPENING__"), *Inputs.SlopeNormalDampeningCode);
    TerrainShaderCode.ReplaceInline(TEXT("__FALLOFF__"), *Inputs.FalloffCode);
    TerrainShaderCode.ReplaceInline(TEXT("__SPECULARITY__"), *Inputs.SpecularityCode);
    TerrainShaderCode.ReplaceInline(TEXT("__SPECULARITY_BASE__"), *Inputs.SpecularityBaseCode);
    TerrainShaderCode.ReplaceInline(TEXT("__SPECULARITY_SCALE__"), *Inputs.SpecularityScaleCode);
    TerrainShaderCode.ReplaceInline(TEXT("__HAS_NORMALS__"), Inputs.bHasNormalArray ? TEXT("1.0") : TEXT("0.0"));
    Custom->Code = TerrainShaderCode;

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

    const TSharedPtr<FJsonObject>* TransformPtr = nullptr;
    Terrain->TryGetObjectField(TEXT("transform"), TransformPtr);
    const TSharedPtr<FJsonObject> TransformObj = TransformPtr ? *TransformPtr : nullptr;
    const FVector Location = JsonVector(TransformObj, TEXT("location"), FVector::ZeroVector);
    const FVector Scale = JsonVector(TransformObj, TEXT("scale"), FVector(100.0, 100.0, 100.0));

    const FString TerrainAssetRel = JsonString(Terrain, TEXT("asset_path"), TEXT("witcher_terrain"));
    UMaterialInterface* TerrainMaterial = nullptr;
    {
        const FString MaterialRel = TerrainAssetRel + TEXT("_terrain_m");
        const TArray<TSharedPtr<FJsonValue>>* Layers = nullptr;
        const bool bHasLayers = Terrain->TryGetArrayField(TEXT("layers"), Layers) && Layers && Layers->Num() > 0;
        const bool bSourceWorldTerrain = !JsonString(Terrain, TEXT("world_path")).IsEmpty();
        if (bSourceWorldTerrain && !bHasLayers)
        {
            AddError(TEXT("Terrain import: manifest has no terrain blend layers; refusing tint-only import."));
            return;
        }
        if (bHasLayers && JsonString(Terrain, TEXT("control")).IsEmpty())
        {
            AddError(TEXT("Terrain import: manifest has blend layers but no control map; refusing tint-only import."));
            return;
        }
        if (bHasLayers)
        {
            TerrainMaterial = BuildTerrainBlendMaterial(Terrain, MaterialRel);
            if (!TerrainMaterial)
            {
                AddError(TEXT("Terrain import: terrain blend material failed; refusing fallback tint material."));
                return;
            }
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
    TArray<FLandscapeImportLayerInfo>& ImportLayers =
        MaterialLayerDataPerLayer.Add(FGuid(), TArray<FLandscapeImportLayerInfo>());

    bool bHasHoles = false;
    const TSharedPtr<FJsonObject>* VisibilityPtr = nullptr;
    if (Terrain->TryGetObjectField(TEXT("visibility"), VisibilityPtr) && VisibilityPtr)
    {
        const TSharedPtr<FJsonObject> Visibility = *VisibilityPtr;
        const FString VisPath = ResolveBundleFile(JsonString(Visibility, TEXT("file")));
        TArray<uint8> VisBytes;
        if (FFileHelper::LoadFileToArray(VisBytes, *VisPath) && VisBytes.Num() == ExpectedSamples)
        {
            ULandscapeLayerInfoObject* VisLayerInfo = ALandscapeProxy::VisibilityLayer;
            if (VisLayerInfo)
            {
                FLandscapeImportLayerInfo VisImport;
                VisImport.LayerName = VisLayerInfo->LayerName;
                VisImport.LayerInfo = VisLayerInfo;
                VisImport.LayerData = MoveTemp(VisBytes);
                ImportLayers.Add(MoveTemp(VisImport));
                bHasHoles = true;
            }
            else
            {
                AddError(TEXT("Terrain holes: ALandscapeProxy::VisibilityLayer is unavailable; landscape will be solid."));
            }
        }
        else
        {
            AddError(FString::Printf(
                TEXT("Terrain holes: visibility map missing or wrong size (%s); landscape will be solid."),
                *VisPath));
        }
    }

    if (bHasHoles && TerrainMaterial)
    {
        EnsureLandscapeVisibilityMask(TerrainMaterial);
    }

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
