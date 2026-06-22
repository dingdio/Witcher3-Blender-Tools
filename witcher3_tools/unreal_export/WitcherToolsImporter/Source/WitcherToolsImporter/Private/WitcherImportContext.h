#pragma once

#include "CoreMinimal.h"

class UMaterial;
class UMaterialInterface;
class UMaterialInstanceConstant;
class UMaterialExpression;
class UMaterialExpressionTextureSampleParameter2D;
class UAnimSequence;
class USkeletalMesh;
class USkeleton;
class UTexture;
class UObject;
class UWorld;
class AActor;
class FJsonObject;
class FJsonValue;

/**
 * Imports a witcher_unreal_export.v2 bundle: assets mirror the Witcher depot
 * layout under the manifest's content root. Existing assets at the mirrored
 * paths (hand-authored master materials, textures, material instances) are
 * reused instead of being recreated.
 */
class FWitcherImportContext
{
public:
    static FString HandleRequest(const FString& RequestJson);
    static FString ErrorResponse(const FString& ErrorMessage);

private:
    explicit FWitcherImportContext(const TSharedPtr<FJsonObject>& InManifest);

    FString ImportBundle();
    void ImportTextures();
    void ImportMasters();
    void ImportMaterials();
    void ImportRig();
    void ImportMeshes();
    void ImportSpeedTrees();
    void ImportAnimations();
    void ImportBlueprint();
    void ImportRetargetSetup();
    void ImportTerrain();
    void ImportPlacements();
    void ImportFoliage();
    class UFoliageType_InstancedStaticMesh* EnsureFoliageType(
        class UStaticMesh* Mesh,
        const FString& MeshAssetRel,
        const FString& FoliageFolder);
    class UStaticMesh* FindPlacementMesh(const FString& AssetRel);

    struct FPlacementLayer
    {
        UWorld* World = nullptr;
        FString LayerId;
        FName LayerTag;
        FString MeshFolder;
        FString CollisionFolder;
        FString LightFolder;
        FString EntityFolder;
    };
    struct FPlacementLayerStats
    {
        int32 Actors = 0;
        int32 CollisionActors = 0;
        int32 Instances = 0;
        int32 Lights = 0;
        int32 Entities = 0;
        int32 EntityComponents = 0;
    };
    bool ArePlacementLayerMeshesReady(const TSharedPtr<FJsonObject>& Layer, const FString& LayerId);
    void TagPlacementActor(const FPlacementLayer& Layer, AActor* Actor, const FString& ActorName, const FString& ActorFolder);
    void ImportPlacementActors(const FPlacementLayer& Layer, const TArray<TSharedPtr<FJsonValue>>& Actors, FPlacementLayerStats& Stats);
    void ImportPlacementInstancers(const FPlacementLayer& Layer, const TArray<TSharedPtr<FJsonValue>>& Instancers, FPlacementLayerStats& Stats);
    void ImportPlacementLights(const FPlacementLayer& Layer, const TArray<TSharedPtr<FJsonValue>>& Lights, FPlacementLayerStats& Stats);
    void ImportPlacementEntities(const FPlacementLayer& Layer, const TArray<TSharedPtr<FJsonValue>>& Entities, FPlacementLayerStats& Stats);

    class UTexture2DArray* BuildTerrainTextureArray(const TArray<class UTexture2D*>& Slices, const FString& AssetRel, bool bNormal);

    struct FTerrainBlendInputs
    {
        class UTexture2DArray* DiffuseArray = nullptr;
        class UTexture2DArray* NormalArray = nullptr;
        bool bHasNormalArray = false;
        UTexture* ControlTex = nullptr;
        UTexture* TintTex = nullptr;
        float SizeCm = 0.0f;
        FVector Location = FVector::ZeroVector;
        int32 LayerCount = 0;
        int32 ParamCount = 0;
        FString BlendSharpnessCode;
        FString SlopeBaseDampeningCode;
        FString SlopeNormalDampeningCode;
        FString FalloffCode;
        FString SpecularityCode;
        FString SpecularityBaseCode;
        FString SpecularityScaleCode;
    };
    bool GatherTerrainBlendInputs(const TSharedPtr<FJsonObject>& Terrain, const FString& AssetRel, FTerrainBlendInputs& Out);
    UMaterialInterface* BuildTerrainBlendMaterial(const TSharedPtr<FJsonObject>& Terrain, const FString& AssetRel);

