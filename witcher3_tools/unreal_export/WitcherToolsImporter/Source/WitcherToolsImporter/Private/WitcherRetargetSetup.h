#pragma once

#include "CoreMinimal.h"

class USkeletalMesh;

class FWitcherRetargetSetup
{
public:
    struct FOptions
    {
        bool bForceRegenerate = false;
        bool bSaveAssets = true;
        FString TargetProfile = TEXT("woman_base");
        USkeletalMesh* TargetPreviewMesh = nullptr;
    };

    struct FResult
    {
        bool bSuccess = false;
        TArray<FString> ImportedAssets;
        TArray<FString> Warnings;
        TArray<FString> Errors;
    };

    static FResult CreateOrRepairWomanBase(const FOptions& Options);
    static FResult CreateOrRepairHumanBase(const FOptions& Options);
};
