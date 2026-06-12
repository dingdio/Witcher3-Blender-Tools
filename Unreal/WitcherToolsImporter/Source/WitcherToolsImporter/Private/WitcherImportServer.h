#pragma once

#include "CoreMinimal.h"
#include "HAL/Runnable.h"

class FSocket;
class FRunnableThread;

class FWitcherImportServer final : public FRunnable
{
public:
    FWitcherImportServer();
    virtual ~FWitcherImportServer() override;

    void Start();
    void Stop();

    virtual bool Init() override;
    virtual uint32 Run() override;
    virtual void Exit() override;

private:
    bool ReceiveExact(FSocket* ClientSocket, TArray<uint8>& OutData, int32 Size) const;
    bool SendResponse(FSocket* ClientSocket, const FString& ResponseJson) const;
    void HandleClient(FSocket* ClientSocket) const;

    FString BindAddress = TEXT("127.0.0.1");
    int32 Port = 40777;
    FSocket* ListenSocket = nullptr;
    FRunnableThread* Thread = nullptr;
    FThreadSafeBool bRunning = false;
};
