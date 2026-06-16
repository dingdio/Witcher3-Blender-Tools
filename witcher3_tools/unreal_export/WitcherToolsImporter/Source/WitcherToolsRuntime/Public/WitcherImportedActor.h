#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "WitcherImportedActor.generated.h"

class USkeletalMeshComponent;

UCLASS(Blueprintable)
class WITCHERTOOLSRUNTIME_API AWitcherImportedActor : public AActor
{
    GENERATED_BODY()

public:
    explicit AWitcherImportedActor(const FObjectInitializer& ObjectInitializer);

    virtual void OnConstruction(const FTransform& Transform) override;
    virtual void PostInitializeComponents() override;

    UFUNCTION(BlueprintCallable, Category = "Witcher")
    void ConfigureModularSkeletalComponents();

    // Shared bounds/tick setup for the driver (base) and follower skeletal
    // components of a modular Witcher character. Used both at import time (to
    // seed the blueprint's component templates) and at runtime construction, so
    // the serialized defaults and the live actor can never drift apart.
    static void ConfigureBaseComponent(USkeletalMeshComponent* Component, bool bUpdateAnimationInEditor);
    static void ConfigureFollowerComponent(
        USkeletalMeshComponent* Component, USkeletalMeshComponent* LeaderPose, bool bUpdateAnimationInEditor);
};
