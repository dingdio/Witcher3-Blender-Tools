#include "WitcherImportedActor.h"

#include "Components/SkeletalMeshComponent.h"
#include "Engine/World.h"

AWitcherImportedActor::AWitcherImportedActor(const FObjectInitializer& ObjectInitializer)
    : Super(ObjectInitializer)
{
    PrimaryActorTick.bCanEverTick = false;
}

void AWitcherImportedActor::OnConstruction(const FTransform& Transform)
{
    Super::OnConstruction(Transform);
    ConfigureModularSkeletalComponents();
}

void AWitcherImportedActor::PostInitializeComponents()
{
    Super::PostInitializeComponents();
    ConfigureModularSkeletalComponents();
}

void AWitcherImportedActor::ConfigureBaseComponent(USkeletalMeshComponent* Component, bool bUpdateAnimationInEditor)
{
    if (!Component)
    {
        return;
    }
    Component->bUseAttachParentBound = false;
    Component->bUseBoundsFromLeaderPoseComponent = false;
    Component->bSkipBoundsUpdateWhenInterpolating = false;
    Component->VisibilityBasedAnimTickOption = EVisibilityBasedAnimTickOption::AlwaysTickPoseAndRefreshBones;
    Component->bPropagateCurvesToFollowers = true;
#if WITH_EDITOR
    Component->SetUpdateAnimationInEditor(bUpdateAnimationInEditor);
#endif
    Component->InvalidateCachedBounds();
}

void AWitcherImportedActor::ConfigureFollowerComponent(
    USkeletalMeshComponent* Component, USkeletalMeshComponent* LeaderPose, bool bUpdateAnimationInEditor)
{
    if (!Component)
    {
        return;
    }
    // The driver (base) can be a tiny skeleton-carrier mesh. Visible body parts
    // must not inherit its bounds through the leader-pose path or the normal
    // attach-parent bounds path, especially in the editor viewport.
    Component->bUseAttachParentBound = false;
    Component->bUseBoundsFromLeaderPoseComponent = false;
    Component->bComponentUseFixedSkelBounds = false;
    Component->bSkipBoundsUpdateWhenInterpolating = false;
    Component->VisibilityBasedAnimTickOption = EVisibilityBasedAnimTickOption::AlwaysTickPoseAndRefreshBones;
#if WITH_EDITOR
    Component->SetUpdateAnimationInEditor(bUpdateAnimationInEditor);
#endif
    Component->SetLeaderPoseComponent(LeaderPose, false);
    Component->InvalidateCachedBounds();
}

void AWitcherImportedActor::ConfigureModularSkeletalComponents()
{
    USkeletalMeshComponent* BaseComponent = Cast<USkeletalMeshComponent>(GetRootComponent());

    TArray<USkeletalMeshComponent*> SkeletalComponents;
    GetComponents(SkeletalComponents);
    if (!BaseComponent)
    {
        for (USkeletalMeshComponent* Component : SkeletalComponents)
        {
            if (Component && Component->GetSkeletalMeshAsset())
            {
                BaseComponent = Component;
                break;
            }
        }
    }
    if (!BaseComponent)
    {
        return;
    }

#if WITH_EDITOR
    const bool bPlainEditorWorld = GetWorld() && GetWorld()->WorldType == EWorldType::Editor;
#else
    const bool bPlainEditorWorld = false;
#endif
    const bool bUpdateAnimationInEditor = !bPlainEditorWorld;

    ConfigureBaseComponent(BaseComponent, bUpdateAnimationInEditor);

    for (USkeletalMeshComponent* Component : SkeletalComponents)
    {
        if (!Component || Component == BaseComponent || !Component->GetSkeletalMeshAsset())
        {
            continue;
        }
        // Level-editor construction reruns while an actor is dragged. UE can
        // briefly render follower components against stale leader bone buffers
        // in that path, which looks like exploding triangles. Keep editor
        // placement static (no leader); PIE/game and blueprint preview still
        // use the leader-pose setup.
        USkeletalMeshComponent* LeaderPose = bPlainEditorWorld ? nullptr : BaseComponent;
        ConfigureFollowerComponent(Component, LeaderPose, bUpdateAnimationInEditor);
    }
}
