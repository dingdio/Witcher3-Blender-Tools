#include "WitcherImportServer.h"

#include "Async/Async.h"
#include "Common/TcpSocketBuilder.h"
#include "Containers/Ticker.h"
#include "Interfaces/IPv4/IPv4Address.h"
#include "Interfaces/IPv4/IPv4Endpoint.h"
#include "Sockets.h"
#include "SocketSubsystem.h"
#include "WitcherImportContext.h"

DEFINE_LOG_CATEGORY_STATIC(LogWitcherImportServer, Log, All);

FWitcherImportServer::FWitcherImportServer() = default;

FWitcherImportServer::~FWitcherImportServer()
{
    Stop();
}

void FWitcherImportServer::Start()
{
    if (Thread)
    {
        return;
    }
    bRunning = true;
    Thread = FRunnableThread::Create(this, TEXT("WitcherToolsImportServer"));
}

void FWitcherImportServer::Stop()
{
    bRunning = false;
    if (ListenSocket)
    {
        ListenSocket->Close();
    }
    if (Thread)
    {
        // Clear the member first: ~FRunnableThread calls Kill(), which
        // re-enters this Stop() - leaving the pointer set recursed until
        // stack overflow on editor shutdown.
        FRunnableThread* LocalThread = Thread;
        Thread = nullptr;
        LocalThread->WaitForCompletion();
        delete LocalThread;
    }
    if (ListenSocket)
    {
        ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(ListenSocket);
        ListenSocket = nullptr;
    }
}

bool FWitcherImportServer::Init()
{
    FIPv4Address Address;
    FIPv4Address::Parse(BindAddress, Address);
    const FIPv4Endpoint Endpoint(Address, Port);

    ListenSocket = FTcpSocketBuilder(TEXT("WitcherTools Import Socket"))
        .AsReusable()
        .BoundToEndpoint(Endpoint)
        .Listening(8);

    if (!ListenSocket)
    {
        UE_LOG(LogWitcherImportServer, Error, TEXT("Failed to bind Witcher import server at %s:%d"), *BindAddress, Port);
        return false;
    }

    UE_LOG(LogWitcherImportServer, Log, TEXT("Witcher import server listening at %s:%d"), *BindAddress, Port);
    return true;
}

uint32 FWitcherImportServer::Run()
{
    while (bRunning)
    {
        bool bHasPendingConnection = false;
        if (!ListenSocket || !ListenSocket->HasPendingConnection(bHasPendingConnection) || !bHasPendingConnection)
        {
            FPlatformProcess::Sleep(0.02f);
            continue;
        }

        TSharedRef<FInternetAddr> RemoteAddress = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->CreateInternetAddr();
        FSocket* ClientSocket = ListenSocket->Accept(*RemoteAddress, TEXT("WitcherTools Import Client"));
        if (!ClientSocket)
        {
            continue;
        }

        HandleClient(ClientSocket);
        ClientSocket->Close();
        ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(ClientSocket);
    }
    return 0;
}

void FWitcherImportServer::Exit()
{
}

bool FWitcherImportServer::ReceiveExact(FSocket* ClientSocket, TArray<uint8>& OutData, int32 Size) const
{
    OutData.SetNumUninitialized(Size);
    int32 BytesReceived = 0;
    while (BytesReceived < Size)
    {
        int32 BytesRead = 0;
        if (!ClientSocket->Recv(OutData.GetData() + BytesReceived, Size - BytesReceived, BytesRead) || BytesRead <= 0)
        {
            return false;
        }
        BytesReceived += BytesRead;
    }
    return true;
}

bool FWitcherImportServer::SendResponse(FSocket* ClientSocket, const FString& ResponseJson) const
{
    FTCHARToUTF8 Converter(*ResponseJson);
    const int32 BodySize = Converter.Length();
    TArray<uint8> Packet;
    Packet.SetNumUninitialized(4 + BodySize);
    Packet[0] = static_cast<uint8>(BodySize & 0xff);
    Packet[1] = static_cast<uint8>((BodySize >> 8) & 0xff);
    Packet[2] = static_cast<uint8>((BodySize >> 16) & 0xff);
    Packet[3] = static_cast<uint8>((BodySize >> 24) & 0xff);
    FMemory::Memcpy(Packet.GetData() + 4, Converter.Get(), BodySize);

    int32 BytesSent = 0;
    return ClientSocket->Send(Packet.GetData(), Packet.Num(), BytesSent) && BytesSent == Packet.Num();
}

void FWitcherImportServer::HandleClient(FSocket* ClientSocket) const
{
    TArray<uint8> Header;
    if (!ReceiveExact(ClientSocket, Header, 4))
    {
        return;
    }

    const int32 PayloadSize =
        static_cast<int32>(Header[0]) |
        (static_cast<int32>(Header[1]) << 8) |
        (static_cast<int32>(Header[2]) << 16) |
        (static_cast<int32>(Header[3]) << 24);

    if (PayloadSize <= 0 || PayloadSize > 64 * 1024 * 1024)
    {
        SendResponse(ClientSocket, FWitcherImportContext::ErrorResponse(TEXT("Invalid payload size")));
        return;
    }

    TArray<uint8> Payload;
    if (!ReceiveExact(ClientSocket, Payload, PayloadSize))
    {
        return;
    }
    Payload.Add(0);

    const FString RequestJson = FString(UTF8_TO_TCHAR(reinterpret_cast<const char*>(Payload.GetData())));
    TSharedRef<TPromise<FString>, ESPMode::ThreadSafe> Promise = MakeShared<TPromise<FString>, ESPMode::ThreadSafe>();
    TFuture<FString> Future = Promise->GetFuture();

    ExecuteOnGameThread(TEXT("WitcherTools Import Request"), [Promise, RequestJson]()
    {
        Promise->SetValue(FWitcherImportContext::HandleRequest(RequestJson));
    });

    SendResponse(ClientSocket, Future.Get());
}
