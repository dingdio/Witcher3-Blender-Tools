#include "WitcherRetargetSetupCommandlet.h"

#include "Animation/Skeleton.h"
#include "Dom/JsonObject.h"
#include "Engine/SkeletalMesh.h"
#include "Misc/FileHelper.h"
#include "Misc/Parse.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "WitcherRetargetSetup.h"

namespace
{
const TArray<FString>& WatchBoneNames()
{
    static const TArray<FString> Names = {
        TEXT("Root"),
        TEXT("Trajectory"),
        TEXT("pelvis"),
        TEXT("torso"),
        TEXT("torso3"),
        TEXT("neck"),
        TEXT("head"),
        TEXT("placer_jaw"),
        TEXT("jaw"),
        TEXT("dyng_hair_b_01"),
        TEXT("dyng_hair_b_02"),
        TEXT("dyng_hair_l_01"),
        TEXT("dyng_hair_r_01"),
    };
    return Names;
}

TSharedRef<FJsonObject> VectorObject(const FVector& Value)
{
    TSharedRef<FJsonObject> Object = MakeShared<FJsonObject>();
    Object->SetNumberField(TEXT("x"), Value.X);
    Object->SetNumberField(TEXT("y"), Value.Y);
    Object->SetNumberField(TEXT("z"), Value.Z);
    return Object;
}

TSharedRef<FJsonObject> RotatorObject(const FRotator& Value)
{
    TSharedRef<FJsonObject> Object = MakeShared<FJsonObject>();
    Object->SetNumberField(TEXT("pitch"), Value.Pitch);
    Object->SetNumberField(TEXT("yaw"), Value.Yaw);
    Object->SetNumberField(TEXT("roll"), Value.Roll);
    return Object;
}

TSharedRef<FJsonObject> TransformObject(const FTransform& Transform)
{
    TSharedRef<FJsonObject> Object = MakeShared<FJsonObject>();
    Object->SetObjectField(TEXT("translation"), VectorObject(Transform.GetTranslation()));
    Object->SetObjectField(TEXT("rotation"), RotatorObject(Transform.Rotator()));
    Object->SetObjectField(TEXT("scale"), VectorObject(Transform.GetScale3D()));
    return Object;
}

TSharedRef<FJsonObject> BoneObject(const FReferenceSkeleton& RefSkeleton, const int32 Index)
{
    TSharedRef<FJsonObject> Object = MakeShared<FJsonObject>();
    const int32 ParentIndex = RefSkeleton.GetParentIndex(Index);
    Object->SetNumberField(TEXT("index"), Index);
    Object->SetStringField(TEXT("name"), RefSkeleton.GetBoneName(Index).ToString());
    Object->SetNumberField(TEXT("parent_index"), ParentIndex);
    Object->SetStringField(TEXT("parent_name"),
        ParentIndex != INDEX_NONE ? RefSkeleton.GetBoneName(ParentIndex).ToString() : FString());
    Object->SetObjectField(TEXT("local"), TransformObject(RefSkeleton.GetRefBonePose()[Index]));
    return Object;
}

void DumpRefSkeleton(const FReferenceSkeleton& RefSkeleton, const FString& ClassName, TSharedRef<FJsonObject> Entry)
{
    Entry->SetStringField(TEXT("class"), ClassName);
    Entry->SetNumberField(TEXT("bone_count"), RefSkeleton.GetRawBoneNum());

    TArray<TSharedPtr<FJsonValue>> FirstBones;
    for (int32 Index = 0; Index < FMath::Min(RefSkeleton.GetRawBoneNum(), 32); ++Index)
    {
        FirstBones.Add(MakeShared<FJsonValueString>(RefSkeleton.GetBoneName(Index).ToString()));
    }
    Entry->SetArrayField(TEXT("first_bones"), FirstBones);

    TSharedRef<FJsonObject> Watch = MakeShared<FJsonObject>();
    for (const FString& BoneNameString : WatchBoneNames())
    {
        const int32 Index = RefSkeleton.FindBoneIndex(FName(*BoneNameString));
        if (Index == INDEX_NONE)
        {
            Watch->SetField(BoneNameString, MakeShared<FJsonValueNull>());
            continue;
        }
        Watch->SetObjectField(BoneNameString, BoneObject(RefSkeleton, Index));
    }
    Entry->SetObjectField(TEXT("watch_bones"), Watch);
}

int32 DumpSkeletons(const FString& Params)
{
    FString OutputPath;
    if (!FParse::Value(*Params, TEXT("DumpSkeletons="), OutputPath) || OutputPath.IsEmpty())
    {
        UE_LOG(LogTemp, Error, TEXT("Missing -DumpSkeletons=<json path>."));
        return 1;
    }

    FString AssetsParam;
    if (!FParse::Value(*Params, TEXT("Assets="), AssetsParam) || AssetsParam.IsEmpty())
    {
        UE_LOG(LogTemp, Error, TEXT("Missing -Assets=/Game/AssetA;/Game/AssetB."));
        return 1;
    }

    TArray<FString> AssetPaths;
    AssetsParam.ParseIntoArray(AssetPaths, TEXT(";"), true);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    for (FString AssetPath : AssetPaths)
    {
        AssetPath.TrimStartAndEndInline();
        if (AssetPath.IsEmpty())
        {
            continue;
        }

        TSharedRef<FJsonObject> Entry = MakeShared<FJsonObject>();
        UObject* Asset = StaticLoadObject(UObject::StaticClass(), nullptr, *AssetPath, nullptr, LOAD_NoWarn | LOAD_Quiet);
        if (!Asset)
        {
            Entry->SetBoolField(TEXT("missing"), true);
            Root->SetObjectField(AssetPath, Entry);
            continue;
        }

        Entry->SetStringField(TEXT("path"), Asset->GetPathName());
        if (USkeletalMesh* Mesh = Cast<USkeletalMesh>(Asset))
        {
            Entry->SetStringField(TEXT("skeleton"), Mesh->GetSkeleton() ? Mesh->GetSkeleton()->GetPathName() : FString());
            DumpRefSkeleton(Mesh->GetRefSkeleton(), TEXT("SkeletalMesh"), Entry);
        }
        else if (USkeleton* Skeleton = Cast<USkeleton>(Asset))
        {
            DumpRefSkeleton(Skeleton->GetReferenceSkeleton(), TEXT("Skeleton"), Entry);
        }
        else
        {
            Entry->SetStringField(TEXT("class"), Asset->GetClass()->GetName());
        }
        Root->SetObjectField(AssetPath, Entry);
    }

    FString JsonText;
    const TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&JsonText);
    if (!FJsonSerializer::Serialize(Root, Writer))
    {
        UE_LOG(LogTemp, Error, TEXT("Could not serialize skeleton dump JSON."));
        return 1;
    }
    if (!FFileHelper::SaveStringToFile(JsonText, *OutputPath))
    {
        UE_LOG(LogTemp, Error, TEXT("Could not write skeleton dump: %s"), *OutputPath);
        return 1;
    }
    UE_LOG(LogTemp, Display, TEXT("Wrote skeleton dump: %s"), *OutputPath);
    return 0;
}
}

