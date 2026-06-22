#pragma once

#include "Commandlets/Commandlet.h"
#include "WitcherRetargetSetupCommandlet.generated.h"

UCLASS()
class UWitcherRetargetSetupCommandlet : public UCommandlet
{
    GENERATED_BODY()

public:
    UWitcherRetargetSetupCommandlet();

    virtual int32 Main(const FString& Params) override;
};
