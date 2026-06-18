#include "WitcherBlenderClient.h"

#include "Common/TcpSocketBuilder.h"
#include "Interfaces/IPv4/IPv4Address.h"
#include "Interfaces/IPv4/IPv4Endpoint.h"
#include "Misc/ScopeExit.h"
#include "Sockets.h"
#include "SocketSubsystem.h"

namespace
{
bool SendAll(FSocket* Socket, const uint8* Data, int32 Size)
{
    int32 Sent = 0;
    while (Sent < Size)
    {
        int32 Wrote = 0;
        if (!Socket->Send(Data + Sent, Size - Sent, Wrote) || Wrote <= 0)
        {
            return false;
        }
        Sent += Wrote;
    }
    return true;
}

bool RecvAll(FSocket* Socket, uint8* Data, int32 Size)
{
    int32 Received = 0;
    while (Received < Size)
    {
        int32 Read = 0;
        if (!Socket->Recv(Data + Received, Size - Received, Read) || Read <= 0)
        {
            return false;
        }
        Received += Read;
    }
    return true;
}
}

bool FWitcherBlenderClient::Send(const FString& Host, int32 Port, const FString& RequestJson, FString& OutResponseJson)
{
    FIPv4Address Address;
    if (!FIPv4Address::Parse(Host, Address))
    {
        return false;
    }

    FSocket* Socket = FTcpSocketBuilder(TEXT("WitcherTools Blender Client")).AsBlocking();
    if (!Socket)
    {
        return false;
    }

    ON_SCOPE_EXIT
    {
        Socket->Close();
        ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Socket);
    };

    const FIPv4Endpoint Endpoint(Address, Port);
    if (!Socket->Connect(*Endpoint.ToInternetAddr()))
    {
        return false;
    }

    FTCHARToUTF8 Body(*RequestJson);
    const int32 BodySize = Body.Length();
    TArray<uint8> Packet;
    Packet.SetNumUninitialized(4 + BodySize);
    Packet[0] = static_cast<uint8>(BodySize & 0xff);
    Packet[1] = static_cast<uint8>((BodySize >> 8) & 0xff);
    Packet[2] = static_cast<uint8>((BodySize >> 16) & 0xff);
    Packet[3] = static_cast<uint8>((BodySize >> 24) & 0xff);
    FMemory::Memcpy(Packet.GetData() + 4, Body.Get(), BodySize);

    if (!SendAll(Socket, Packet.GetData(), Packet.Num()))
    {
        return false;
    }

    uint8 Header[4];
    if (!RecvAll(Socket, Header, 4))
    {
        return false;
    }
    const int32 ResponseSize = Header[0] | (Header[1] << 8) | (Header[2] << 16) | (Header[3] << 24);
    if (ResponseSize <= 0 || ResponseSize > 64 * 1024 * 1024)
    {
        return false;
    }

    TArray<uint8> Response;
    Response.SetNumUninitialized(ResponseSize + 1);
    if (!RecvAll(Socket, Response.GetData(), ResponseSize))
    {
        return false;
    }
    Response[ResponseSize] = 0;
    OutResponseJson = FString(UTF8_TO_TCHAR(reinterpret_cast<const char*>(Response.GetData())));
    return true;
}