    UTexture* ImportTexture(const TSharedPtr<FJsonObject>& TextureObject);
    UMaterialInterface* EnsureMasterMaterial(const TSharedPtr<FJsonObject>& MasterObject);
    struct FMasterMaterialSources
    {
        UMaterialExpression* BaseColorSource = nullptr;
        UMaterialExpression* NormalSource = nullptr;
        UMaterialExpression* RoughTextureSource = nullptr;
        UMaterialExpression* RoughScalarSource = nullptr;
        UMaterialExpressionTextureSampleParameter2D* BaseColorTextureSource = nullptr;
        UMaterialExpressionTextureSampleParameter2D* NormalTextureSource = nullptr;
        int32 NodeY = 0;
    };
    void CreateMasterMaterialParams(UMaterial* Material, const TSharedPtr<FJsonObject>& MasterObject, const FString& AssetRel, FMasterMaterialSources& Out);
    void WireMasterMaterialPins(UMaterial* Material, const FMasterMaterialSources& Sources);
    UMaterialInterface* ImportMaterialInstance(const TSharedPtr<FJsonObject>& MaterialObject);
    UMaterialInterface* ResolveParent(const TSharedPtr<FJsonObject>& MaterialObject);
    void ApplyInstanceParams(UMaterialInstanceConstant* Instance, const TSharedPtr<FJsonObject>& MaterialObject);
    UTexture* FindTexture(const FString& DepotRel);

    UObject* ImportFbxMesh(const FString& BundleRelativeFbx, const FString& AssetRel, bool bSkeletal, USkeleton* Skeleton,
        bool bApplyRetargetPreviewFacing = false);
    UObject* ImportBufferMesh(const FString& BundleRelativeBuffer, const FString& AssetRel, bool bSkeletal, USkeleton* Skeleton);
    UAnimSequence* ImportAnimation(const TSharedPtr<FJsonObject>& AnimationObject);
    void AssignMaterialsToMesh(UObject* MeshObject, const TSharedPtr<FJsonObject>& MeshEntry);
    UAnimSequence* FindAnimSequence(const FString& AssetRel);

    FString PackagePathFor(const FString& AssetRel) const;
    FString ObjectPathFor(const FString& AssetRel) const;
    UObject* LoadAnyAsset(const FString& AssetRel) const;
    void BuildManifestAssetPathReservations();
    FString ClassSafeAssetRel(const FString& AssetRel, UClass* DesiredClass, const FString& Suffix);
    FString ResolveBundleFile(const FString& RelativePath) const;
    bool ShouldOverwrite(const FString& Category) const;
    void AddWarning(const FString& Warning);
    void AddError(const FString& Error);
    void SaveImportedPackages();
    FString BuildResponse(bool bSuccess) const;

    TSharedPtr<FJsonObject> Manifest;
    TSharedPtr<FJsonObject> OverwriteObject;
    FString BundleRoot;
    FString ContentRoot;
    FString AssetName;
    TArray<FString> ImportedAssets;
    TArray<FString> Warnings;
    TArray<FString> Errors;

    TArray<TPair<FString, double>> PhaseTimings;
    double TotalImportSeconds = 0.0;

    TMap<FString, TWeakObjectPtr<UTexture>> TexturesByDepot;
    TMap<FString, FString> TextureAssetRelByDepot;
    TMap<FString, TWeakObjectPtr<UMaterialInterface>> MastersByRel;
    TMap<FString, TWeakObjectPtr<UMaterialInterface>> MaterialsById;
    TMap<FString, TWeakObjectPtr<UObject>> MeshesByAssetRel;
    TMap<FString, FString> MeshAssetRelBySource;
    TWeakObjectPtr<USkeleton> SharedSkeleton;
    TWeakObjectPtr<USkeletalMesh> RetargetPreviewMesh;
};
