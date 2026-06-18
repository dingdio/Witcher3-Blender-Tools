#pragma once

#include "CoreMinimal.h"

class FWitcherBlenderClient
{
public:
    static bool Send(const FString& Host, int32 Port, const FString& RequestJson, FString& OutResponseJson);
};
