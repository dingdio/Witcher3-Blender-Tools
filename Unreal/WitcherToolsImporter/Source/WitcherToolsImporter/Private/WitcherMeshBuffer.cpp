#include "WitcherMeshBuffer.h"

#include "Engine/SkeletalMesh.h"
#include "Engine/SkinnedAssetCommon.h"
#include "Engine/StaticMesh.h"
#include "MeshDescription.h"
#include "StaticMeshAttributes.h"
#include "SkeletalMeshAttributes.h"
#include "StaticToSkeletalMeshConverter.h"
#include "Animation/Skeleton.h"
#include "ReferenceSkeleton.h"
#include "BoneWeights.h"
#include "Misc/FileHelper.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"

static constexpr float kUnrealScale = 100.0f;

static FVector3f ToUnrealPos(const FVector3f& P) { return FVector3f(P.X, -P.Y, P.Z) * kUnrealScale; }
static FVector3f ToUnrealDir(const FVector3f& D) { return FVector3f(D.X, -D.Y, D.Z); }
static FVector2f ToUnrealUV(const FVector2f& UV) { return FVector2f(UV.X, 1.0f - UV.Y); }

namespace
{
struct FCombined
{
    TArray<FVector3f> Positions;
    TArray<FVector3f> Normals;
    TArray<FVector4f> Tangents;
    TArray<FVector2f> UV0;
    TArray<FVector2f> UV1;
    TArray<FColor> Colors;
    TArray<FIntVector4> BoneIndices;
    TArray<FVector4f> BoneWeights;
    TArray<uint32> Indices;

    struct FRange { FString Material; int32 FirstIndex; int32 NumFaces; };
    TArray<FRange> Materials;

