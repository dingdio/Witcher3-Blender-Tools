#include "WitcherToolsImporterModule.h"

#include "Animation/AnimData/IAnimationDataModel.h"
#include "Animation/AnimCurveTypes.h"
#include "Animation/AnimSequence.h"
#include "Animation/AnimationPoseData.h"
#include "Animation/AttributesRuntime.h"
#include "Animation/Skeleton.h"
#include "Async/Async.h"
#include "AssetRegistry/AssetData.h"
#include "BoneContainer.h"
#include "BonePose.h"
#include "ContentBrowserModule.h"
#include "Dom/JsonObject.h"
#include "Framework/Notifications/NotificationManager.h"
#include "IContentBrowserSingleton.h"
#include "LevelEditorViewport.h"
#include "Misc/MemStack.h"
#include "Policies/CondensedJsonPrintPolicy.h"
#include "ReferenceSkeleton.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "ToolMenus.h"
#include "Widgets/Notifications/SNotificationList.h"
#include "WitcherBlenderClient.h"
#include "WitcherImportServer.h"
#include "WitcherRetargetSetup.h"
#include "WitcherVisibilityPanel.h"

#define LOCTEXT_NAMESPACE "FWitcherToolsImporterModule"

DEFINE_LOG_CATEGORY_STATIC(LogWitcherToolsImporter, Log, All);

namespace
{
const TCHAR* BlenderHost = TEXT("127.0.0.1");
constexpr int32 BlenderPort = 40778;

double CleanJsonNumber(const double Value)
{
    return FMath::IsFinite(Value) ? Value : 0.0;
}

TSharedPtr<FJsonValue> JsonVector(const FVector& Value)
{
    TArray<TSharedPtr<FJsonValue>> Array;
    Array.Reserve(3);
    Array.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Value.X)));
    Array.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Value.Y)));
    Array.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Value.Z)));
    return MakeShared<FJsonValueArray>(Array);
}

TSharedPtr<FJsonValue> JsonQuat(const FQuat& Value)
{
    const FQuat Normalized = Value.GetNormalized();
    TArray<TSharedPtr<FJsonValue>> Array;
    Array.Reserve(4);
    Array.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Normalized.X)));
    Array.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Normalized.Y)));
    Array.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Normalized.Z)));
    Array.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Normalized.W)));
    return MakeShared<FJsonValueArray>(Array);
}

void ShowWitcherNotification(const FString& Message, const float ExpireDuration = 6.0f)
{
    FNotificationInfo Info(FText::FromString(FString::Printf(TEXT("Witcher: %s"), *Message)));
    Info.ExpireDuration = ExpireDuration;
    FSlateNotificationManager::Get().AddNotification(Info);
}

FString BlenderResponseMessage(const FString& ResponseJson)
{
    TSharedPtr<FJsonObject> ResponseObject;
    const TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(ResponseJson);
    if (!FJsonSerializer::Deserialize(Reader, ResponseObject) || !ResponseObject.IsValid())
    {
        return ResponseJson;
    }

    bool bSuccess = false;
    ResponseObject->TryGetBoolField(TEXT("success"), bSuccess);

    FString Message;
    if (ResponseObject->TryGetStringField(TEXT("message"), Message) && !Message.IsEmpty())
    {
        return Message;
    }

    FString Error;
    if (ResponseObject->TryGetStringField(TEXT("error"), Error) && !Error.IsEmpty())
    {
        return bSuccess ? Error : FString::Printf(TEXT("Blender failed: %s"), *Error);
    }

    return ResponseJson;
}

