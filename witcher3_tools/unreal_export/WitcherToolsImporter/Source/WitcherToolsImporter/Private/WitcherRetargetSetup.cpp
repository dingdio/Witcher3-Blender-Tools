#include "WitcherRetargetSetup.h"

#include "Animation/Skeleton.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetToolsModule.h"
#include "ControlRig.h"
#include "ControlRigBlueprintFactory.h"
#include "ControlRigBlueprintLegacy.h"
#include "Engine/SkeletalMesh.h"
#include "FileHelpers.h"
#include "IAssetTools.h"
#include "ObjectTools.h"
#include "RetargetEditor/IKRetargetFactory.h"
#include "RetargetEditor/IKRetargeterController.h"
#include "RetargetEditor/IKRetargeterPoseGenerator.h"
#include "Retargeter/IKRetargetChainMapping.h"
#include "Retargeter/IKRetargetSettings.h"
#include "Retargeter/IKRetargeter.h"
#include "Rig/IKRigDefinition.h"
#include "RigEditor/IKRigController.h"
#include "RigEditor/IKRigDefinitionFactory.h"
#include "Rigs/RigHierarchyController.h"
#include "Runtime/Launch/Resources/Version.h"
#include "UObject/GarbageCollection.h"

DEFINE_LOG_CATEGORY_STATIC(LogWitcherRetargetSetup, Log, All);

#define WITCHER_HAS_UE58_IKRIG_ROOT_MOTION (ENGINE_MAJOR_VERSION > 5 || (ENGINE_MAJOR_VERSION == 5 && ENGINE_MINOR_VERSION >= 8))

