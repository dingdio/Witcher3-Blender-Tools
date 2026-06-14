#pragma once

#include "Commandlets/Commandlet.h"
#include "WitcherImportBundleCommandlet.generated.h"

UCLASS()
class UWitcherImportBundleCommandlet : public UCommandlet
{
    GENERATED_BODY()

public:
    UWitcherImportBundleCommandlet();

    virtual int32 Main(const FString& Params) override;
};