UAnimSequence* GetSelectedAnimSequence(FString& OutError)
{
    FContentBrowserModule& ContentBrowserModule = FModuleManager::LoadModuleChecked<FContentBrowserModule>(TEXT("ContentBrowser"));

    TArray<FAssetData> SelectedAssets;
    ContentBrowserModule.Get().GetSelectedAssets(SelectedAssets);
    if (SelectedAssets.Num() == 0)
    {
        OutError = TEXT("Select one AnimSequence asset in the Content Browser.");
        return nullptr;
    }

    for (const FAssetData& AssetData : SelectedAssets)
    {
        UObject* Asset = AssetData.GetAsset();
        if (UAnimSequence* AnimSequence = Cast<UAnimSequence>(Asset))
        {
            return AnimSequence;
        }
    }

    OutError = TEXT("Selected asset is not an AnimSequence.");
    return nullptr;
}

double GuessTranslationScaleToMeters(const UAnimSequence* AnimSequence)
{
    const USkeleton* Skeleton = AnimSequence ? AnimSequence->GetSkeleton() : nullptr;
    if (!Skeleton)
    {
        return 0.01;
    }

    const FReferenceSkeleton& ReferenceSkeleton = Skeleton->GetReferenceSkeleton();
    if (ReferenceSkeleton.GetRawBoneNum() > 0)
    {
        const FVector RootScale = ReferenceSkeleton.GetRefBonePose()[0].GetScale3D();
        if (RootScale.GetAbsMax() > 10.0)
        {
            // Normal Witcher imports keep metre bone locals under a scaled root.
            return 1.0;
        }
    }

    // Retarget preview skeletons are imported in centimetres.
    return 0.01;
}

double PreviewFacingYawDegrees(const UAnimSequence* AnimSequence)
{
    const USkeleton* Skeleton = AnimSequence ? AnimSequence->GetSkeleton() : nullptr;
    const FString SkeletonPath = Skeleton ? Skeleton->GetPathName() : FString();
    return SkeletonPath.Contains(TEXT("RetargetPreview")) ? 180.0 : 0.0;
}

TSharedPtr<FJsonValue> JsonTrack(const FString& BoneName, const TArray<FTransform>& Transforms, const int32 NumKeys)
{
    TArray<TSharedPtr<FJsonValue>> Positions;
    TArray<TSharedPtr<FJsonValue>> Rotations;
    TArray<TSharedPtr<FJsonValue>> Scales;
    Positions.Reserve(NumKeys);
    Rotations.Reserve(NumKeys);
    Scales.Reserve(NumKeys);

    for (int32 FrameIndex = 0; FrameIndex < NumKeys; ++FrameIndex)
    {
        const FTransform& Transform = Transforms[FMath::Min(FrameIndex, Transforms.Num() - 1)];
        Positions.Add(JsonVector(Transform.GetTranslation()));
        Rotations.Add(JsonQuat(Transform.GetRotation()));
        Scales.Add(JsonVector(Transform.GetScale3D()));
    }

    TSharedRef<FJsonObject> TrackObject = MakeShared<FJsonObject>();
    TrackObject->SetStringField(TEXT("bone"), BoneName);
    TrackObject->SetArrayField(TEXT("positions"), Positions);
    TrackObject->SetArrayField(TEXT("rotations"), Rotations);
    TrackObject->SetArrayField(TEXT("scales"), Scales);
    return MakeShared<FJsonValueObject>(TrackObject);
}