namespace
{
constexpr const TCHAR* RetargetFolder = TEXT("/Game/RETARGET_");
constexpr const TCHAR* MannequinIKRigPath = TEXT("/Game/RETARGET_/IK_Mannequin.IK_Mannequin");
constexpr const TCHAR* WomanBaseMeshPath = TEXT("/Game/Witcher3/characters/base_entities/woman_base/woman_base.woman_base");
constexpr const TCHAR* WomanBaseSkeletonPath = TEXT("/Game/Witcher3/characters/base_entities/woman_base/woman_base_Skeleton.woman_base_Skeleton");
constexpr const TCHAR* ManBaseMeshPath = TEXT("/Game/Witcher3/characters/base_entities/man_base/man_base.man_base");
constexpr const TCHAR* ManBaseSkeletonPath = TEXT("/Game/Witcher3/characters/base_entities/man_base/man_base_Skeleton.man_base_Skeleton");
constexpr const TCHAR* QuinnMeshPath = TEXT("/Game/Characters/Mannequins/Meshes/SKM_Quinn_Simple.SKM_Quinn_Simple");
constexpr const TCHAR* MannyMeshPath = TEXT("/Game/Characters/Mannequins/Meshes/SKM_Manny_Simple.SKM_Manny_Simple");

struct FTargetProfile
{
    FString ProfileName;
    FString Label;
    FString IKRigAssetName;
    FString RetargeterAssetName;
    FString ControlRigAssetName;
    FString BaseMeshPath;
    FString BaseSkeletonPath;
};

FTargetProfile MakeTargetProfile(const FString& RequestedProfile)
{
    const FString ProfileName = RequestedProfile.ToLower();
    if (ProfileName == TEXT("man_base"))
    {
        return {
            TEXT("man_base"),
            TEXT("ManBase"),
            TEXT("IK_ManBase"),
            TEXT("RTG_MannequinToManBase"),
            TEXT("CR_ManBase"),
            ManBaseMeshPath,
            ManBaseSkeletonPath,
        };
    }

    return {
        TEXT("woman_base"),
        TEXT("WomanBase"),
        TEXT("IK_WomanBase"),
        TEXT("RTG_MannequinToWomanBase"),
        TEXT("CR_WomanBase"),
        WomanBaseMeshPath,
        WomanBaseSkeletonPath,
    };
}

FString RetargetAssetObjectPath(const FString& AssetName)
{
    return FString::Printf(TEXT("%s/%s.%s"), RetargetFolder, *AssetName, *AssetName);
}

struct FChainSpec
{
    FName ChainName;
    FName StartBone;
    FName EndBone;
    FName GoalName = NAME_None;
    FName GoalBone = NAME_None;
};

TArray<FChainSpec> WitcherHumanChainSpecs()
{
    return {
        {TEXT("Spine"), TEXT("torso"), TEXT("torso3")},
        {TEXT("Head"), TEXT("neck"), TEXT("head")},
        {TEXT("LeftClavicle"), TEXT("l_shoulder"), TEXT("l_shoulder")},
        {TEXT("RightClavicle"), TEXT("r_shoulder"), TEXT("r_shoulder")},
        {TEXT("LeftArm"), TEXT("l_bicep"), TEXT("l_hand"), TEXT("LeftHandIK"), TEXT("l_hand")},
        {TEXT("RightArm"), TEXT("r_bicep"), TEXT("r_hand"), TEXT("RightHandIK"), TEXT("r_hand")},
        {TEXT("LeftLeg"), TEXT("l_thigh"), TEXT("l_toe"), TEXT("LeftFootIK"), TEXT("l_foot")},
        {TEXT("RightLeg"), TEXT("r_thigh"), TEXT("r_toe"), TEXT("RightFootIK"), TEXT("r_foot")},
        {TEXT("LeftThumb"), TEXT("l_thumb_roll"), TEXT("l_thumb3")},
        {TEXT("RightThumb"), TEXT("r_thumb_roll"), TEXT("r_thumb3")},
        {TEXT("LeftIndex"), TEXT("l_index_knuckleRoll"), TEXT("l_index3")},
        {TEXT("RightIndex"), TEXT("r_index_knuckleRoll"), TEXT("r_index3")},
        {TEXT("LeftMiddle"), TEXT("l_middle_knuckleRoll"), TEXT("l_middle3")},
        {TEXT("RightMiddle"), TEXT("r_middle_knuckleRoll"), TEXT("r_middle3")},
        {TEXT("LeftRing"), TEXT("l_ring_knuckleRoll"), TEXT("l_ring3")},
        {TEXT("RightRing"), TEXT("r_ring_knuckleRoll"), TEXT("r_ring3")},
        {TEXT("LeftPinky"), TEXT("l_pinky_knuckleRoll"), TEXT("l_pinky3")},
        {TEXT("RightPinky"), TEXT("r_pinky_knuckleRoll"), TEXT("r_pinky3")},
        {TEXT("LeftWeapon"), TEXT("l_weapon"), TEXT("l_weapon")},
        {TEXT("RightWeapon"), TEXT("r_weapon"), TEXT("r_weapon")},
    };
}

TArray<FChainSpec> MannequinChainSpecs()
{
    return {
        {TEXT("Spine"), TEXT("spine_01"), TEXT("spine_05")},
        {TEXT("Head"), TEXT("neck_01"), TEXT("head")},
        {TEXT("LeftClavicle"), TEXT("clavicle_l"), TEXT("clavicle_l")},
        {TEXT("RightClavicle"), TEXT("clavicle_r"), TEXT("clavicle_r")},
        {TEXT("LeftArm"), TEXT("upperarm_l"), TEXT("hand_l"), TEXT("LeftHandIK"), TEXT("hand_l")},
        {TEXT("RightArm"), TEXT("upperarm_r"), TEXT("hand_r"), TEXT("RightHandIK"), TEXT("hand_r")},
        {TEXT("LeftLeg"), TEXT("thigh_l"), TEXT("ball_l"), TEXT("LeftFootIK"), TEXT("foot_l")},
        {TEXT("RightLeg"), TEXT("thigh_r"), TEXT("ball_r"), TEXT("RightFootIK"), TEXT("foot_r")},
        {TEXT("LeftThumb"), TEXT("thumb_01_l"), TEXT("thumb_03_l")},
        {TEXT("RightThumb"), TEXT("thumb_01_r"), TEXT("thumb_03_r")},
        {TEXT("LeftIndex"), TEXT("index_01_l"), TEXT("index_03_l")},
        {TEXT("RightIndex"), TEXT("index_01_r"), TEXT("index_03_r")},
        {TEXT("LeftMiddle"), TEXT("middle_01_l"), TEXT("middle_03_l")},
        {TEXT("RightMiddle"), TEXT("middle_01_r"), TEXT("middle_03_r")},
        {TEXT("LeftRing"), TEXT("ring_01_l"), TEXT("ring_03_l")},
        {TEXT("RightRing"), TEXT("ring_01_r"), TEXT("ring_03_r")},
        {TEXT("LeftPinky"), TEXT("pinky_01_l"), TEXT("pinky_03_l")},
        {TEXT("RightPinky"), TEXT("pinky_01_r"), TEXT("pinky_03_r")},
    };
}

template <typename T>
T* LoadAsset(const FString& Path)
{
    return Cast<T>(StaticLoadObject(T::StaticClass(), nullptr, *Path, nullptr, LOAD_NoWarn | LOAD_Quiet));
}

template <typename T>
T* LoadAsset(const TCHAR* Path)
{
    return LoadAsset<T>(FString(Path));
}

void AddUniqueString(TArray<FString>& Values, const FString& Value)
{
    if (!Value.IsEmpty())
    {
        Values.AddUnique(Value);
    }
}

void AddWarning(FWitcherRetargetSetup::FResult& Result, const FString& Message)
{
    Result.Warnings.Add(Message);
    UE_LOG(LogWitcherRetargetSetup, Warning, TEXT("%s"), *Message);
}

void AddError(FWitcherRetargetSetup::FResult& Result, const FString& Message)
{
    Result.Errors.Add(Message);
    UE_LOG(LogWitcherRetargetSetup, Error, TEXT("%s"), *Message);
}

bool BoneExists(const USkeletalMesh* Mesh, const FName BoneName)
{
    return Mesh && BoneName != NAME_None && Mesh->GetRefSkeleton().FindBoneIndex(BoneName) != INDEX_NONE;
}

TSet<FName> BoneNameSet(const USkeletalMesh* Mesh)
{
    TSet<FName> Names;
    if (!Mesh)
    {
        return Names;
    }
    const FReferenceSkeleton& RefSkeleton = Mesh->GetRefSkeleton();
    for (int32 Index = 0; Index < RefSkeleton.GetNum(); ++Index)
    {
        Names.Add(RefSkeleton.GetBoneName(Index));
    }
    return Names;
}

void AddMissingBone(TArray<FName>& MissingBones, const TSet<FName>& TargetBones, const FName BoneName)
{
    if (BoneName != NAME_None && !TargetBones.Contains(BoneName))
    {
        MissingBones.AddUnique(BoneName);
    }
}

FString JoinBoneNames(const TArray<FName>& Bones)
{
    TArray<FString> Names;
    Names.Reserve(Bones.Num());
    for (const FName Bone : Bones)
    {
        Names.Add(Bone.ToString());
    }
    Names.Sort();
    return FString::Join(Names, TEXT(", "));
}

bool ValidateWitcherHumanSkeleton(
    USkeletalMesh* TargetMesh,
    FWitcherRetargetSetup::FResult& Result,
    const TCHAR* MeshLabel,
    const bool bExpectBaseBoneCount)
{
    if (!TargetMesh)
    {
        AddError(Result, FString::Printf(TEXT("Witcher human retarget setup: missing %s mesh."), MeshLabel));
        return false;
    }

    const int32 TargetBoneCount = TargetMesh->GetRefSkeleton().GetNum();
    if (bExpectBaseBoneCount && TargetBoneCount != 94)
    {
        AddWarning(Result, FString::Printf(
            TEXT("Witcher human retarget setup: expected the default 94-bone Witcher human base mesh, got %d bones. Extra bones will not be added as retarget-driving chains."),
            TargetBoneCount));
    }

    const TSet<FName> TargetBones = BoneNameSet(TargetMesh);
    TArray<FName> MissingBones;

    AddMissingBone(MissingBones, TargetBones, TEXT("Root"));
    AddMissingBone(MissingBones, TargetBones, TEXT("Trajectory"));
    AddMissingBone(MissingBones, TargetBones, TEXT("pelvis"));

    for (const FChainSpec& Chain : WitcherHumanChainSpecs())
    {
        AddMissingBone(MissingBones, TargetBones, Chain.StartBone);
        AddMissingBone(MissingBones, TargetBones, Chain.EndBone);
        AddMissingBone(MissingBones, TargetBones, Chain.GoalBone);
    }

    if (!MissingBones.IsEmpty())
    {
        AddError(Result, FString::Printf(
            TEXT("Witcher human retarget setup: %s is missing required Witcher human animation bones: %s"),
            MeshLabel,
            *JoinBoneNames(MissingBones)));
        return false;
    }

    return true;
}

bool DeleteGeneratedAssetIfForced(const FString& AssetPath, const bool bForce, FWitcherRetargetSetup::FResult& Result)
{
    if (!bForce)
    {
        return true;
    }

    UObject* Existing = LoadAsset<UObject>(AssetPath);
    if (!Existing)
    {
        return true;
    }

    TArray<UObject*> ObjectsToDelete;
    ObjectsToDelete.Add(Existing);
    if (ObjectTools::DeleteObjectsUnchecked(ObjectsToDelete) <= 0)
    {
        AddError(Result, FString::Printf(TEXT("Could not delete existing generated asset: %s"), *AssetPath));
        return false;
    }

    CollectGarbage(GARBAGE_COLLECTION_KEEPFLAGS);
    return true;
}

void TrackAsset(UObject* Asset, TSet<UPackage*>& Packages, FWitcherRetargetSetup::FResult& Result)
{
    if (!Asset)
    {
        return;
    }
    Asset->MarkPackageDirty();
    AddUniqueString(Result.ImportedAssets, Asset->GetPathName());
    if (UPackage* Package = Asset->GetOutermost())
    {
        Packages.Add(Package);
    }
}

USkeletalMesh* LoadMannequinMesh(FWitcherRetargetSetup::FResult& Result)
{
    if (USkeletalMesh* Quinn = LoadAsset<USkeletalMesh>(QuinnMeshPath))
    {
        return Quinn;
    }
    if (USkeletalMesh* Manny = LoadAsset<USkeletalMesh>(MannyMeshPath))
    {
        AddWarning(Result, TEXT("SKM_Quinn_Simple was not found; using SKM_Manny_Simple for IK_Mannequin."));
        return Manny;
    }
    AddError(Result, TEXT("Witcher human retarget setup: neither SKM_Quinn_Simple nor SKM_Manny_Simple could be loaded."));
    return nullptr;
}

bool RebindIKRig(
    UIKRigDefinition* IKRig,
    USkeletalMesh* PreviewMesh,
    const FName PelvisBone,
    const FName RootMotionBone,
    FWitcherRetargetSetup::FResult& Result)
{
    if (!IKRig || !PreviewMesh)
    {
        return false;
    }
    UIKRigController* Controller = UIKRigController::GetController(IKRig);
    if (!Controller)
    {
        AddError(Result, FString::Printf(TEXT("Could not get IK Rig controller for %s"), *IKRig->GetPathName()));
        return false;
    }
    if (!Controller->SetSkeletalMesh(PreviewMesh))
    {
        AddError(Result, FString::Printf(
            TEXT("Could not bind preview mesh '%s' to IK Rig '%s'. Check missing goals/chains/bone settings."),
            *PreviewMesh->GetPathName(),
            *IKRig->GetPathName()));
        return false;
    }
    if (BoneExists(PreviewMesh, PelvisBone))
    {
        Controller->SetRetargetRoot(PelvisBone);
    }
    else
    {
        AddWarning(Result, FString::Printf(TEXT("%s has no pelvis bone '%s'."), *PreviewMesh->GetName(), *PelvisBone.ToString()));
    }
#if WITCHER_HAS_UE58_IKRIG_ROOT_MOTION
    if (BoneExists(PreviewMesh, RootMotionBone))
    {
        Controller->SetRootMotionBone(RootMotionBone);
    }
    else
    {
        AddWarning(Result, FString::Printf(TEXT("%s has no root-motion bone '%s'."), *PreviewMesh->GetName(), *RootMotionBone.ToString()));
    }
#else
    if (!BoneExists(PreviewMesh, RootMotionBone))
    {
        AddWarning(Result, FString::Printf(TEXT("%s has no root-motion bone '%s'."), *PreviewMesh->GetName(), *RootMotionBone.ToString()));
    }
#endif
    return true;
}

void RemoveSolversAndGoals(UIKRigController* Controller)
{
    if (!Controller)
    {
        return;
    }

    for (int32 SolverIndex = Controller->GetNumSolvers() - 1; SolverIndex >= 0; --SolverIndex)
    {
        Controller->RemoveSolver(SolverIndex);
    }

    TArray<FName> GoalNames;
    for (const UIKRigEffectorGoal* Goal : Controller->GetAllGoals())
    {
        if (Goal)
        {
            GoalNames.Add(Goal->GoalName);
        }
    }
    for (const FName GoalName : GoalNames)
    {
        Controller->RemoveGoal(GoalName);
    }
}

TSet<FName> ChainNameSet(const TArray<FChainSpec>& Chains)
{
    TSet<FName> Names;
    for (const FChainSpec& Chain : Chains)
    {
        Names.Add(Chain.ChainName);
    }
    return Names;
}

void RemoveUnprofiledChains(UIKRigController* Controller, const TSet<FName>& ProfileChains)
{
    TArray<FName> ExistingChains;
    for (const FBoneChain& Chain : Controller->GetRetargetChains())
    {
        ExistingChains.Add(Chain.ChainName);
    }
    for (const FName ChainName : ExistingChains)
    {
        if (!ProfileChains.Contains(ChainName))
        {
            Controller->RemoveRetargetChain(ChainName);
        }
    }
}

bool HasRetargetChain(UIKRigController* Controller, const FName ChainName)
{
    for (const FBoneChain& Chain : Controller->GetRetargetChains())
    {
        if (Chain.ChainName == ChainName)
        {
            return true;
        }
    }
    return false;
}

void EnsureChain(
    UIKRigController* Controller,
    USkeletalMesh* TargetMesh,
    const FString& RigLabel,
    const FName ChainName,
    const FName StartBone,
    const FName EndBone,
    const FName GoalName,
    FWitcherRetargetSetup::FResult& Result)
{
    if (!BoneExists(TargetMesh, StartBone) || !BoneExists(TargetMesh, EndBone))
    {
        AddWarning(Result, FString::Printf(
            TEXT("%s chain '%s' skipped; missing '%s' or '%s'."),
            *RigLabel,
            *ChainName.ToString(),
            *StartBone.ToString(),
            *EndBone.ToString()));
        return;
    }

    if (HasRetargetChain(Controller, ChainName))
    {
        Controller->SetRetargetChainStartBone(ChainName, StartBone);
        Controller->SetRetargetChainEndBone(ChainName, EndBone);
        Controller->SetRetargetChainGoal(ChainName, GoalName);
    }
    else
    {
        Controller->AddRetargetChain(ChainName, StartBone, EndBone, GoalName);
    }
}

bool ConfigureIKRigFromProfile(
    UIKRigDefinition* IKRig,
    USkeletalMesh* Mesh,
    const FString& RigLabel,
    const FName PelvisBone,
    const FName RootMotionBone,
    const TArray<FChainSpec>& Chains,
    FWitcherRetargetSetup::FResult& Result)
{
    if (!RebindIKRig(IKRig, Mesh, PelvisBone, RootMotionBone, Result))
    {
        return false;
    }

    UIKRigController* Controller = UIKRigController::GetController(IKRig);
    if (!Controller)
    {
        AddError(Result, FString::Printf(TEXT("Could not get IK Rig controller for %s"), *IKRig->GetPathName()));
        return false;
    }

    RemoveUnprofiledChains(Controller, ChainNameSet(Chains));
    RemoveSolversAndGoals(Controller);
    for (const FChainSpec& Chain : Chains)
    {
        EnsureChain(Controller, Mesh, RigLabel, Chain.ChainName, Chain.StartBone, Chain.EndBone, NAME_None, Result);
    }
    Controller->SortRetargetChains();
    return true;
}

UIKRigDefinition* EnsureTargetIKRig(
    const FTargetProfile& Profile,
    USkeletalMesh* TargetMesh,
    const bool bForceRegenerate,
    TSet<UPackage*>& Packages,
    FWitcherRetargetSetup::FResult& Result)
{
    const FString IKRigPath = RetargetAssetObjectPath(Profile.IKRigAssetName);
    if (!DeleteGeneratedAssetIfForced(IKRigPath, bForceRegenerate, Result))
    {
        return nullptr;
    }

    UIKRigDefinition* TargetRig = LoadAsset<UIKRigDefinition>(IKRigPath);
    if (!TargetRig)
    {
        TargetRig = UIKRigDefinitionFactory::CreateNewIKRigAsset(RetargetFolder, *Profile.IKRigAssetName);
    }
    if (!TargetRig)
    {
        AddError(Result, FString::Printf(TEXT("Could not create /Game/RETARGET_/%s."), *Profile.IKRigAssetName));
        return nullptr;
    }

    if (ConfigureIKRigFromProfile(TargetRig, TargetMesh, Profile.IKRigAssetName, TEXT("pelvis"), TEXT("Trajectory"), WitcherHumanChainSpecs(), Result))
    {
        TrackAsset(TargetRig, Packages, Result);
    }
    return TargetRig;
}

UIKRigDefinition* EnsureMannequinIKRig(
    USkeletalMesh* SourceMesh,
    const bool bForceRegenerate,
    TSet<UPackage*>& Packages,
    FWitcherRetargetSetup::FResult& Result)
{
    if (!DeleteGeneratedAssetIfForced(MannequinIKRigPath, bForceRegenerate, Result))
    {
        return nullptr;
    }

    UIKRigDefinition* MannequinRig = LoadAsset<UIKRigDefinition>(MannequinIKRigPath);
    if (!MannequinRig)
    {
        MannequinRig = UIKRigDefinitionFactory::CreateNewIKRigAsset(RetargetFolder, TEXT("IK_Mannequin"));
    }
    if (!MannequinRig)
    {
        AddError(Result, TEXT("Could not create /Game/RETARGET_/IK_Mannequin."));
        return nullptr;
    }

    if (ConfigureIKRigFromProfile(MannequinRig, SourceMesh, TEXT("IK_Mannequin"), TEXT("pelvis"), TEXT("root"), MannequinChainSpecs(), Result))
    {
        TrackAsset(MannequinRig, Packages, Result);
    }
    return MannequinRig;
}

UIKRetargeter* EnsureRetargeter(
    const FTargetProfile& Profile,
    UIKRigDefinition* SourceRig,
    UIKRigDefinition* TargetRig,
    USkeletalMesh* SourceMesh,
    USkeletalMesh* TargetMesh,
    const bool bForceRegenerate,
    TSet<UPackage*>& Packages,
    FWitcherRetargetSetup::FResult& Result)
{
    const FString RetargeterPath = RetargetAssetObjectPath(Profile.RetargeterAssetName);
    if (!DeleteGeneratedAssetIfForced(RetargeterPath, bForceRegenerate, Result))
    {
        return nullptr;
    }

    UIKRetargeter* Retargeter = LoadAsset<UIKRetargeter>(RetargeterPath);
    if (!Retargeter)
    {
        FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
        UIKRetargetFactory* Factory = NewObject<UIKRetargetFactory>();
        Retargeter = Cast<UIKRetargeter>(
            AssetToolsModule.Get().CreateAsset(
                *Profile.RetargeterAssetName,
                RetargetFolder,
                UIKRetargeter::StaticClass(),
                Factory));
    }
    if (!Retargeter)
    {
        AddError(Result, FString::Printf(TEXT("Could not create /Game/RETARGET_/%s."), *Profile.RetargeterAssetName));
        return nullptr;
    }

    UIKRetargeterController* Controller = UIKRetargeterController::GetController(Retargeter);
    if (!Controller)
    {
        AddError(Result, TEXT("Could not get IK Retargeter controller."));
        return nullptr;
    }

    Controller->SetIKRig(ERetargetSourceOrTarget::Source, SourceRig);
    Controller->SetIKRig(ERetargetSourceOrTarget::Target, TargetRig);
    Controller->SetPreviewMesh(ERetargetSourceOrTarget::Source, SourceMesh);
    Controller->SetPreviewMesh(ERetargetSourceOrTarget::Target, TargetMesh);
    Controller->RemoveAllOps();
    Controller->AddDefaultOps();
    Controller->AssignIKRigToAllOps(ERetargetSourceOrTarget::Source, SourceRig);
    Controller->AssignIKRigToAllOps(ERetargetSourceOrTarget::Target, TargetRig);
    Controller->AutoMapChains(EAutoMapChainType::Exact, true);

    TSet<FName> SourceChainNames;
    for (const FName ChainName : SourceRig->GetRetargetChainNames())
    {
        SourceChainNames.Add(ChainName);
    }
    for (const FName ChainName : TargetRig->GetRetargetChainNames())
    {
        if (SourceChainNames.Contains(ChainName))
        {
            Controller->SetSourceChain(ChainName, ChainName);
        }
    }

    // Align mapped chains before auto-retargeting.
    Controller->AutoAlignAllBones(ERetargetSourceOrTarget::Target, ERetargetAutoAlignMethod::ChainToChain);

    Controller->CleanAsset();
    TrackAsset(Retargeter, Packages, Result);
    return Retargeter;
}

UControlRigBlueprint* EnsureControlRig(
    const FTargetProfile& Profile,
    USkeletalMesh* TargetMesh,
    USkeleton* TargetSkeleton,
    const bool bForceRegenerate,
    TSet<UPackage*>& Packages,
    FWitcherRetargetSetup::FResult& Result)
{
    const FString ControlRigPath = RetargetAssetObjectPath(Profile.ControlRigAssetName);
    if (!DeleteGeneratedAssetIfForced(ControlRigPath, bForceRegenerate, Result))
    {
        return nullptr;
    }

    UControlRigBlueprint* ControlRig = LoadAsset<UControlRigBlueprint>(ControlRigPath);
    if (!ControlRig)
    {
        FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
        UControlRigBlueprintFactory* Factory = NewObject<UControlRigBlueprintFactory>();
        Factory->ParentClass = UControlRig::StaticClass();
        ControlRig = Cast<UControlRigBlueprint>(
            AssetToolsModule.Get().CreateAsset(
                *Profile.ControlRigAssetName,
                RetargetFolder,
                UControlRigBlueprint::StaticClass(),
                Factory));
    }
    if (!ControlRig)
    {
        AddError(Result, FString::Printf(TEXT("Could not create /Game/RETARGET_/%s."), *Profile.ControlRigAssetName));
        return nullptr;
    }

    if (URigHierarchyController* Controller = ControlRig->GetHierarchyController())
    {
        Controller->ImportBones(TargetMesh, NAME_None, true, true, false, false);
        Controller->ImportCurvesFromSkeletalMesh(TargetMesh, NAME_None, false, false);
    }
    else
    {
        AddWarning(Result, FString::Printf(TEXT("%s was created but its hierarchy controller was not available."), *Profile.ControlRigAssetName));
    }

    ControlRig->GetSourceHierarchyImport() = TargetSkeleton;
    ControlRig->GetSourceCurveImport() = TargetSkeleton;
    ControlRig->SetPreviewMesh(TargetMesh);
    ControlRig->PropagateHierarchyFromBPToInstances();
    ControlRig->RecompileVM();

    TrackAsset(ControlRig, Packages, Result);
    return ControlRig;
}

void SavePackages(const TSet<UPackage*>& Packages, FWitcherRetargetSetup::FResult& Result)
{
    if (Packages.IsEmpty())
    {
        return;
    }
    TArray<UPackage*> PackageArray = Packages.Array();
    if (!UEditorLoadingAndSavingUtils::SavePackages(PackageArray, false))
    {
        AddWarning(Result, TEXT("One or more generated retarget packages could not be saved."));
    }
}
}

