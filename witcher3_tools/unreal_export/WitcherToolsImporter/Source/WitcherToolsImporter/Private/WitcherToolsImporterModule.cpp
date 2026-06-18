#include "WitcherToolsImporterModule.h"

#include "Async/Async.h"
#include "Dom/JsonObject.h"
#include "Framework/Notifications/NotificationManager.h"
#include "LevelEditorViewport.h"
#include "Serialization/JsonSerializer.h"
#include "ToolMenus.h"
#include "Widgets/Notifications/SNotificationList.h"
#include "WitcherBlenderClient.h"
#include "WitcherImportServer.h"
#include "WitcherVisibilityPanel.h"

#define LOCTEXT_NAMESPACE "FWitcherToolsImporterModule"

DEFINE_LOG_CATEGORY_STATIC(LogWitcherToolsImporter, Log, All);

namespace
{
const TCHAR* BlenderHost = TEXT("127.0.0.1");
constexpr int32 BlenderPort = 40778;
}

void FWitcherToolsImporterModule::StartupModule()
{
    Server = MakeUnique<FWitcherImportServer>();
    Server->Start();

    WitcherVisibilityPanel::Register();

    UToolMenus::RegisterStartupCallback(
        FSimpleMulticastDelegate::FDelegate::CreateRaw(this, &FWitcherToolsImporterModule::RegisterMenus));
}

void FWitcherToolsImporterModule::ShutdownModule()
{
    UToolMenus::UnRegisterStartupCallback(this);
    UToolMenus::UnregisterOwner(this);

    WitcherVisibilityPanel::Unregister();

    if (Server)
    {
        Server->Stop();
        Server.Reset();
    }
}

void FWitcherToolsImporterModule::RegisterMenus()
{
    FToolMenuOwnerScoped OwnerScoped(this);

    UToolMenu* ToolsMenu = UToolMenus::Get()->ExtendMenu(TEXT("LevelEditor.MainMenu.Tools"));
    FToolMenuSection& Section = ToolsMenu->FindOrAddSection(TEXT("WitcherTools"), LOCTEXT("WitcherTools", "Witcher Tools"));
    Section.AddMenuEntry(
        TEXT("LoadW2LAroundCamera"),
        LOCTEXT("LoadW2LAroundCamera", "Load W2L Around Camera"),
        LOCTEXT("LoadW2LAroundCameraTip", "Ask Blender to export and import the .w2l layers near the viewport camera"),
        FSlateIcon(),
        FUIAction(FExecuteAction::CreateRaw(this, &FWitcherToolsImporterModule::OnLoadW2LAroundCamera)));
    Section.AddMenuEntry(
        TEXT("LayerVisibility"),
        LOCTEXT("LayerVisibility", "Layer Visibility"),
        LOCTEXT("LayerVisibilityTip", "Open the panel to show/hide imported layer groups (collision, meshes, lights)"),
        FSlateIcon(),
        FUIAction(FExecuteAction::CreateStatic(&WitcherVisibilityPanel::OpenTab)));
}

void FWitcherToolsImporterModule::OnLoadW2LAroundCamera()
{
    const FVector Camera = GCurrentLevelEditingViewportClient
        ? GCurrentLevelEditingViewportClient->GetViewLocation()
        : FVector::ZeroVector;

    TSharedRef<FJsonObject> Request = MakeShared<FJsonObject>();
    Request->SetStringField(TEXT("command"), TEXT("load_w2l_around_camera"));
    TArray<TSharedPtr<FJsonValue>> CameraArray;
    CameraArray.Add(MakeShared<FJsonValueNumber>(Camera.X));
    CameraArray.Add(MakeShared<FJsonValueNumber>(Camera.Y));
    CameraArray.Add(MakeShared<FJsonValueNumber>(Camera.Z));
    Request->SetArrayField(TEXT("camera_unreal"), CameraArray);

    FString RequestJson;
    TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&RequestJson);
    FJsonSerializer::Serialize(Request, Writer);

    Async(EAsyncExecution::Thread, [RequestJson]()
    {
        FString Response;
        const bool bOk = FWitcherBlenderClient::Send(BlenderHost, BlenderPort, RequestJson, Response);
        const FString Message = bOk ? Response : FString::Printf(TEXT("Could not reach Blender listener at %s:%d"), BlenderHost, BlenderPort);

        AsyncTask(ENamedThreads::GameThread, [Message]()
        {
            FNotificationInfo Info(FText::FromString(FString::Printf(TEXT("Witcher: %s"), *Message)));
            Info.ExpireDuration = 6.0f;
            FSlateNotificationManager::Get().AddNotification(Info);
        });
    });
}

#undef LOCTEXT_NAMESPACE

IMPLEMENT_MODULE(FWitcherToolsImporterModule, WitcherToolsImporter)
