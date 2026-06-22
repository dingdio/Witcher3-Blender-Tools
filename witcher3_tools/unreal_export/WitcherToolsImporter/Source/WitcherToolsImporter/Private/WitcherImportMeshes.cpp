#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

#include "Factories/FbxFactory.h"
#include "UObject/GarbageCollection.h"

using namespace WitcherImportInternal;

namespace
{
bool MoveExistingAssetOutOfImportPath(UObject* Existing)
{
    if (!Existing)
    {
        return true;
    }
    FAssetCompilingManager::Get().FinishAllCompilation();
    const FString OldPath = Existing->GetPathName();
    FAssetRegistryModule::AssetDeleted(Existing);
    if (!Existing->Rename(nullptr, GetTransientPackage(), REN_DontCreateRedirectors | REN_NonTransactional))
    {
        UE_LOG(LogWitcherImportContext, Warning, TEXT("Could not move existing asset out of import path: %s"), *OldPath);
        return false;
    }
    Existing->ClearFlags(RF_Public | RF_Standalone);
    Existing->MarkAsGarbage();
    CollectGarbage(GARBAGE_COLLECTION_KEEPFLAGS);
    return true;
}

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
        FString SourceAssetRel = JsonString(MeshEntry, TEXT("asset_path"));
        SourceAssetRel = SourceAssetRel.Replace(TEXT("\\"), TEXT("/"));
        const bool bSkeletal = JsonString(MeshEntry, TEXT("kind")) == TEXT("skeletal");
        const bool bCollisionMesh = JsonBool(MeshEntry, TEXT("collision"), false);
        const bool bOwnSkeleton = JsonBool(MeshEntry, TEXT("own_skeleton"), false);
        USkeleton* MeshSkeleton = (bSkeletal && !bOwnSkeleton) ? SharedSkeleton.Get() : nullptr;
        const FString ReservedRel = MeshAssetRelBySource.FindRef(SourceAssetRel);
        const FString AssetRel = ClassSafeAssetRel(
            ReservedRel.IsEmpty() ? SourceAssetRel : ReservedRel,
            bSkeletal ? USkeletalMesh::StaticClass() : UStaticMesh::StaticClass(),
            TEXT("_mesh"));

