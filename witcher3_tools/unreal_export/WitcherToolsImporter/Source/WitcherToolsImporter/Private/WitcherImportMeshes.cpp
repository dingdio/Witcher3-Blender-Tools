#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

using namespace WitcherImportInternal;

namespace
{
void ConfigureStaticMeshAsCollisionMesh(UStaticMesh* Mesh)
{
    if (!Mesh)
    {
        return;
    }
    if (UBodySetup* BodySetup = Mesh->GetBodySetup())
    {
        Mesh->PreEditChange(nullptr);
        BodySetup->CollisionTraceFlag = CTF_UseComplexAsSimple;
        BodySetup->InvalidatePhysicsData();
        BodySetup->CreatePhysicsMeshes();
        Mesh->PostEditChange();
        Mesh->MarkPackageDirty();
    }
}
}

void FWitcherImportContext::ImportRig()
{
    const TSharedPtr<FJsonObject>* RigObject = nullptr;
    if (!Manifest->TryGetObjectField(TEXT("rig"), RigObject))
    {
        return;
    }

    const FString AssetRel = JsonString(*RigObject, TEXT("asset_path"));
    const FString SkeletonPath = ObjectPathFor(AssetRel + TEXT("_Skeleton"));
    if (USkeleton* ExistingSkeleton = LoadExistingAsset<USkeleton>(SkeletonPath))
    {
        if (!ShouldOverwrite(TEXT("skeletons")))
        {
            SharedSkeleton = ExistingSkeleton;
            return;
        }
    }

    UObject* Imported = ImportFbxMesh(JsonString(*RigObject, TEXT("fbx")), AssetRel, true, nullptr);
    if (USkeletalMesh* SkeletalMesh = Cast<USkeletalMesh>(Imported))
    {
        SharedSkeleton = SkeletalMesh->GetSkeleton();
    }
    if (!SharedSkeleton.IsValid())
    {
        AddWarning(FString::Printf(TEXT("Rig '%s' did not produce a skeleton; meshes will create their own"), *AssetRel));
    }
}

void FWitcherImportContext::ImportMeshes()
{
    const TArray<TSharedPtr<FJsonValue>>* Meshes = nullptr;
    if (!Manifest->TryGetArrayField(TEXT("meshes"), Meshes))
    {
        return;
    }
    for (const TSharedPtr<FJsonValue>& Value : *Meshes)
    {
        const TSharedPtr<FJsonObject> MeshEntry = Value->AsObject();
        if (!MeshEntry.IsValid())
        {
            continue;
        }
        const FString AssetRel = JsonString(MeshEntry, TEXT("asset_path"));
        const bool bSkeletal = JsonString(MeshEntry, TEXT("kind")) == TEXT("skeletal");
        const bool bCollisionMesh = JsonBool(MeshEntry, TEXT("collision"), false);
        const bool bOwnSkeleton = JsonBool(MeshEntry, TEXT("own_skeleton"), false);
        USkeleton* MeshSkeleton = (bSkeletal && !bOwnSkeleton) ? SharedSkeleton.Get() : nullptr;

        if (!ShouldOverwrite(TEXT("meshes")))
        {
            UObject* ExistingMesh = bSkeletal
                ? static_cast<UObject*>(LoadExistingAsset<USkeletalMesh>(ObjectPathFor(AssetRel)))
                : static_cast<UObject*>(LoadExistingAsset<UStaticMesh>(ObjectPathFor(AssetRel)));
            if (ExistingMesh)
            {
                MeshesByAssetRel.Add(AssetRel, ExistingMesh);
                if (bSkeletal && !bOwnSkeleton && !SharedSkeleton.IsValid())
                {
                    if (USkeletalMesh* SkeletalMesh = Cast<USkeletalMesh>(ExistingMesh))
                    {
                        SharedSkeleton = SkeletalMesh->GetSkeleton();
                    }
                }
                continue;
            }
        }

        UObject* Imported = MeshEntry->HasField(TEXT("buffer"))
            ? ImportBufferMesh(JsonString(MeshEntry, TEXT("buffer")), AssetRel, bSkeletal, MeshSkeleton)
            : ImportFbxMesh(JsonString(MeshEntry, TEXT("fbx")), AssetRel, bSkeletal, MeshSkeleton);
        if (!Imported)
        {
            AddError(FString::Printf(TEXT("Failed to import mesh '%s'"), *AssetRel));
            continue;
        }
        MeshesByAssetRel.Add(AssetRel, Imported);
        if (bSkeletal && !bOwnSkeleton && !SharedSkeleton.IsValid())
        {
            if (USkeletalMesh* SkeletalMesh = Cast<USkeletalMesh>(Imported))
            {
                SharedSkeleton = SkeletalMesh->GetSkeleton();
            }
        }
        if (bCollisionMesh)
        {
            ConfigureStaticMeshAsCollisionMesh(Cast<UStaticMesh>(Imported));
            continue;
        }
        AssignMaterialsToMesh(Imported, MeshEntry);
    }
}