void AddSourceSkeletonJson(const UAnimSequence* AnimSequence, TSharedRef<FJsonObject> Request)
{
    const USkeleton* Skeleton = AnimSequence ? AnimSequence->GetSkeleton() : nullptr;
    if (!Skeleton)
    {
        return;
    }

    const FReferenceSkeleton& ReferenceSkeleton = Skeleton->GetReferenceSkeleton();
    const int32 BoneCount = ReferenceSkeleton.GetRawBoneNum();
    if (BoneCount <= 0)
    {
        return;
    }

    const TArray<FTransform>& RefPoses = ReferenceSkeleton.GetRefBonePose();
    TArray<TSharedPtr<FJsonValue>> Names;
    TArray<TSharedPtr<FJsonValue>> Parents;
    TArray<TSharedPtr<FJsonValue>> Poses;
    Names.Reserve(BoneCount);
    Parents.Reserve(BoneCount);
    Poses.Reserve(BoneCount);

    for (int32 BoneIndex = 0; BoneIndex < BoneCount; ++BoneIndex)
    {
        Names.Add(MakeShared<FJsonValueString>(ReferenceSkeleton.GetBoneName(BoneIndex).ToString()));
        Parents.Add(MakeShared<FJsonValueNumber>(ReferenceSkeleton.GetParentIndex(BoneIndex)));

        const FTransform& RefPose = RefPoses.IsValidIndex(BoneIndex) ? RefPoses[BoneIndex] : FTransform::Identity;
        TArray<TSharedPtr<FJsonValue>> Pose;
        Pose.Reserve(10);

        const FVector Translation = RefPose.GetTranslation();
        const FQuat Rotation = RefPose.GetRotation().GetNormalized();
        const FVector Scale = RefPose.GetScale3D();
        Pose.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Translation.X)));
        Pose.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Translation.Y)));
        Pose.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Translation.Z)));
        Pose.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Rotation.X)));
        Pose.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Rotation.Y)));
        Pose.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Rotation.Z)));
        Pose.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Rotation.W)));
        Pose.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Scale.X)));
        Pose.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Scale.Y)));
        Pose.Add(MakeShared<FJsonValueNumber>(CleanJsonNumber(Scale.Z)));
        Poses.Add(MakeShared<FJsonValueArray>(Pose));
    }

    TSharedRef<FJsonObject> SourceSkeleton = MakeShared<FJsonObject>();
    SourceSkeleton->SetStringField(TEXT("path"), Skeleton->GetPathName());
    SourceSkeleton->SetArrayField(TEXT("names"), Names);
    SourceSkeleton->SetArrayField(TEXT("parents"), Parents);
    SourceSkeleton->SetArrayField(TEXT("poses"), Poses);
    Request->SetObjectField(TEXT("source_skeleton"), SourceSkeleton);
}