    bool bHasUV1 = false;
    bool bHasColor = false;
};

FCombined Flatten(const FW3BufMesh& Mesh)
{
    FCombined C;
    C.bHasUV1 = Mesh.HasUV1();
    C.bHasColor = Mesh.HasColor();
    TArray<int32> Order;
    for (int32 i = 0; i < Mesh.Submeshes.Num(); ++i) Order.Add(i);
    Order.Sort([&Mesh](int32 A, int32 B) { return Mesh.Submeshes[A].MatId < Mesh.Submeshes[B].MatId; });
    for (int32 SubIndex : Order)
    {
        const FW3BufSubmesh& Sub = Mesh.Submeshes[SubIndex];
        const int32 Base = C.Positions.Num();
        const int32 FirstIndex = C.Indices.Num();
        for (int32 v = 0; v < Sub.VertexCount; ++v)
        {
            C.Positions.Add(ToUnrealPos(Sub.Positions[v]));
            C.Normals.Add(Sub.Normals.IsValidIndex(v) ? ToUnrealDir(Sub.Normals[v]) : FVector3f::ZAxisVector);
            const FVector3f T = Sub.Tangents.IsValidIndex(v) ? ToUnrealDir(FVector3f(Sub.Tangents[v])) : FVector3f::XAxisVector;
            const float Sign = Sub.Tangents.IsValidIndex(v) ? Sub.Tangents[v].W : 1.0f;
            C.Tangents.Add(FVector4f(T.X, T.Y, T.Z, Sign));
            C.UV0.Add(Sub.UV0.IsValidIndex(v) ? ToUnrealUV(Sub.UV0[v]) : FVector2f::ZeroVector);
            if (C.bHasUV1) C.UV1.Add(Sub.UV1.IsValidIndex(v) ? ToUnrealUV(Sub.UV1[v]) : FVector2f::ZeroVector);
            if (C.bHasColor) C.Colors.Add(Sub.Colors.IsValidIndex(v) ? Sub.Colors[v] : FColor::White);
            C.BoneIndices.Add(Sub.BoneIndices.IsValidIndex(v) ? Sub.BoneIndices[v] : FIntVector4(0, 0, 0, 0));
            C.BoneWeights.Add(Sub.BoneWeights.IsValidIndex(v) ? Sub.BoneWeights[v] : FVector4f(1, 0, 0, 0));
        }
        // Keep source winding; the axis conversion already matches UE front faces.
        for (int32 i = 0; i + 2 < Sub.Indices.Num(); i += 3)
        {
            C.Indices.Add(Base + Sub.Indices[i]);
            C.Indices.Add(Base + Sub.Indices[i + 1]);
            C.Indices.Add(Base + Sub.Indices[i + 2]);
        }
        C.Materials.Add({ Sub.Material, FirstIndex, (C.Indices.Num() - FirstIndex) / 3 });
    }
    return C;
}

void PopulateDescription(FMeshDescription& Desc, const FCombined& C)
{
    Desc.ReserveNewVertices(C.Positions.Num());
    Desc.ReserveNewVertexInstances(C.Indices.Num());
    Desc.ReserveNewPolygons(C.Indices.Num() / 3);
    Desc.ReserveNewPolygonGroups(C.Materials.Num());

    FStaticMeshAttributes Attributes(Desc);
    Attributes.Register();

    const int32 NumUV = C.bHasUV1 ? 2 : 1;
    Attributes.GetVertexInstanceUVs().SetNumChannels(NumUV);

    for (int32 v = 0; v < C.Positions.Num(); ++v)
    {
        Desc.CreateVertex();
        Attributes.GetVertexPositions().Set(FVertexID(v), C.Positions[v]);
    }

    TVertexInstanceAttributesRef<FVector3f> Normals = Attributes.GetVertexInstanceNormals();
    TVertexInstanceAttributesRef<FVector3f> Tangents = Attributes.GetVertexInstanceTangents();
    TVertexInstanceAttributesRef<float> BinormalSigns = Attributes.GetVertexInstanceBinormalSigns();
    TVertexInstanceAttributesRef<FVector4f> Colors = Attributes.GetVertexInstanceColors();
    TVertexInstanceAttributesRef<FVector2f> UVs = Attributes.GetVertexInstanceUVs();

    for (int32 i = 0; i < C.Indices.Num(); ++i)
    {
        const int32 Vert = C.Indices[i];
        const FVertexInstanceID Inst = Desc.CreateVertexInstance(FVertexID(Vert));
        Normals.Set(Inst, C.Normals[Vert]);
        Tangents.Set(Inst, FVector3f(C.Tangents[Vert]));
        BinormalSigns.Set(Inst, C.Tangents[Vert].W);
        if (C.bHasColor) Colors.Set(Inst, FVector4f(FLinearColor(C.Colors[Vert])));
        UVs.Set(Inst, 0, C.UV0[Vert]);
        if (C.bHasUV1) UVs.Set(Inst, 1, C.UV1[Vert]);
    }

    TPolygonGroupAttributesRef<FName> SlotNames = Attributes.GetPolygonGroupMaterialSlotNames();
    for (const FCombined::FRange& Range : C.Materials)
    {
        const FPolygonGroupID Group = Desc.CreatePolygonGroup();
        SlotNames.Set(Group, FName(*Range.Material));
        for (int32 f = 0; f < Range.NumFaces; ++f)
        {
            const int32 i = Range.FirstIndex + f * 3;
            Desc.CreatePolygon(Group, { FVertexInstanceID(i), FVertexInstanceID(i + 1), FVertexInstanceID(i + 2) });
        }
    }
}

template <typename T>
TArray<T> ReadTypedArray(const TArray<uint8>& Payload, const TSharedPtr<FJsonObject>& Entry, int32 BytesPerElem)
{
    TArray<T> Out;
    if (!Entry.IsValid()) return Out;
    const int32 Offset = Entry->GetIntegerField(TEXT("offset"));
    const int32 Size = Entry->GetIntegerField(TEXT("size"));
    if (Offset < 0 || Offset + Size > Payload.Num()) return Out;
    Out.SetNumUninitialized(Size / BytesPerElem);
    FMemory::Memcpy(Out.GetData(), Payload.GetData() + Offset, Size);
    return Out;
}

TArray<float> ReadFloats(const TArray<uint8>& Payload, const TSharedPtr<FJsonObject>& Attrs, const TCHAR* Name)
{
    const TSharedPtr<FJsonObject>* Entry = nullptr;
    if (!Attrs.IsValid() || !Attrs->TryGetObjectField(Name, Entry)) return {};
    return ReadTypedArray<float>(Payload, *Entry, 4);
}
} // namespace