FWitcherRetargetSetup::FResult FWitcherRetargetSetup::CreateOrRepairHumanBase(const FOptions& Options)
{
    FResult Result;
    TSet<UPackage*> PackagesToSave;
    const FTargetProfile Profile = MakeTargetProfile(Options.TargetProfile);

    USkeletalMesh* BaseMesh = LoadAsset<USkeletalMesh>(Profile.BaseMeshPath);
    USkeleton* BaseSkeleton = LoadAsset<USkeleton>(Profile.BaseSkeletonPath);

    if (!BaseMesh)
    {
        AddError(Result, FString::Printf(TEXT("%s mesh not found: %s"), *Profile.Label, *Profile.BaseMeshPath));
    }
    if (!BaseSkeleton)
    {
        AddError(Result, FString::Printf(TEXT("%s skeleton not found: %s"), *Profile.Label, *Profile.BaseSkeletonPath));
    }
    if (!Result.Errors.IsEmpty())
    {
        return Result;
    }

    if (!ValidateWitcherHumanSkeleton(
            BaseMesh,
            Result,
            *Profile.ProfileName,
            /*bExpectBaseBoneCount=*/true))
    {
        return Result;
    }

    USkeletalMesh* TargetPreviewMesh = Options.TargetPreviewMesh ? Options.TargetPreviewMesh : BaseMesh;
    // Keep recoverable preview validation errors out of the final result.
    FResult PreviewProbe;
    if (!ValidateWitcherHumanSkeleton(
            TargetPreviewMesh,
            PreviewProbe,
            TEXT("target preview mesh"),
            /*bExpectBaseBoneCount=*/false))
    {
        if (TargetPreviewMesh != BaseMesh)
        {
            AddWarning(Result, FString::Printf(
                TEXT("%s retarget setup: preview mesh '%s' is not a usable Witcher human skeleton; using %s instead."),
                *Profile.Label,
                *TargetPreviewMesh->GetPathName(),
                *Profile.ProfileName));
            TargetPreviewMesh = BaseMesh;
        }
        if (!ValidateWitcherHumanSkeleton(
                TargetPreviewMesh,
                Result,
                *Profile.ProfileName,
                /*bExpectBaseBoneCount=*/true))
        {
            return Result;
        }
    }

    UIKRigDefinition* TargetRig = EnsureTargetIKRig(
        Profile,
        TargetPreviewMesh,
        Options.bForceRegenerate,
        PackagesToSave,
        Result);
    USkeletalMesh* MannequinMesh = LoadMannequinMesh(Result);
    UIKRigDefinition* MannequinRig = MannequinMesh
        ? EnsureMannequinIKRig(MannequinMesh, Options.bForceRegenerate, PackagesToSave, Result)
        : nullptr;

    if (TargetRig && MannequinRig && MannequinMesh)
    {
        EnsureRetargeter(
            Profile,
            MannequinRig,
            TargetRig,
            MannequinMesh,
            TargetPreviewMesh,
            Options.bForceRegenerate,
            PackagesToSave,
            Result);
    }

    EnsureControlRig(
        Profile,
        TargetPreviewMesh,
        TargetPreviewMesh->GetSkeleton() ? TargetPreviewMesh->GetSkeleton() : BaseSkeleton,
        Options.bForceRegenerate,
        PackagesToSave,
        Result);

    if (Options.bSaveAssets)
    {
        SavePackages(PackagesToSave, Result);
    }

    Result.bSuccess = Result.Errors.IsEmpty();
    return Result;
}

FWitcherRetargetSetup::FResult FWitcherRetargetSetup::CreateOrRepairWomanBase(const FOptions& Options)
{
    FOptions WomanOptions = Options;
    WomanOptions.TargetProfile = TEXT("woman_base");
    return CreateOrRepairHumanBase(WomanOptions);
}