bool BuildEvaluatedTrackArrays(
    UAnimSequence* AnimSequence,
    const int32 NumKeys,
    const double Duration,
    const double Fps,
    TArray<TSharedPtr<FJsonValue>>& OutLocalTrackArray,
    TArray<TSharedPtr<FJsonValue>>& OutComponentTrackArray)
{
    USkeleton* Skeleton = AnimSequence ? AnimSequence->GetSkeleton() : nullptr;
    if (!AnimSequence || !Skeleton)
    {
        return false;
    }

    const FReferenceSkeleton& ReferenceSkeleton = Skeleton->GetReferenceSkeleton();
    const int32 BoneCount = ReferenceSkeleton.GetRawBoneNum();
    if (BoneCount <= 0)
    {
        return false;
    }

    FMemMark Mark(FMemStack::Get());

    TArray<FBoneIndexType> RequiredBoneIndices;
    RequiredBoneIndices.AddUninitialized(BoneCount);
    for (int32 BoneIndex = 0; BoneIndex < BoneCount; ++BoneIndex)
    {
        RequiredBoneIndices[BoneIndex] = static_cast<FBoneIndexType>(BoneIndex);
    }

    FBoneContainer RequiredBones;
    RequiredBones.InitializeTo(
        RequiredBoneIndices,
        UE::Anim::FCurveFilterSettings(UE::Anim::ECurveFilterMode::DisallowAll),
        *Skeleton);
    RequiredBones.SetUseRAWData(false);
    RequiredBones.SetUseSourceData(false);
    RequiredBones.SetDisableRetargeting(false);

    TArray<TArray<FTransform>> PerBoneLocalTransforms;
    TArray<TArray<FTransform>> PerBoneComponentTransforms;
    PerBoneLocalTransforms.SetNum(BoneCount);
    PerBoneComponentTransforms.SetNum(BoneCount);
    for (int32 BoneIndex = 0; BoneIndex < BoneCount; ++BoneIndex)
    {
        PerBoneLocalTransforms[BoneIndex].Reserve(NumKeys);
        PerBoneComponentTransforms[BoneIndex].Reserve(NumKeys);
    }

    FCompactPose CompactPose;
    CompactPose.SetBoneContainer(&RequiredBones);
    FBlendedCurve Curve;
    Curve.InitFrom(RequiredBones);
    UE::Anim::FStackAttributeContainer Attributes;
    FAnimationPoseData PoseData(CompactPose, Curve, Attributes);

    for (int32 FrameIndex = 0; FrameIndex < NumKeys; ++FrameIndex)
    {
        const double Time = NumKeys > 1
            ? FMath::Clamp(static_cast<double>(FrameIndex) / FMath::Max(Fps, 1.0), 0.0, FMath::Max(Duration, 0.0))
            : 0.0;

        CompactPose.ResetToRefPose();
        Curve.InitFrom(RequiredBones);
        Attributes.Empty();

        FAnimExtractContext ExtractionContext(Time, false);
        AnimSequence->GetAnimationPose(PoseData, ExtractionContext);
        FCSPose<FCompactPose> ComponentPose;
        ComponentPose.InitPose(CompactPose);

        for (int32 BoneIndex = 0; BoneIndex < BoneCount; ++BoneIndex)
        {
            const FCompactPoseBoneIndex PoseIndex = RequiredBones.GetCompactPoseIndexFromSkeletonIndex(BoneIndex);
            const FTransform LocalTransform = PoseIndex.IsValid()
                ? CompactPose[PoseIndex]
                : ReferenceSkeleton.GetRefBonePose()[BoneIndex];
            const FTransform ComponentTransform = PoseIndex.IsValid()
                ? ComponentPose.GetComponentSpaceTransform(PoseIndex)
                : ReferenceSkeleton.GetRefBonePose()[BoneIndex];
            PerBoneLocalTransforms[BoneIndex].Add(LocalTransform);
            PerBoneComponentTransforms[BoneIndex].Add(ComponentTransform);
        }
    }

    OutLocalTrackArray.Reserve(BoneCount);
    OutComponentTrackArray.Reserve(BoneCount);
    for (int32 BoneIndex = 0; BoneIndex < BoneCount; ++BoneIndex)
    {
        const FString BoneName = ReferenceSkeleton.GetBoneName(BoneIndex).ToString();
        OutLocalTrackArray.Add(JsonTrack(BoneName, PerBoneLocalTransforms[BoneIndex], NumKeys));
        OutComponentTrackArray.Add(JsonTrack(BoneName, PerBoneComponentTransforms[BoneIndex], NumKeys));
    }
    return OutLocalTrackArray.Num() > 0 && OutComponentTrackArray.Num() == OutLocalTrackArray.Num();
}

bool BuildRawTrackArray(const IAnimationDataModel* DataModel, const TArray<FName>& TrackNames, const int32 NumKeys, TArray<TSharedPtr<FJsonValue>>& OutTrackArray)
{
    if (!DataModel || TrackNames.Num() == 0)
    {
        return false;
    }

    OutTrackArray.Reserve(TrackNames.Num());
    for (const FName& TrackName : TrackNames)
    {
        TArray<FTransform> Transforms;
        DataModel->GetBoneTrackTransforms(TrackName, Transforms);
        if (Transforms.Num() == 0)
        {
            continue;
        }
        OutTrackArray.Add(JsonTrack(TrackName.ToString(), Transforms, NumKeys));
    }

    return OutTrackArray.Num() > 0;
}