bool FW3BufMesh::HasUV1() const
{
    for (const FW3BufSubmesh& Sub : Submeshes) if (Sub.UV1.Num() > 0) return true;
    return false;
}

bool FW3BufMesh::HasColor() const
{
    for (const FW3BufSubmesh& Sub : Submeshes) if (Sub.Colors.Num() > 0) return true;
    return false;
}

bool ReadW3Buf(const FString& Path, FW3BufMesh& Out, FString& OutError)
{
    TArray<uint8> Bytes;
    if (!FFileHelper::LoadFileToArray(Bytes, *Path))
    {
        OutError = FString::Printf(TEXT("could not read %s"), *Path);
        return false;
    }
    if (Bytes.Num() < 14 || FMemory::Memcmp(Bytes.GetData(), "W3BUF\0", 6) != 0)
    {
        OutError = TEXT("bad .w3buf magic");
        return false;
    }
    uint32 Version = 0, HeaderLen = 0;
    FMemory::Memcpy(&Version, Bytes.GetData() + 6, 4);
    FMemory::Memcpy(&HeaderLen, Bytes.GetData() + 10, 4);
    if (Version != 1)
    {
        OutError = FString::Printf(TEXT("unsupported .w3buf version %u"), Version);
        return false;
    }

    const int32 HeaderStart = 14;
    const FString HeaderStr(HeaderLen, reinterpret_cast<const ANSICHAR*>(Bytes.GetData() + HeaderStart));
    TSharedPtr<FJsonObject> Header;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(HeaderStr);
    if (!FJsonSerializer::Deserialize(Reader, Header) || !Header.IsValid())
    {
        OutError = TEXT("could not parse .w3buf header json");
        return false;
    }

    TArray<uint8> Payload;
    Payload.Append(Bytes.GetData() + HeaderStart + HeaderLen, Bytes.Num() - HeaderStart - HeaderLen);

    Out.MeshName = Header->GetStringField(TEXT("mesh_name"));
    Out.DepotPath = Header->GetStringField(TEXT("depot_path"));
    Out.bIsSkinned = Header->GetBoolField(TEXT("is_skinned"));

    const TSharedPtr<FJsonObject>* Bones = nullptr;
    if (Out.bIsSkinned && Header->TryGetObjectField(TEXT("bones"), Bones))
    {
        const TArray<TSharedPtr<FJsonValue>>* Names = nullptr;
        if ((*Bones)->TryGetArrayField(TEXT("names"), Names))
            for (const TSharedPtr<FJsonValue>& N : *Names) Out.BoneNames.Add(N->AsString());
        const TArray<TSharedPtr<FJsonValue>>* Parents = nullptr;
        if ((*Bones)->TryGetArrayField(TEXT("parents"), Parents))
            for (const TSharedPtr<FJsonValue>& P : *Parents) Out.BoneParents.Add(static_cast<int32>(P->AsNumber()));
        const TArray<TSharedPtr<FJsonValue>>* Poses = nullptr;
        if ((*Bones)->TryGetArrayField(TEXT("poses"), Poses))
        {
            for (const TSharedPtr<FJsonValue>& PoseVal : *Poses)
            {
                const TArray<TSharedPtr<FJsonValue>>& P = PoseVal->AsArray();
                FTransform Xform = FTransform::Identity;
                if (P.Num() >= 10)
                {
                    auto F = [&P](int32 i) { return static_cast<float>(P[i]->AsNumber()); };
                    Xform.SetLocation(FVector(F(0), F(1), F(2)));
                    Xform.SetRotation(FQuat(F(3), F(4), F(5), F(6)));
                    Xform.SetScale3D(FVector(F(7), F(8), F(9)));
                }
                Out.BonePoses.Add(Xform);
            }
        }
    }

    const TArray<TSharedPtr<FJsonValue>>* Subs = nullptr;
    if (!Header->TryGetArrayField(TEXT("submeshes"), Subs))
    {
        OutError = TEXT(".w3buf has no submeshes");
        return false;
    }

    for (const TSharedPtr<FJsonValue>& SubVal : *Subs)
    {
        const TSharedPtr<FJsonObject> S = SubVal->AsObject();
        FW3BufSubmesh Sub;
        Sub.Lod = S->GetIntegerField(TEXT("lod"));
        Sub.MatId = S->GetIntegerField(TEXT("mat_id"));
        Sub.Material = S->GetStringField(TEXT("material"));
        Sub.VertexCount = S->GetIntegerField(TEXT("vertex_count"));

        const TSharedPtr<FJsonObject>* Attrs = nullptr;
        S->TryGetObjectField(TEXT("attrs"), Attrs);
        const TSharedPtr<FJsonObject> AttrsObj = Attrs ? *Attrs : nullptr;

        const TArray<float> Pos = ReadFloats(Payload, AttrsObj, TEXT("position"));
        for (int32 i = 0; i + 2 < Pos.Num(); i += 3) Sub.Positions.Add(FVector3f(Pos[i], Pos[i + 1], Pos[i + 2]));
        const TArray<float> Nrm = ReadFloats(Payload, AttrsObj, TEXT("normal"));
        for (int32 i = 0; i + 2 < Nrm.Num(); i += 3) Sub.Normals.Add(FVector3f(Nrm[i], Nrm[i + 1], Nrm[i + 2]));
        const TArray<float> Tan = ReadFloats(Payload, AttrsObj, TEXT("tangent"));
        const int32 TanComps = Tan.Num() == Sub.VertexCount * 4 ? 4 : 3;
        for (int32 i = 0; i + TanComps - 1 < Tan.Num(); i += TanComps)
            Sub.Tangents.Add(FVector4f(Tan[i], Tan[i + 1], Tan[i + 2], TanComps == 4 ? Tan[i + 3] : 1.0f));
        const TArray<float> U0 = ReadFloats(Payload, AttrsObj, TEXT("uv0"));
        for (int32 i = 0; i + 1 < U0.Num(); i += 2) Sub.UV0.Add(FVector2f(U0[i], U0[i + 1]));
        const TArray<float> U1 = ReadFloats(Payload, AttrsObj, TEXT("uv1"));
        for (int32 i = 0; i + 1 < U1.Num(); i += 2) Sub.UV1.Add(FVector2f(U1[i], U1[i + 1]));

        const TSharedPtr<FJsonObject>* ColEntry = nullptr;
        if (AttrsObj.IsValid() && AttrsObj->TryGetObjectField(TEXT("color"), ColEntry))
        {
            const TArray<uint8> Col = ReadTypedArray<uint8>(Payload, *ColEntry, 1);
            for (int32 i = 0; i + 3 < Col.Num(); i += 4) Sub.Colors.Add(FColor(Col[i], Col[i + 1], Col[i + 2], Col[i + 3]));
        }
        const TSharedPtr<FJsonObject>* IdxEntry = nullptr;
        if (AttrsObj.IsValid() && AttrsObj->TryGetObjectField(TEXT("bone_index"), IdxEntry))
        {
            const TArray<uint16> BI = ReadTypedArray<uint16>(Payload, *IdxEntry, 2);
            for (int32 i = 0; i + 3 < BI.Num(); i += 4) Sub.BoneIndices.Add(FIntVector4(BI[i], BI[i + 1], BI[i + 2], BI[i + 3]));
        }
        const TArray<float> BW = ReadFloats(Payload, AttrsObj, TEXT("bone_weight"));
        for (int32 i = 0; i + 3 < BW.Num(); i += 4) Sub.BoneWeights.Add(FVector4f(BW[i], BW[i + 1], BW[i + 2], BW[i + 3]));

        const TSharedPtr<FJsonObject>* Indices = nullptr;
        if (S->TryGetObjectField(TEXT("indices"), Indices))
            Sub.Indices = ReadTypedArray<uint32>(Payload, *Indices, 4);

        Out.Submeshes.Add(MoveTemp(Sub));
    }
    return true;
}

