#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"
#include "WitcherRetargetSetup.h"

using namespace WitcherImportInternal;

namespace
{
constexpr const TCHAR* WomanBaseAssetRel = TEXT("characters/base_entities/woman_base/woman_base");
constexpr const TCHAR* WomanBaseSkeletonObjectPath = TEXT("/Game/Witcher3/characters/base_entities/woman_base/woman_base_Skeleton.woman_base_Skeleton");
constexpr const TCHAR* ManBaseAssetRel = TEXT("characters/base_entities/man_base/man_base");
constexpr const TCHAR* ManBaseSkeletonObjectPath = TEXT("/Game/Witcher3/characters/base_entities/man_base/man_base_Skeleton.man_base_Skeleton");

FString NormalizeAssetRel(FString Value)
{
    Value.ReplaceInline(TEXT("\\"), TEXT("/"));
    Value.TrimStartAndEndInline();
    while (Value.StartsWith(TEXT("/")))
    {
        Value.RightChopInline(1);
    }
    return Value.ToLower();
}

bool IsBaseAssetRel(const FString& Value, const TCHAR* BaseAssetRel)
{
    return NormalizeAssetRel(Value) == BaseAssetRel;
}

FString ProfileForBaseAssetRel(const FString& Value)
{
    if (IsBaseAssetRel(Value, WomanBaseAssetRel))
    {
        return TEXT("woman_base");
    }
    if (IsBaseAssetRel(Value, ManBaseAssetRel))
    {
        return TEXT("man_base");
    }
    return FString();
}

FString ProfileForSkeleton(const USkeleton* Skeleton)
{
    if (!Skeleton)
    {
        return FString();
    }
    const FString SkeletonPath = Skeleton->GetPathName();
    if (SkeletonPath == WomanBaseSkeletonObjectPath)
    {
        return TEXT("woman_base");
    }
    if (SkeletonPath == ManBaseSkeletonObjectPath)
    {
        return TEXT("man_base");
    }
    return FString();
}

FString RetargetTargetProfile(const TSharedPtr<FJsonObject>& Manifest, const USkeleton* SharedSkeleton)
{
    const TSharedPtr<FJsonObject>* RetargetObject = nullptr;
    if (Manifest->TryGetObjectField(TEXT("retarget_setup"), RetargetObject) && RetargetObject && RetargetObject->IsValid())
    {
        const FString TargetProfile = JsonString(*RetargetObject, TEXT("target_profile")).ToLower();
        if (TargetProfile == TEXT("woman_base") || TargetProfile == TEXT("man_base"))
        {
            return TargetProfile;
        }
    }

    const TSharedPtr<FJsonObject>* RigObject = nullptr;
    if (Manifest->TryGetObjectField(TEXT("rig"), RigObject) && RigObject && RigObject->IsValid())
    {
        const FString Profile = ProfileForBaseAssetRel(JsonString(*RigObject, TEXT("asset_path")));
        if (!Profile.IsEmpty())
        {
            return Profile;
        }
    }

    const TSharedPtr<FJsonObject>* BlueprintObject = nullptr;
    if (Manifest->TryGetObjectField(TEXT("blueprint"), BlueprintObject) && BlueprintObject && BlueprintObject->IsValid())
    {
        const FString Profile = ProfileForBaseAssetRel(JsonString(*BlueprintObject, TEXT("base_mesh_asset_path")));
        if (!Profile.IsEmpty())
        {
            return Profile;
        }
    }

    return ProfileForSkeleton(SharedSkeleton);
}

bool RetargetSetupForceRegenerate(const TSharedPtr<FJsonObject>& Manifest, const TSharedPtr<FJsonObject>& OverwriteObject)
{
    const bool bOverwriteCategory = JsonBool(OverwriteObject, TEXT("retarget_assets"), false);
    const TSharedPtr<FJsonObject>* RetargetObject = nullptr;
    if (!Manifest->TryGetObjectField(TEXT("retarget_setup"), RetargetObject) || !RetargetObject || !RetargetObject->IsValid())
    {
        return bOverwriteCategory;
    }
    return bOverwriteCategory || JsonBool(*RetargetObject, TEXT("overwrite_retarget_assets"), false);
}
}

void FWitcherImportContext::ImportRetargetSetup()
{
    const FString TargetProfile = RetargetTargetProfile(Manifest, SharedSkeleton.Get());
    if (TargetProfile.IsEmpty())
    {
        return;
    }

    FWitcherRetargetSetup::FOptions Options;
    Options.bForceRegenerate = RetargetSetupForceRegenerate(Manifest, OverwriteObject);
    Options.bSaveAssets = true;
    Options.TargetPreviewMesh = RetargetPreviewMesh.Get();
    Options.TargetProfile = TargetProfile;

    FWitcherRetargetSetup::FResult Result = FWitcherRetargetSetup::CreateOrRepairHumanBase(Options);
    for (const FString& AssetPath : Result.ImportedAssets)
    {
        ImportedAssets.AddUnique(AssetPath);
    }
    for (const FString& Warning : Result.Warnings)
    {
        AddWarning(Warning);
    }
    for (const FString& Error : Result.Errors)
    {
        AddError(Error);
    }
}
