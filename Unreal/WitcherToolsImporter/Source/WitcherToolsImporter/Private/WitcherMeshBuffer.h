#pragma once

#include "CoreMinimal.h"

class USkeleton;

struct FW3BufSubmesh
{
    int32 Lod = 0;
    int32 MatId = 0;
    FString Material;
    int32 VertexCount = 0;

    TArray<FVector3f> Positions;
    TArray<FVector3f> Normals;
    TArray<FVector4f> Tangents;
    TArray<FVector2f> UV0;
    TArray<FVector2f> UV1;
    TArray<FColor> Colors;
    TArray<FIntVector4> BoneIndices;
    TArray<FVector4f> BoneWeights;
    TArray<uint32> Indices;
};

struct FW3BufMesh
{
    FString MeshName;
    FString DepotPath;
    bool bIsSkinned = false;
    TArray<FString> BoneNames;
    TArray<int32> BoneParents;
    TArray<FTransform> BonePoses;
    TArray<FW3BufSubmesh> Submeshes;

    bool HasUV1() const;
    bool HasColor() const;
};

bool ReadW3Buf(const FString& Path, FW3BufMesh& Out, FString& OutError);

class UStaticMesh* BuildStaticMeshFromBuffer(const FW3BufMesh& Mesh, UObject* Package, FName Name);

class USkeletalMesh* BuildSkeletalMeshFromBuffer(const FW3BufMesh& Mesh, UObject* Package, FName Name,
    USkeleton* Skeleton, FString& OutError);