UStaticMesh* BuildStaticMeshFromBuffer(const FW3BufMesh& Mesh, UObject* Package, FName Name)
{
    const FCombined C = Flatten(Mesh);
    if (C.Positions.Num() == 0) return nullptr;

    UStaticMesh* StaticMesh = NewObject<UStaticMesh>(Package, Name, RF_Public | RF_Standalone);

    FMeshDescription Desc;
    FStaticMeshAttributes(Desc).Register();
    PopulateDescription(Desc, C);

    TArray<FStaticMaterial>& Materials = StaticMesh->GetStaticMaterials();
    for (const FCombined::FRange& Range : C.Materials)
        Materials.Add(FStaticMaterial(nullptr, FName(*Range.Material), FName(*Range.Material)));

    UStaticMesh::FBuildMeshDescriptionsParams Params;
    Params.bBuildSimpleCollision = false;
    Params.bFastBuild = true;
    TArray<const FMeshDescription*> DescPtrs;
    DescPtrs.Add(&Desc);
    StaticMesh->BuildFromMeshDescriptions(DescPtrs, Params);

    for (int32 i = 0; i < StaticMesh->GetSourceModels().Num(); ++i)
    {
        FStaticMeshSourceModel& Source = StaticMesh->GetSourceModel(i);
        Source.BuildSettings.bRecomputeNormals = false;
        Source.BuildSettings.bRecomputeTangents = false;
        Source.BuildSettings.bRemoveDegenerates = false;
        Source.BuildSettings.bGenerateLightmapUVs = false;
    }
    StaticMesh->PostEditChange();
    return StaticMesh;
}