UObject* FWitcherImportContext::ImportFbxMesh(const FString& BundleRelativeFbx, const FString& AssetRel, bool bSkeletal, USkeleton* Skeleton)
{
    const FString FbxPath = ResolveBundleFile(BundleRelativeFbx);
    if (!FPaths::FileExists(FbxPath))
    {
        AddWarning(FString::Printf(TEXT("FBX file does not exist: %s"), *FbxPath));
        return nullptr;
    }

    UAssetImportTask* Task = NewObject<UAssetImportTask>();
    Task->Filename = FbxPath;
    Task->DestinationPath = PackagePathFor(AssetRel);
    Task->DestinationName = AssetRelName(AssetRel);
    Task->bAutomated = true;
    Task->bSave = false;
    Task->bReplaceExisting = true;

    UFbxImportUI* Options = NewObject<UFbxImportUI>();
    Options->bImportMaterials = false;
    Options->bImportTextures = false;
    Options->bImportAnimations = false;
    Options->bCreatePhysicsAsset = false;
    Options->bAutomatedImportShouldDetectType = false;
    Options->MeshTypeToImport = bSkeletal ? FBXIT_SkeletalMesh : FBXIT_StaticMesh;
    Options->bImportAsSkeletal = bSkeletal;
    if (Skeleton)
    {
        Options->Skeleton = Skeleton;
    }
    if (Options->SkeletalMeshImportData)
    {
        Options->SkeletalMeshImportData->bImportMeshesInBoneHierarchy = true;
        // Honor the custom split normals + tangent basis the Blender exporter
        Options->SkeletalMeshImportData->NormalImportMethod = FBXNIM_ImportNormalsAndTangents;
        Options->SkeletalMeshImportData->NormalGenerationMethod = EFBXNormalGenerationMethod::MikkTSpace;
    }
    if (Options->StaticMeshImportData)
    {
        Options->StaticMeshImportData->bCombineMeshes = true;
        Options->StaticMeshImportData->bAutoGenerateCollision = false;
        Options->StaticMeshImportData->NormalImportMethod = FBXNIM_ImportNormalsAndTangents;
        Options->StaticMeshImportData->NormalGenerationMethod = EFBXNormalGenerationMethod::MikkTSpace;
    }
    Task->Options = Options;

    FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
    TArray<UAssetImportTask*> Tasks;
    Tasks.Add(Task);
    AssetToolsModule.Get().ImportAssetTasks(Tasks);

    UObject* MainAsset = nullptr;
    for (const FString& Path : Task->ImportedObjectPaths)
    {
        UObject* Imported = StaticLoadObject(UObject::StaticClass(), nullptr, *Path);
        if (!Imported)
        {
            continue;
        }
        ImportedAssets.Add(Path);
        if (!MainAsset && (Imported->IsA<USkeletalMesh>() || Imported->IsA<UStaticMesh>()))
        {
            MainAsset = Imported;
        }
    }

    if (MainAsset && MainAsset->GetPathName() != ObjectPathFor(AssetRel))
    {
        AddWarning(FString::Printf(
            TEXT("Mesh '%s' imported as '%s' instead of replacing the existing asset; the original may be open in an editor or locked."),
            *AssetRel, *MainAsset->GetPathName()));
    }
    return MainAsset;
}

