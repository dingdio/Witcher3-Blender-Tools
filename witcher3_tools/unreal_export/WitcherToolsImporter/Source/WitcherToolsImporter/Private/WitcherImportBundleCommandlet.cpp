#include "WitcherImportBundleCommandlet.h"

#include "FileHelpers.h"
#include "Misc/PackageName.h"
#include "Misc/Parse.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "UObject/SavePackage.h"
#include "WitcherImportContext.h"

namespace
{
FString BuildImportRequestJson(const FString& ManifestPath)
{
    TSharedPtr<FJsonObject> Request = MakeShared<FJsonObject>();
    Request->SetStringField(TEXT("command"), TEXT("import_bundle"));
    Request->SetStringField(TEXT("schema"), TEXT("witcher_unreal_export.v2"));
    Request->SetStringField(TEXT("manifest_path"), ManifestPath);

    FString Output;
    TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Output);
    FJsonSerializer::Serialize(Request.ToSharedRef(), Writer);
    return Output;
}

bool ResponseSucceeded(const FString& ResponseJson)
{
    TSharedPtr<FJsonObject> Response;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(ResponseJson);
    return FJsonSerializer::Deserialize(Reader, Response) &&
        Response.IsValid() &&
        Response->GetBoolField(TEXT("success"));
}

bool SavePackageDirect(UPackage* Package)
{
    if (!Package || !Package->IsDirty())
    {
        return true;
    }

    const FString PackageName = Package->GetName();
    FString PackageFilename;
    if (!FPackageName::TryConvertLongPackageNameToFilename(
            PackageName,
            PackageFilename,
            FPackageName::GetAssetPackageExtension()))
    {
        UE_LOG(LogTemp, Warning, TEXT("Could not resolve package filename for '%s'"), *PackageName);
        return false;
    }

    UE_LOG(LogTemp, Display, TEXT("Saving imported package: %s"), *PackageName);
    FSavePackageArgs SaveArgs;
    SaveArgs.TopLevelFlags = RF_Public | RF_Standalone;
    SaveArgs.SaveFlags = SAVE_NoError;
    SaveArgs.bSlowTask = false;
    const bool bSaved = UPackage::SavePackage(Package, nullptr, *PackageFilename, SaveArgs);
    if (!bSaved)
    {
        UE_LOG(LogTemp, Warning, TEXT("Failed to save imported package: %s"), *PackageName);
    }
    return bSaved;
}

bool SaveImportedPackages(const FString& ResponseJson)
{
    TSharedPtr<FJsonObject> Response;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(ResponseJson);
    if (!FJsonSerializer::Deserialize(Reader, Response) || !Response.IsValid())
    {
        return false;
    }

    const TArray<TSharedPtr<FJsonValue>>* ImportedAssets = nullptr;
    if (!Response->TryGetArrayField(TEXT("imported_assets"), ImportedAssets))
    {
        return true;
    }

    TSet<UPackage*> Packages;
    for (const TSharedPtr<FJsonValue>& Value : *ImportedAssets)
    {
        const FString AssetPath = Value->AsString();
        if (UPackage* Package = FindPackage(nullptr, *FPackageName::ObjectPathToPackageName(AssetPath)))
        {
            Packages.Add(Package);
            continue;
        }
        if (UObject* Object = FindObject<UObject>(nullptr, *AssetPath))
        {
            Packages.Add(Object->GetOutermost());
        }
    }

    bool bAllSaved = true;
    UE_LOG(LogTemp, Display, TEXT("Saving %d imported packages"), Packages.Num());
    for (UPackage* Package : Packages)
    {
        bAllSaved = SavePackageDirect(Package) && bAllSaved;
    }
    return bAllSaved;
}
}

UWitcherImportBundleCommandlet::UWitcherImportBundleCommandlet()
{
    IsClient = false;
    IsEditor = true;
    IsServer = false;
    LogToConsole = true;
}

int32 UWitcherImportBundleCommandlet::Main(const FString& Params)
{
    FString ManifestPath;
    if (!FParse::Value(*Params, TEXT("manifest="), ManifestPath) &&
        !FParse::Value(*Params, TEXT("manifest_path="), ManifestPath))
    {
        UE_LOG(LogTemp, Error, TEXT("Missing required -manifest=<path> argument"));
        return 2;
    }

    const FString ResponseJson = FWitcherImportContext::HandleRequest(BuildImportRequestJson(ManifestPath));
    UE_LOG(LogTemp, Display, TEXT("Witcher import response: %s"), *ResponseJson);
    if (!ResponseSucceeded(ResponseJson))
    {
        return 1;
    }
    if (FParse::Param(*Params, TEXT("save")) && !SaveImportedPackages(ResponseJson))
    {
        UE_LOG(LogTemp, Error, TEXT("Failed to save imported packages"));
        return 1;
    }
    return 0;
}