UWitcherRetargetSetupCommandlet::UWitcherRetargetSetupCommandlet()
{
    IsClient = false;
    IsEditor = true;
    IsServer = false;
    LogToConsole = true;
}

int32 UWitcherRetargetSetupCommandlet::Main(const FString& Params)
{
    FString DumpPath;
    if (FParse::Value(*Params, TEXT("DumpSkeletons="), DumpPath))
    {
        return DumpSkeletons(Params);
    }

    FWitcherRetargetSetup::FOptions Options;
    Options.bForceRegenerate = FParse::Param(*Params, TEXT("force"));
    Options.bSaveAssets = true;

    const FWitcherRetargetSetup::FResult Result = FWitcherRetargetSetup::CreateOrRepairWomanBase(Options);

    for (const FString& AssetPath : Result.ImportedAssets)
    {
        UE_LOG(LogTemp, Display, TEXT("Retarget asset ready: %s"), *AssetPath);
    }
    for (const FString& Warning : Result.Warnings)
    {
        UE_LOG(LogTemp, Warning, TEXT("%s"), *Warning);
    }
    for (const FString& Error : Result.Errors)
    {
        UE_LOG(LogTemp, Error, TEXT("%s"), *Error);
    }

    return Result.bSuccess ? 0 : 1;
}