bool BuildAnimationRequestJson(UAnimSequence* AnimSequence, FString& OutRequestJson, FString& OutError)
{
    if (!AnimSequence)
    {
        OutError = TEXT("No animation selected.");
        return false;
    }

    IAnimationDataModel* DataModel = AnimSequence->GetDataModel();
    if (!DataModel)
    {
        OutError = FString::Printf(TEXT("Animation has no data model: %s"), *AnimSequence->GetName());
        return false;
    }

    TArray<FName> TrackNames;
    DataModel->GetBoneTrackNames(TrackNames);
    if (TrackNames.Num() == 0)
    {
        OutError = FString::Printf(TEXT("Animation has no bone tracks: %s"), *AnimSequence->GetName());
        return false;
    }

    const int32 NumKeys = FMath::Max(1, DataModel->GetNumberOfKeys());
    const FFrameRate FrameRate = DataModel->GetFrameRate();
    double Fps = FrameRate.AsDecimal();
    if (Fps <= 0.0)
    {
        Fps = 30.0;
    }

    double Duration = DataModel->GetPlayLength();
    if (Duration <= 0.0 && NumKeys > 1)
    {
        Duration = static_cast<double>(NumKeys - 1) / Fps;
    }

    TArray<TSharedPtr<FJsonValue>> TrackArray;
    TArray<TSharedPtr<FJsonValue>> ComponentTrackArray;
    const bool bUsedEvaluatedTracks = BuildEvaluatedTrackArrays(AnimSequence, NumKeys, Duration, Fps, TrackArray, ComponentTrackArray);
    if (!bUsedEvaluatedTracks)
    {
        BuildRawTrackArray(DataModel, TrackNames, NumKeys, TrackArray);
    }

    if (TrackArray.Num() == 0)
    {
        OutError = FString::Printf(TEXT("Animation has no readable bone tracks: %s"), *AnimSequence->GetName());
        return false;
    }

    TSharedRef<FJsonObject> Request = MakeShared<FJsonObject>();
    Request->SetStringField(TEXT("command"), TEXT("send_animation_to_blender"));
    Request->SetStringField(TEXT("format"), TEXT("witcher_unreal_anim.v1"));
    Request->SetStringField(TEXT("space"), bUsedEvaluatedTracks ? TEXT("source_local_evaluated") : TEXT("source_local_raw"));
    Request->SetStringField(TEXT("preferred_pose_space"), bUsedEvaluatedTracks ? TEXT("source_component_evaluated") : TEXT("source_local_raw"));
    Request->SetStringField(TEXT("track_source"), bUsedEvaluatedTracks ? TEXT("GetAnimationPose") : TEXT("IAnimationDataModel"));
    Request->SetStringField(TEXT("name"), AnimSequence->GetName());
    Request->SetStringField(TEXT("asset_path"), AnimSequence->GetPathName());
    Request->SetNumberField(TEXT("duration"), CleanJsonNumber(Duration));
    Request->SetNumberField(TEXT("fps"), CleanJsonNumber(Fps));
    Request->SetNumberField(TEXT("num_frames"), NumKeys);
    Request->SetNumberField(TEXT("dt"), CleanJsonNumber(Fps > 0.0 ? 1.0 / Fps : 1.0 / 30.0));
    Request->SetNumberField(TEXT("translation_scale"), CleanJsonNumber(GuessTranslationScaleToMeters(AnimSequence)));
    Request->SetNumberField(TEXT("preview_facing_yaw_degrees"), CleanJsonNumber(PreviewFacingYawDegrees(AnimSequence)));
    Request->SetArrayField(TEXT("tracks"), TrackArray);
    if (ComponentTrackArray.Num() > 0)
    {
        Request->SetArrayField(TEXT("component_tracks"), ComponentTrackArray);
    }
    AddSourceSkeletonJson(AnimSequence, Request);

    const USkeleton* Skeleton = AnimSequence->GetSkeleton();
    if (Skeleton)
    {
        Request->SetStringField(TEXT("skeleton_path"), Skeleton->GetPathName());
    }

    TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer =
        TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&OutRequestJson);
    FJsonSerializer::Serialize(Request, Writer);
    return true;
}
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
        TEXT("SendAnimationToBlender"),
        LOCTEXT("SendAnimationToBlender", "Send Animation to Blender"),
        LOCTEXT("SendAnimationToBlenderTip", "Send the selected animation asset to Blender and load it on the active Witcher armature"),
        FSlateIcon(),
        FUIAction(FExecuteAction::CreateRaw(this, &FWitcherToolsImporterModule::OnSendAnimationToBlender)));
    Section.AddMenuEntry(
        TEXT("LayerVisibility"),
        LOCTEXT("LayerVisibility", "Layer Visibility"),
        LOCTEXT("LayerVisibilityTip", "Open the panel to show/hide imported layer groups (collision, meshes, lights)"),
        FSlateIcon(),
        FUIAction(FExecuteAction::CreateStatic(&WitcherVisibilityPanel::OpenTab)));
    Section.AddMenuEntry(
        TEXT("CreateWomanBaseRetargetSetup"),
        LOCTEXT("CreateWomanBaseRetargetSetup", "Create/Repair Woman Base Retarget Setup"),
        LOCTEXT("CreateWomanBaseRetargetSetupTip", "Create or repair IK_WomanBase, IK_Mannequin, CR_WomanBase, and RTG_MannequinToWomanBase"),
        FSlateIcon(),
        FUIAction(FExecuteAction::CreateRaw(this, &FWitcherToolsImporterModule::OnCreateWomanBaseRetargetSetup)));
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
        const FString Message = bOk ? BlenderResponseMessage(Response) : FString::Printf(TEXT("Could not reach Blender listener at %s:%d"), BlenderHost, BlenderPort);

        AsyncTask(ENamedThreads::GameThread, [Message]()
        {
            ShowWitcherNotification(Message);
        });
    });
}