UObject* FWitcherImportContext::ImportBufferMesh(const FString& BundleRelativeBuffer, const FString& AssetRel,
    bool bSkeletal, USkeleton* Skeleton)
{
    const FString BufPath = ResolveBundleFile(BundleRelativeBuffer);
    FW3BufMesh Mesh;
    FString Error;
    if (!ReadW3Buf(BufPath, Mesh, Error))
    {
        AddWarning(FString::Printf(TEXT("Buffer mesh '%s' failed: %s"), *AssetRel, *Error));
        return nullptr;
    }

    UPackage* Package = CreatePackage(*FString::Printf(TEXT("%s/%s"), *ContentRoot, *AssetRel));
    const FName ObjectName(*AssetRelName(AssetRel));
    if (UObject* Existing = LoadAnyAsset(AssetRel))
    {
        Existing->Rename(nullptr, GetTransientPackage(),
            REN_DontCreateRedirectors | REN_ForceNoResetLoaders | REN_NonTransactional);
        Existing->ClearFlags(RF_Public | RF_Standalone);
        Existing->MarkAsGarbage();
    }

    UObject* Result = nullptr;
    if (bSkeletal)
    {
        FString SkelError;
        Result = BuildSkeletalMeshFromBuffer(Mesh, Package, ObjectName, Skeleton, SkelError);
        if (!SkelError.IsEmpty())
        {
            AddWarning(FString::Printf(TEXT("%s: %s"), *AssetRel, *SkelError));
        }
    }
    else
    {
        Result = BuildStaticMeshFromBuffer(Mesh, Package, ObjectName);
    }

    if (!Result)
    {
        return nullptr;
    }
    FAssetRegistryModule::AssetCreated(Result);
    Result->MarkPackageDirty();
    ImportedAssets.Add(ObjectPathFor(AssetRel));
    return Result;
}

void FWitcherImportContext::ImportAnimations()
{
    const TArray<TSharedPtr<FJsonValue>>* Animations = nullptr;
    if (!Manifest->TryGetArrayField(TEXT("animations"), Animations) || Animations->Num() == 0)
    {
        return;
    }
    if (!SharedSkeleton.IsValid())
    {
        AddWarning(TEXT("Animations skipped: no shared skeleton was imported."));
        return;
    }

    for (const TSharedPtr<FJsonValue>& Value : *Animations)
    {
        const TSharedPtr<FJsonObject> AnimationObject = Value->AsObject();
        if (!AnimationObject.IsValid())
        {
            continue;
        }
        UAnimSequence* Imported = ImportAnimation(AnimationObject);
        if (!Imported)
        {
            AddError(FString::Printf(TEXT("Failed to import animation '%s'"),
                *JsonString(AnimationObject, TEXT("asset_path"))));
        }
    }
}

UAnimSequence* FWitcherImportContext::ImportAnimation(const TSharedPtr<FJsonObject>& AnimationObject)
{
    if (!AnimationObject.IsValid() || !SharedSkeleton.IsValid())
    {
        return nullptr;
    }

    const FString RawAssetRel = JsonString(AnimationObject, TEXT("asset_path"));
    if (RawAssetRel.IsEmpty())
    {
        return nullptr;
    }
    const FString AssetRel = ClassSafeAssetRel(RawAssetRel, UAnimSequence::StaticClass(), TEXT("_anim"));

    if (!ShouldOverwrite(TEXT("animations")))
    {
        if (UAnimSequence* Existing = FindAnimSequence(RawAssetRel))
        {
            return Existing;
        }
    }

    const FString FbxPath = ResolveBundleFile(JsonString(AnimationObject, TEXT("fbx")));
    if (!FPaths::FileExists(FbxPath))
    {
        AddWarning(FString::Printf(TEXT("Animation FBX file does not exist: %s"), *FbxPath));
        return nullptr;
    }

    UAssetImportTask* Task = NewObject<UAssetImportTask>();
    Task->Filename = FbxPath;
    Task->DestinationPath = PackagePathFor(AssetRel);
    Task->DestinationName = AssetRelName(AssetRel);
    Task->bAutomated = true;
    Task->bSave = false;
    Task->bReplaceExisting = true;

    UFbxImportUI* Options = NewObject<UFbxImportUI>();
    Options->bImportMaterials = false;
    Options->bImportTextures = false;
    Options->bImportAnimations = true;
    Options->bCreatePhysicsAsset = false;
    Options->bAutomatedImportShouldDetectType = false;
    Options->MeshTypeToImport = FBXIT_Animation;
    Options->bImportAsSkeletal = true;
    Options->Skeleton = SharedSkeleton.Get();
    Options->bImportMesh = false;
    if (Options->AnimSequenceImportData)
    {
        // Match skeletal mesh import, which folds Blender's Armature m->cm
        // factor into the root bone; otherwise clips collapse the root 100x.
        Options->AnimSequenceImportData->ImportUniformScale = 100.0f;
    }
    Task->Options = Options;

    FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
    TArray<UAssetImportTask*> Tasks;
    Tasks.Add(Task);
    AssetToolsModule.Get().ImportAssetTasks(Tasks);

    UAnimSequence* AnimSequence = nullptr;
    for (const FString& Path : Task->ImportedObjectPaths)
    {
        UObject* Imported = StaticLoadObject(UObject::StaticClass(), nullptr, *Path);
        if (!Imported)
        {
            continue;
        }
        ImportedAssets.Add(Path);
        if (!AnimSequence)
        {
            AnimSequence = Cast<UAnimSequence>(Imported);
        }
    }
    return AnimSequence;
}