USkeletalMesh* BuildSkeletalMeshFromBuffer(const FW3BufMesh& Mesh, UObject* Package, FName Name,
    USkeleton* Skeleton, FString& OutError)
{
    if (Mesh.BoneNames.Num() == 0)
    {
        OutError = TEXT("skeletal buffer mesh carries no skeleton");
        return nullptr;
    }
    const FCombined C = Flatten(Mesh);
    if (C.Positions.Num() == 0) return nullptr;

    FReferenceSkeleton RefSkeleton;
    {
        FReferenceSkeletonModifier Modifier(RefSkeleton, nullptr);
        for (int32 b = 0; b < Mesh.BoneNames.Num(); ++b)
        {
            const int32 Parent = Mesh.BoneParents.IsValidIndex(b) ? Mesh.BoneParents[b] : -1;
            const FTransform Pose = Mesh.BonePoses.IsValidIndex(b) ? Mesh.BonePoses[b] : FTransform::Identity;
            Modifier.Add(FMeshBoneInfo(FName(*Mesh.BoneNames[b]), Mesh.BoneNames[b], Parent), Pose);
        }
    }

    FMeshDescription Desc;
    FSkeletalMeshAttributes SkelAttributes(Desc);
    SkelAttributes.Register();
    PopulateDescription(Desc, C);

    FSkeletalMeshAttributes::FBoneNameAttributesRef BoneNames = SkelAttributes.GetBoneNames();
    FSkeletalMeshAttributes::FBoneParentIndexAttributesRef BoneParents = SkelAttributes.GetBoneParentIndices();
    FSkeletalMeshAttributes::FBonePoseAttributesRef BonePoses = SkelAttributes.GetBonePoses();
    for (int32 b = 0; b < RefSkeleton.GetRawBoneNum(); ++b)
    {
        SkelAttributes.CreateBone();
        BoneNames.Set(b, RefSkeleton.GetRefBoneInfo()[b].Name);
        BoneParents.Set(b, RefSkeleton.GetRefBoneInfo()[b].ParentIndex);
        BonePoses.Set(b, RefSkeleton.GetRefBonePose()[b]);
    }

    const int32 NumBones = RefSkeleton.GetRawBoneNum();
    FSkinWeightsVertexAttributesRef VertexWeights = SkelAttributes.GetVertexSkinWeights();
    for (int32 v = 0; v < C.Positions.Num(); ++v)
    {
        const FIntVector4& Idx = C.BoneIndices[v];
        const FVector4f& Wgt = C.BoneWeights[v];
        TArray<UE::AnimationCore::FBoneWeight> Influences;
        for (int32 k = 0; k < 4; ++k)
        {
            if (Wgt[k] <= 0.0f) continue;
            const int32 Bone = (Idx[k] >= 0 && Idx[k] < NumBones) ? Idx[k] : 0;
            Influences.Emplace(static_cast<uint16>(Bone), Wgt[k]);
        }
        if (Influences.Num() == 0) Influences.Emplace(0, 1.0f);
        VertexWeights.Set(FVertexID(v), UE::AnimationCore::FBoneWeights::Create(Influences));
    }

    USkeletalMesh* SkeletalMesh = NewObject<USkeletalMesh>(Package, Name, RF_Public | RF_Standalone);
    TArray<FSkeletalMaterial> Materials;
    for (const FCombined::FRange& Range : C.Materials)
    {
        FSkeletalMaterial Material;
        Material.MaterialInterface = nullptr;
        Material.MaterialSlotName = FName(*Range.Material);
        Material.ImportedMaterialSlotName = FName(*Range.Material);
        Materials.Add(Material);
    }
    SkeletalMesh->GetMaterials() = Materials;

    TArray<const FMeshDescription*> DescPtrs;
    DescPtrs.Add(&Desc);
    if (!FStaticToSkeletalMeshConverter::InitializeSkeletalMeshFromMeshDescriptions(
            SkeletalMesh, DescPtrs, Materials, RefSkeleton, false, false))
    {
        OutError = TEXT("InitializeSkeletalMeshFromMeshDescriptions failed");
        return nullptr;
    }

    const int32 ExpectedBones = RefSkeleton.GetRawBoneNum();
    if (Skeleton)
    {
        SkeletalMesh->SetSkeleton(Skeleton);
        const bool bMerged = Skeleton->MergeAllBonesToBoneTree(SkeletalMesh, false);
        if (!bMerged)
        {
            OutError = FString::Printf(TEXT("shared skeleton rejected merge; mesh keeps %d local bone(s)"), ExpectedBones);
        }
        else
        {
            Skeleton->MarkPackageDirty();
        }
    }
    SkeletalMesh->PreEditChange(nullptr);
    SkeletalMesh->SetRefSkeleton(RefSkeleton);
    SkeletalMesh->CalculateInvRefMatrices();
    SkeletalMesh->PostEditChange();
    if (SkeletalMesh->GetRefSkeleton().GetRawBoneNum() != ExpectedBones)
    {
        OutError = FString::Printf(TEXT("skeletal buffer built %d/%d bone(s)"),
            SkeletalMesh->GetRefSkeleton().GetRawBoneNum(), ExpectedBones);
    }
    return SkeletalMesh;
}
