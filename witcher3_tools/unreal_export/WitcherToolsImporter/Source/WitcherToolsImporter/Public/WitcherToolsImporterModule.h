#pragma once

#include "Modules/ModuleManager.h"

class FWitcherImportServer;

class FWitcherToolsImporterModule final : public IModuleInterface
{
public:
    virtual void StartupModule() override;
    virtual void ShutdownModule() override;

private:
    void RegisterMenus();
    void OnLoadW2LAroundCamera();
    void OnSendAnimationToBlender();
    void OnCreateWomanBaseRetargetSetup();

    TUniquePtr<FWitcherImportServer> Server;
};