UAnimSequence* FWitcherImportContext::FindAnimSequence(const FString& AssetRel)
{
    if (AssetRel.IsEmpty())
    {
        return nullptr;
    }
    if (UAnimSequence* Existing = LoadExistingAsset<UAnimSequence>(ObjectPathFor(AssetRel)))
    {
        return Existing;
    }
    // Check the sibling path used when same-stem assets collide by class.
    return LoadExistingAsset<UAnimSequence>(ObjectPathFor(AssetRel + TEXT("_anim")));
}

void FWitcherImportContext::AssignMaterialsToMesh(UObject* MeshObject, const TSharedPtr<FJsonObject>& MeshEntry)
{
    const TArray<TSharedPtr<FJsonValue>>* Slots = nullptr;
    if (!MeshEntry->TryGetArrayField(TEXT("slots"), Slots))
    {
        return;
    }

    TMap<int32, UMaterialInterface*> MaterialsBySlot;
    for (const TSharedPtr<FJsonValue>& Value : *Slots)
    {
        const TSharedPtr<FJsonObject> Slot = Value->AsObject();
        const FString MaterialId = JsonString(Slot, TEXT("material_id"));
        UMaterialInterface* Material = nullptr;
        if (const TWeakObjectPtr<UMaterialInterface>* Found = MaterialsById.Find(MaterialId))
        {
            Material = Found->Get();
        }
        if (!Material && !MaterialId.IsEmpty())
        {
            Material = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(MaterialId));
            if (!Material)
            {
                Material = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(MaterialId + TEXT("_mi")));
            }
        }
        if (Material)
        {
            MaterialsBySlot.Add(JsonInt(Slot, TEXT("slot_index"), 0), Material);
        }
        else if (!MaterialId.IsEmpty())
        {
            AddWarning(FString::Printf(TEXT("%s: material '%s' was not created"),
                *MeshObject->GetName(), *MaterialId));
        }
    }

    if (UStaticMesh* StaticMesh = Cast<UStaticMesh>(MeshObject))
    {
        const int32 Count = StaticMesh->GetStaticMaterials().Num();
        for (const TPair<int32, UMaterialInterface*>& Pair : MaterialsBySlot)
        {
            if (Pair.Key >= 0 && Pair.Key < Count)
            {
                StaticMesh->SetMaterial(Pair.Key, Pair.Value);
            }
        }
        StaticMesh->MarkPackageDirty();
    }
    else if (USkeletalMesh* SkeletalMesh = Cast<USkeletalMesh>(MeshObject))
    {
        TArray<FSkeletalMaterial>& SkeletalMaterials = SkeletalMesh->GetMaterials();
        for (const TPair<int32, UMaterialInterface*>& Pair : MaterialsBySlot)
        {
            if (Pair.Key >= 0 && Pair.Key < SkeletalMaterials.Num())
            {
                SkeletalMaterials[Pair.Key].MaterialInterface = Pair.Value;
            }
        }
        SkeletalMesh->MarkPackageDirty();
    }
}