        if (!ShouldOverwrite(TEXT("meshes")))
        {
            UObject* ExistingMesh = bSkeletal
                ? static_cast<UObject*>(LoadExistingAsset<USkeletalMesh>(ObjectPathFor(AssetRel)))
                : static_cast<UObject*>(LoadExistingAsset<UStaticMesh>(ObjectPathFor(AssetRel)));
            if (ExistingMesh)
            {
                MeshesByAssetRel.Add(SourceAssetRel, ExistingMesh);
                if (AssetRel != SourceAssetRel)
                {
                    MeshesByAssetRel.Add(AssetRel, ExistingMesh);
                }
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

        UObject* Imported = nullptr;
        const FString BufferRel = JsonString(MeshEntry, TEXT("buffer"));
        const FString FbxRel = JsonString(MeshEntry, TEXT("fbx"));
        if (!BufferRel.IsEmpty())
        {
            Imported = ImportBufferMesh(BufferRel, AssetRel, bSkeletal, MeshSkeleton);
        }
        else if (!bSkeletal)
        {
            const FString SiblingBufferRel = FPaths::ChangeExtension(FbxRel, TEXT(".w3buf"));
            if (!SiblingBufferRel.IsEmpty() && FPaths::FileExists(ResolveBundleFile(SiblingBufferRel)))
            {
                AddWarning(FString::Printf(
                    TEXT("Static mesh '%s': using sibling mesh buffer '%s' instead of automated FBX import"),
                    *AssetRel, *SiblingBufferRel));
                Imported = ImportBufferMesh(SiblingBufferRel, AssetRel, false, nullptr);
            }
            else
            {
                AddError(FString::Printf(
                    TEXT("Static mesh '%s' has no mesh buffer. Static FBX import is disabled in the automated pipeline."),
                    *AssetRel));
            }
        }
        else
        {
            Imported = ImportFbxMesh(FbxRel, AssetRel, true, MeshSkeleton);
        }
        if (!Imported)
        {
            AddError(FString::Printf(TEXT("Failed to import mesh '%s'"), *SourceAssetRel));
            continue;
        }
        MeshesByAssetRel.Add(SourceAssetRel, Imported);
        if (AssetRel != SourceAssetRel)
        {
            MeshesByAssetRel.Add(AssetRel, Imported);
        }
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

UObject* FWitcherImportContext::ImportFbxMesh(const FString& BundleRelativeFbx, const FString& AssetRel, bool bSkeletal,
    USkeleton* Skeleton, const bool bApplyRetargetPreviewFacing)
{
    const FString FbxPath = ResolveBundleFile(BundleRelativeFbx);
    if (!FPaths::FileExists(FbxPath))
    {
        AddWarning(FString::Printf(TEXT("FBX file does not exist: %s"), *FbxPath));
        return nullptr;
    }

    UObject* ExistingBeforeImport = LoadAnyAsset(AssetRel);
    UClass* ExpectedMeshClass = bSkeletal ? USkeletalMesh::StaticClass() : UStaticMesh::StaticClass();
    if (ExistingBeforeImport && !ExistingBeforeImport->IsA(ExpectedMeshClass))
    {
        AddError(FString::Printf(
            TEXT("Refusing to overwrite non-mesh asset '%s' (%s) with mesh import"),
            *AssetRel,
            *ExistingBeforeImport->GetClass()->GetName()));
        return nullptr;
    }

    UAssetImportTask* Task = NewObject<UAssetImportTask>();
    Task->Filename = FbxPath;
    Task->DestinationPath = PackagePathFor(AssetRel);
    Task->DestinationName = AssetRelName(AssetRel);
    Task->bAutomated = true;
    Task->bAsync = false;
    Task->bSave = false;
    Task->bReplaceExisting = true;
    Task->bReplaceExistingSettings = true;

    UFbxImportUI* Options = NewObject<UFbxImportUI>();
    Options->bImportMaterials = false;
    Options->bImportTextures = false;
    Options->bImportAnimations = false;
    Options->bCreatePhysicsAsset = false;
    Options->bAutomatedImportShouldDetectType = false;
    Options->MeshTypeToImport = bSkeletal ? FBXIT_SkeletalMesh : FBXIT_StaticMesh;
    Options->OriginalImportType = Options->MeshTypeToImport;
    Options->bImportAsSkeletal = bSkeletal;
    Options->bImportMesh = true;
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
        // Keep normal Witcher imports in their native basis.
        if (bApplyRetargetPreviewFacing)
        {
            // Rotate only the combined retarget preview to Unreal forward.
            Options->SkeletalMeshImportData->ImportRotation = FRotator(0.0f, 180.0f, 0.0f);
        }
    }
    if (Options->StaticMeshImportData)
    {
        Options->StaticMeshImportData->bCombineMeshes = true;
        Options->StaticMeshImportData->bAutoGenerateCollision = false;
        Options->StaticMeshImportData->NormalImportMethod = FBXNIM_ImportNormalsAndTangents;
        Options->StaticMeshImportData->NormalGenerationMethod = EFBXNormalGenerationMethod::MikkTSpace;
    }
    Task->Options = Options;

    UFbxFactory* FbxFactory = NewObject<UFbxFactory>();
    FbxFactory->ImportUI = Options;
    FbxFactory->SetDetectImportTypeOnImport(false);
    Task->Factory = FbxFactory;

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

    if (!MainAsset)
    {
        UObject* TargetAsset = LoadAnyAsset(AssetRel);
        if (TargetAsset && (TargetAsset != ExistingBeforeImport || TargetAsset->GetOutermost()->IsDirty()))
        {
            if (TargetAsset->IsA<USkeletalMesh>() || TargetAsset->IsA<UStaticMesh>())
            {
                MainAsset = TargetAsset;
                ImportedAssets.AddUnique(TargetAsset->GetPathName());
            }
        }
    }
    if (!MainAsset && Task->ImportedObjectPaths.Num() == 0)
    {
        AddWarning(FString::Printf(TEXT("FBX import produced no mesh asset for '%s' from '%s'"),
            *AssetRel, *FbxPath));
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

    UObject* ExistingAtPath = LoadAnyAsset(AssetRel);
    UClass* ExpectedMeshClass = bSkeletal ? USkeletalMesh::StaticClass() : UStaticMesh::StaticClass();
    if (ExistingAtPath && !ExistingAtPath->IsA(ExpectedMeshClass))
    {
        AddError(FString::Printf(
            TEXT("Refusing to overwrite non-mesh asset '%s' (%s) with buffer mesh import"),
            *AssetRel,
            *ExistingAtPath->GetClass()->GetName()));
        return nullptr;
    }

    if (!MoveExistingAssetOutOfImportPath(ExistingAtPath))
    {
        AddError(FString::Printf(TEXT("Could not overwrite existing buffer mesh '%s'"), *AssetRel));
        return nullptr;
    }

    UPackage* Package = CreatePackage(*FString::Printf(TEXT("%s/%s"), *ContentRoot, *AssetRel));
    const FName ObjectName(*AssetRelName(AssetRel));

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

    UAnimSequence* ExistingBeforeImport = LoadExistingAsset<UAnimSequence>(ObjectPathFor(AssetRel));

    UAssetImportTask* Task = NewObject<UAssetImportTask>();
    Task->Filename = FbxPath;
    Task->DestinationPath = PackagePathFor(AssetRel);
    Task->DestinationName = AssetRelName(AssetRel);
    Task->bAutomated = true;
    Task->bAsync = false;
    Task->bSave = false;
    Task->bReplaceExisting = true;
    Task->bReplaceExistingSettings = true;

    UFbxImportUI* Options = NewObject<UFbxImportUI>();
    Options->bImportMaterials = false;
    Options->bImportTextures = false;
    Options->bImportAnimations = true;
    Options->bCreatePhysicsAsset = false;
    Options->bAutomatedImportShouldDetectType = false;
    Options->MeshTypeToImport = FBXIT_Animation;
    Options->OriginalImportType = Options->MeshTypeToImport;
    Options->bImportAsSkeletal = true;
    Options->Skeleton = SharedSkeleton.Get();
    Options->bImportMesh = false;
    if (Options->AnimSequenceImportData)
    {
        // Match skeletal import root scale; clips otherwise collapse 100x.
        Options->AnimSequenceImportData->ImportUniformScale = 100.0f;
        // Retarget-facing yaw belongs in the generated pose.
    }
    Task->Options = Options;

    UFbxFactory* FbxFactory = NewObject<UFbxFactory>();
    FbxFactory->ImportUI = Options;
    FbxFactory->SetDetectImportTypeOnImport(false);
    Task->Factory = FbxFactory;

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
    if (!AnimSequence)
    {
        UAnimSequence* TargetAsset = LoadExistingAsset<UAnimSequence>(ObjectPathFor(AssetRel));
        if (TargetAsset && (TargetAsset != ExistingBeforeImport || TargetAsset->GetOutermost()->IsDirty()))
        {
            AnimSequence = TargetAsset;
            ImportedAssets.AddUnique(TargetAsset->GetPathName());
        }
    }
    if (!AnimSequence && Task->ImportedObjectPaths.Num() == 0)
    {
        AddWarning(FString::Printf(TEXT("FBX import produced no animation asset for '%s' from '%s'"),
            *AssetRel, *FbxPath));
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