void FWitcherToolsImporterModule::OnSendAnimationToBlender()
{
    FString Error;
    UAnimSequence* AnimSequence = GetSelectedAnimSequence(Error);
    if (!AnimSequence)
    {
        ShowWitcherNotification(Error, 8.0f);
        return;
    }

    FString RequestJson;
    if (!BuildAnimationRequestJson(AnimSequence, RequestJson, Error))
    {
        ShowWitcherNotification(Error, 8.0f);
        return;
    }

    const FString AnimName = AnimSequence->GetName();
    Async(EAsyncExecution::Thread, [RequestJson, AnimName]()
    {
        FString Response;
        const bool bOk = FWitcherBlenderClient::Send(BlenderHost, BlenderPort, RequestJson, Response);
        const FString Message = bOk
            ? BlenderResponseMessage(Response)
            : FString::Printf(TEXT("Could not reach Blender listener at %s:%d"), BlenderHost, BlenderPort);

        AsyncTask(ENamedThreads::GameThread, [Message, AnimName]()
        {
            ShowWitcherNotification(Message.IsEmpty() ? FString::Printf(TEXT("Sent %s to Blender."), *AnimName) : Message);
        });
    });
}

void FWitcherToolsImporterModule::OnCreateWomanBaseRetargetSetup()
{
    FWitcherRetargetSetup::FOptions Options;
    Options.bForceRegenerate = false;
    Options.bSaveAssets = true;
    const FWitcherRetargetSetup::FResult Result = FWitcherRetargetSetup::CreateOrRepairWomanBase(Options);

    FString Message;
    if (Result.bSuccess)
    {
        Message = FString::Printf(
            TEXT("Woman Base retarget setup ready (%d assets, %d warnings)."),
            Result.ImportedAssets.Num(),
            Result.Warnings.Num());
    }
    else
    {
        Message = FString::Printf(
            TEXT("Woman Base retarget setup failed (%d errors, %d warnings). Check Output Log."),
            Result.Errors.Num(),
            Result.Warnings.Num());
    }

    FNotificationInfo Info(FText::FromString(Message));
    Info.ExpireDuration = Result.bSuccess ? 6.0f : 10.0f;
    FSlateNotificationManager::Get().AddNotification(Info);
}

#undef LOCTEXT_NAMESPACE

IMPLEMENT_MODULE(FWitcherToolsImporterModule, WitcherToolsImporter)
