#pragma once

#include "Modules/ModuleManager.h"

class FWitcherImportServer;

class FWitcherToolsImporterModule final : public IModuleInterface
{
public:
    virtual void StartupModule() override;
    virtual void ShutdownModule() override;

private:
    TUniquePtr<FWitcherImportServer> Server;
};
