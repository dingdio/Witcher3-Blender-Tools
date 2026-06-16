#pragma once

#include "CoreMinimal.h"

class UMaterial;
class UMaterialInterface;
class UMaterialInstanceConstant;
class UAnimSequence;
class USkeleton;
class UTexture;
class UObject;
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
    void ImportAnimations();
    void ImportBlueprint();
    void ImportTerrain();
    void ImportPlacements();
    class UStaticMesh* FindPlacementMesh(const FString& AssetRel);
    class UTexture2DArray* BuildTerrainTextureArray(const TArray<class UTexture2D*>& Slices, const FString& AssetRel, bool bNormal);
    UMaterialInterface* BuildTerrainBlendMaterial(const TSharedPtr<FJsonObject>& Terrain, const FString& AssetRel);

    UTexture* ImportTexture(const TSharedPtr<FJsonObject>& TextureObject);
    UMaterialInterface* EnsureMasterMaterial(const TSharedPtr<FJsonObject>& MasterObject);
    UMaterialInterface* ImportMaterialInstance(const TSharedPtr<FJsonObject>& MaterialObject);
    UMaterialInterface* ResolveParent(const TSharedPtr<FJsonObject>& MaterialObject);
    void ApplyInstanceParams(UMaterialInstanceConstant* Instance, const TSharedPtr<FJsonObject>& MaterialObject);
    UTexture* FindTexture(const FString& DepotRel);

    UObject* ImportFbxMesh(const FString& BundleRelativeFbx, const FString& AssetRel, bool bSkeletal, USkeleton* Skeleton);
    UObject* ImportBufferMesh(const FString& BundleRelativeBuffer, const FString& AssetRel, bool bSkeletal, USkeleton* Skeleton);
    UAnimSequence* ImportAnimation(const TSharedPtr<FJsonObject>& AnimationObject);
    void AssignMaterialsToMesh(UObject* MeshObject, const TSharedPtr<FJsonObject>& MeshEntry);
    UAnimSequence* FindAnimSequence(const FString& AssetRel);

    FString PackagePathFor(const FString& AssetRel) const;
    FString ObjectPathFor(const FString& AssetRel) const;
    UObject* LoadAnyAsset(const FString& AssetRel) const;
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
    TMap<FString, TWeakObjectPtr<UMaterialInterface>> MastersByRel;
    TMap<FString, TWeakObjectPtr<UMaterialInterface>> MaterialsById;
    TMap<FString, TWeakObjectPtr<UObject>> MeshesByAssetRel;
    TWeakObjectPtr<USkeleton> SharedSkeleton;
};
