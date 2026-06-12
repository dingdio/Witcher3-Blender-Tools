#include "WitcherToolsImporterModule.h"

#include "WitcherImportServer.h"

#define LOCTEXT_NAMESPACE "FWitcherToolsImporterModule"

DEFINE_LOG_CATEGORY_STATIC(LogWitcherToolsImporter, Log, All);

void FWitcherToolsImporterModule::StartupModule()
{
    Server = MakeUnique<FWitcherImportServer>();
    Server->Start();
}

void FWitcherToolsImporterModule::ShutdownModule()
{
    if (Server)
    {
        Server->Stop();
        Server.Reset();
    }
}

#undef LOCTEXT_NAMESPACE

IMPLEMENT_MODULE(FWitcherToolsImporterModule, WitcherToolsImporter)
