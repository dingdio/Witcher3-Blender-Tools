#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

#include "Animation/Skeleton.h"
#include "HAL/FileManager.h"
#include "Misc/PackageName.h"
#include "Rendering/SkeletalMeshRenderData.h"
#include "UObject/GarbageCollection.h"

using namespace WitcherImportInternal;

namespace
{
FString SafePreviewAssetName(const FString& SourceName)
{
    FString SafeName;
    SafeName.Reserve(SourceName.Len());
    for (const TCHAR Char : SourceName)
    {
        SafeName.AppendChar((FChar::IsAlnum(Char) || Char == TEXT('_')) ? Char : TEXT('_'));
    }
    return SafeName.IsEmpty() ? TEXT("WitcherCharacter") : SafeName;
}

FString PreviewMeshAssetRelForBlueprint(const FString& BlueprintAssetRel)
{
    FString Folder;
    int32 SlashIndex = INDEX_NONE;
    if (BlueprintAssetRel.FindLastChar(TEXT('/'), SlashIndex))
    {
        Folder = BlueprintAssetRel.Left(SlashIndex);
    }

    const FString PreviewName = FString::Printf(
        TEXT("SKM_%s_RetargetPreview"),
        *SafePreviewAssetName(AssetRelName(BlueprintAssetRel)));
    return Folder.IsEmpty() ? PreviewName : FString::Printf(TEXT("%s/%s"), *Folder, *PreviewName);
}

void ReplaceExistingGeneratedPreviewAsset(UObject* Existing)
{
    if (!Existing)
    {
        return;
    }
    Existing->Rename(nullptr, GetTransientPackage(),
        REN_DontCreateRedirectors | REN_ForceNoResetLoaders | REN_NonTransactional);
    Existing->ClearFlags(RF_Public | RF_Standalone);
    Existing->MarkAsGarbage();
    CollectGarbage(GARBAGE_COLLECTION_KEEPFLAGS);
}

bool HasRenderableLOD(const USkeletalMesh* Mesh)
{
    if (!Mesh)
    {
        return false;
    }
    const FSkeletalMeshRenderData* RenderData = Mesh->GetResourceForRendering();
    return RenderData &&
        RenderData->LODRenderData.Num() > 0 &&
        RenderData->LODRenderData[0].RenderSections.Num() > 0 &&
        RenderData->LODRenderData[0].GetNumVertices() > 0;
}

void ConfigureImportedBaseTemplate(USkeletalMeshComponent* Template, UAnimSequence* AnimSequence)
{
    // The driver plays the preview clip; follower parts copy its pose.
    if (!Template)
    {
        return;
    }
    AWitcherImportedActor::ConfigureBaseComponent(Template, /*bUpdateAnimationInEditor=*/true);
    if (AnimSequence)
    {
        Template->OverrideAnimationData(AnimSequence, true, true, 0.0f, 1.0f);
    }
}

bool EnsureWitcherImportedActorParent(UBlueprint* Blueprint)
{
    if (!Blueprint || Blueprint->ParentClass == AWitcherImportedActor::StaticClass())
    {
        return false;
    }
    Blueprint->ParentClass = AWitcherImportedActor::StaticClass();
    FBlueprintEditorUtils::RefreshAllNodes(Blueprint);
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
    return true;
}

USCS_Node* CreateSkeletalMeshNode(USimpleConstructionScript* ConstructionScript, const FString& MeshRel, USkeletalMesh* Mesh)
{
    if (!ConstructionScript || !Mesh)
    {
        return nullptr;
    }

    USCS_Node* Node = ConstructionScript->CreateNode(
        USkeletalMeshComponent::StaticClass(), FName(*AssetRelName(MeshRel)));
    if (USkeletalMeshComponent* Template = Cast<USkeletalMeshComponent>(Node->ComponentTemplate))
    {
        Template->SetSkeletalMeshAsset(Mesh);
    }
    return Node;
}
}

void FWitcherImportContext::ImportBlueprint()
{
    const TSharedPtr<FJsonObject>* BlueprintObject = nullptr;
    if (!Manifest->TryGetObjectField(TEXT("blueprint"), BlueprintObject))
    {
        return;
    }

    const FString AssetRel = JsonString(*BlueprintObject, TEXT("asset_path"));
    if (AssetRel.IsEmpty())
    {
        return;
    }
    const FString AnimationRel = JsonString(*BlueprintObject, TEXT("animation_asset_path"));
    UAnimSequence* AnimSequence = AnimationRel.IsEmpty() ? nullptr : FindAnimSequence(AnimationRel);
    if (!AnimationRel.IsEmpty() && !AnimSequence)
    {
        AddWarning(FString::Printf(TEXT("Blueprint '%s': animation '%s' not found"), *AssetRel, *AnimationRel));
    }
    const FString Name = AssetRelName(AssetRel);
    FString PreviewMeshRel = JsonString(*BlueprintObject, TEXT("retarget_preview_asset_path"));
    if (PreviewMeshRel.IsEmpty())
    {
        PreviewMeshRel = PreviewMeshAssetRelForBlueprint(AssetRel);
    }

    auto ResolveMaterialById = [&](const FString& MaterialId) -> UMaterialInterface*
    {
        if (MaterialId.IsEmpty())
        {
            return nullptr;
        }
        if (const TWeakObjectPtr<UMaterialInterface>* Found = MaterialsById.Find(MaterialId))
        {
            if (UMaterialInterface* Material = Found->Get())
            {
                return Material;
            }
        }
        if (UMaterialInterface* Material = LoadExistingAsset<UMaterialInterface>(ObjectPathFor(MaterialId)))
        {
            return Material;
        }
        return LoadExistingAsset<UMaterialInterface>(ObjectPathFor(MaterialId + TEXT("_mi")));
    };

    auto AssignRetargetPreviewMaterials = [&](USkeletalMesh* PreviewMesh)
    {
        if (!PreviewMesh)
        {
            return;
        }

        TSet<FString> PreviewSourceMeshRels;
        const TArray<TSharedPtr<FJsonValue>>* PartPaths = nullptr;
        if ((*BlueprintObject)->TryGetArrayField(TEXT("mesh_asset_paths"), PartPaths))
        {
            for (const TSharedPtr<FJsonValue>& Value : *PartPaths)
            {
                const FString PartRel = Value->AsString().Replace(TEXT("\\"), TEXT("/"));
                if (!PartRel.IsEmpty())
                {
                    PreviewSourceMeshRels.Add(PartRel);
                }
            }
        }

        TMap<FString, FString> MaterialIdBySlotName;
        const TArray<TSharedPtr<FJsonValue>>* Meshes = nullptr;
        if (Manifest->TryGetArrayField(TEXT("meshes"), Meshes))
        {
            for (const TSharedPtr<FJsonValue>& MeshValue : *Meshes)
            {
                const TSharedPtr<FJsonObject> MeshEntry = MeshValue->AsObject();
                if (!MeshEntry.IsValid())
                {
                    continue;
                }
                const FString MeshRel = JsonString(MeshEntry, TEXT("asset_path")).Replace(TEXT("\\"), TEXT("/"));
                if (!PreviewSourceMeshRels.Contains(MeshRel))
                {
                    continue;
                }

                const TArray<TSharedPtr<FJsonValue>>* Slots = nullptr;
                if (!MeshEntry->TryGetArrayField(TEXT("slots"), Slots))
                {
                    continue;
                }
                for (const TSharedPtr<FJsonValue>& SlotValue : *Slots)
                {
                    const TSharedPtr<FJsonObject> Slot = SlotValue->AsObject();
                    if (!Slot.IsValid())
                    {
                        continue;
                    }
                    const FString SlotName = JsonString(Slot, TEXT("slot_name"));
                    const FString MaterialId = JsonString(Slot, TEXT("material_id"));
                    if (!SlotName.IsEmpty() && !MaterialId.IsEmpty())
                    {
                        MaterialIdBySlotName.Add(SlotName.ToLower(), MaterialId);
                    }
                }
            }
        }

        if (MaterialIdBySlotName.IsEmpty())
        {
            return;
        }

        int32 AssignedCount = 0;
        TArray<FString> MissingSlotNames;
        TArray<FSkeletalMaterial>& SkeletalMaterials = PreviewMesh->GetMaterials();
        for (FSkeletalMaterial& SkeletalMaterial : SkeletalMaterials)
        {
            FString MaterialId;
            const FString SlotName = SkeletalMaterial.MaterialSlotName.ToString();
            if (!SlotName.IsEmpty())
            {
                if (const FString* Found = MaterialIdBySlotName.Find(SlotName.ToLower()))
                {
                    MaterialId = *Found;
                }
            }
            if (MaterialId.IsEmpty())
            {
                const FString ImportedSlotName = SkeletalMaterial.ImportedMaterialSlotName.ToString();
                if (!ImportedSlotName.IsEmpty())
                {
                    if (const FString* Found = MaterialIdBySlotName.Find(ImportedSlotName.ToLower()))
                    {
                        MaterialId = *Found;
                    }
                }
            }
            if (MaterialId.IsEmpty())
            {
                if (!SlotName.IsEmpty())
                {
                    MissingSlotNames.Add(SlotName);
                }
                continue;
            }

            if (UMaterialInterface* Material = ResolveMaterialById(MaterialId))
            {
                SkeletalMaterial.MaterialInterface = Material;
                ++AssignedCount;
            }
            else
            {
                MissingSlotNames.Add(SlotName.IsEmpty() ? MaterialId : SlotName);
            }
        }

        if (AssignedCount > 0)
        {
            PreviewMesh->MarkPackageDirty();
        }
        if (!MissingSlotNames.IsEmpty())
        {
            AddWarning(FString::Printf(
                TEXT("Blueprint '%s': retarget preview has unresolved material slot(s): %s"),
                *Name,
                *FString::Join(MissingSlotNames, TEXT(", "))));
        }
    };

    auto GeneratePreviewMesh = [&](const bool bForceReplace) -> USkeletalMesh*
    {
        const FString PreviewFbxRel = JsonString(*BlueprintObject, TEXT("retarget_preview_fbx"));
        // Avoid loading corrupt generated preview packages before validation.
        USkeletalMesh* ExistingPreview = FindObject<USkeletalMesh>(nullptr, *ObjectPathFor(PreviewMeshRel));
        if (!PreviewFbxRel.IsEmpty() && ExistingPreview && !bForceReplace)
        {
            if (HasRenderableLOD(ExistingPreview))
            {
                RetargetPreviewMesh = ExistingPreview;
                return ExistingPreview;
            }
            AddWarning(FString::Printf(
                TEXT("Blueprint '%s': existing retarget preview mesh has no renderable LODs; regenerating"),
                *Name));
        }

        const FString BaseMeshRel = JsonString(*BlueprintObject, TEXT("base_mesh_asset_path"));
        USkeletalMesh* BaseMesh = BaseMeshRel.IsEmpty()
            ? nullptr
            : LoadExistingAsset<USkeletalMesh>(ObjectPathFor(BaseMeshRel));
        if (!BaseMesh)
        {
            AddWarning(FString::Printf(
                TEXT("Blueprint '%s': retarget preview mesh skipped because base mesh '%s' was not found"),
                *Name, *BaseMeshRel));
            return nullptr;
        }

        USkeleton* BaseSkeleton = BaseMesh->GetSkeleton();
        if (!BaseSkeleton)
        {
            AddWarning(FString::Printf(
                TEXT("Blueprint '%s': retarget preview mesh skipped because base mesh '%s' has no skeleton"),
                *Name, *BaseMeshRel));
            return nullptr;
        }
        if (!HasRenderableLOD(BaseMesh))
        {
            AddWarning(FString::Printf(
                TEXT("Blueprint '%s': retarget preview mesh skipped because base mesh '%s' has no renderable LODs"),
                *Name, *BaseMeshRel));
            return nullptr;
        }

        auto DeleteOldGeneratedPackageFile = [&](const FString& GeneratedAssetRel, const TCHAR* Label) -> bool
        {
            const FString PackageName = FString::Printf(TEXT("%s/%s"), *ContentRoot, *GeneratedAssetRel);
            FString PackageFilename;
            if (FPackageName::TryConvertLongPackageNameToFilename(
                    PackageName,
                    PackageFilename,
                    FPackageName::GetAssetPackageExtension()) &&
                IFileManager::Get().FileExists(*PackageFilename) &&
                !IFileManager::Get().Delete(*PackageFilename, false, true, true))
            {
                AddWarning(FString::Printf(
                    TEXT("Blueprint '%s': could not delete old generated %s file '%s'"),
                    *Name, Label, *PackageFilename));
                return false;
            }
            return true;
        };

        if (!PreviewFbxRel.IsEmpty())
        {
            FAssetCompilingManager::Get().FinishAllCompilation();
            if (ExistingPreview)
            {
                ReplaceExistingGeneratedPreviewAsset(ExistingPreview);
            }
            const FString PreviewSkeletonRel = FString::Printf(TEXT("%s_Skeleton"), *PreviewMeshRel);
            USkeleton* ExistingPreviewSkeleton = FindObject<USkeleton>(nullptr, *ObjectPathFor(PreviewSkeletonRel));
            if (!ExistingPreviewSkeleton && ExistingPreview)
            {
                USkeleton* MeshSkeleton = ExistingPreview->GetSkeleton();
                if (MeshSkeleton && MeshSkeleton->GetPathName() == ObjectPathFor(PreviewSkeletonRel))
                {
                    ExistingPreviewSkeleton = MeshSkeleton;
                }
            }
            if (ExistingPreviewSkeleton)
            {
                ReplaceExistingGeneratedPreviewAsset(ExistingPreviewSkeleton);
            }
            if (!DeleteOldGeneratedPackageFile(PreviewMeshRel, TEXT("retarget preview mesh")) ||
                !DeleteOldGeneratedPackageFile(PreviewSkeletonRel, TEXT("retarget preview skeleton")))
            {
                return nullptr;
            }

            UObject* ImportedPreview = ImportFbxMesh(
                PreviewFbxRel,
                PreviewMeshRel,
                true,
                nullptr,
                /*bApplyRetargetPreviewFacing=*/true);
            if (USkeletalMesh* PreviewMesh = Cast<USkeletalMesh>(ImportedPreview))
            {
                AssignRetargetPreviewMaterials(PreviewMesh);
                if (USkeleton* PreviewSkeleton = PreviewMesh->GetSkeleton())
                {
                    PreviewSkeleton->MarkPackageDirty();
                    ImportedAssets.AddUnique(PreviewSkeleton->GetPathName());
                }
                else
                {
                    AddWarning(FString::Printf(
                        TEXT("Blueprint '%s': combined retarget preview imported without a Skeleton asset"),
                        *Name));
                }
                PreviewMesh->NeverStream = true;
                PreviewMesh->MarkPackageDirty();
                RetargetPreviewMesh = PreviewMesh;
                return PreviewMesh;
            }
            AddWarning(FString::Printf(
                TEXT("Blueprint '%s': combined retarget preview FBX import failed: %s"),
                *Name,
                *PreviewFbxRel));
            return nullptr;
        }

        RetargetPreviewMesh = BaseMesh;
        AddWarning(FString::Printf(
            TEXT("Blueprint '%s': retarget preview uses base mesh because manifest has no combined preview FBX"),
            *Name));
        return BaseMesh;
    };

    auto RebuildBlueprintComponents = [&](UBlueprint* Blueprint) -> USkeletalMeshComponent*
    {
        if (!Blueprint || !Blueprint->SimpleConstructionScript)
        {
            return nullptr;
        }

        USimpleConstructionScript* ConstructionScript = Blueprint->SimpleConstructionScript;
        const TArray<USCS_Node*> ExistingNodes = ConstructionScript->GetAllNodes();
        for (USCS_Node* Node : ExistingNodes)
        {
            if (Node)
            {
                ConstructionScript->RemoveNode(Node, false);
            }
        }

        USCS_Node* RootNode = nullptr;
        USkeletalMeshComponent* BaseTemplate = nullptr;
        const FString BaseMeshRel = JsonString(*BlueprintObject, TEXT("base_mesh_asset_path"));
        if (!BaseMeshRel.IsEmpty())
        {
            USkeletalMesh* BaseMesh = LoadExistingAsset<USkeletalMesh>(ObjectPathFor(BaseMeshRel));
            if (BaseMesh)
            {
                RootNode = CreateSkeletalMeshNode(ConstructionScript, BaseMeshRel, BaseMesh);
                if (RootNode)
                {
                    BaseTemplate = Cast<USkeletalMeshComponent>(RootNode->ComponentTemplate);
                    ConstructionScript->AddNode(RootNode);
                }
            }
            else
            {
                AddWarning(FString::Printf(TEXT("Blueprint '%s': base mesh '%s' not found"), *Name, *BaseMeshRel));
            }
        }

        const TArray<TSharedPtr<FJsonValue>>* Parts = nullptr;
        if ((*BlueprintObject)->TryGetArrayField(TEXT("mesh_asset_paths"), Parts))
        {
            for (const TSharedPtr<FJsonValue>& Value : *Parts)
            {
                const FString PartRel = Value->AsString();
                USkeletalMesh* PartMesh = LoadExistingAsset<USkeletalMesh>(ObjectPathFor(PartRel));
                if (!PartMesh)
                {
                    AddWarning(FString::Printf(TEXT("Blueprint '%s': part mesh '%s' not found"), *Name, *PartRel));
                    continue;
                }

                USCS_Node* Node = CreateSkeletalMeshNode(ConstructionScript, PartRel, PartMesh);
                if (!Node)
                {
                    continue;
                }
                if (USkeletalMeshComponent* Template = Cast<USkeletalMeshComponent>(Node->ComponentTemplate))
                {
                    if (BaseTemplate)
                    {
                        AWitcherImportedActor::ConfigureFollowerComponent(
                            Template, BaseTemplate, /*bUpdateAnimationInEditor=*/true);
                    }
                }
                if (!RootNode)
                {
                    ConstructionScript->AddNode(Node);
                    RootNode = Node;
                    BaseTemplate = Cast<USkeletalMeshComponent>(Node->ComponentTemplate);
                }
                else
                {
                    RootNode->AddChildNode(Node);
                }
            }
        }

        const TArray<TSharedPtr<FJsonValue>>* Attachments = nullptr;
        if ((*BlueprintObject)->TryGetArrayField(TEXT("attachments"), Attachments))
        {
            for (const TSharedPtr<FJsonValue>& Value : *Attachments)
            {
                const TSharedPtr<FJsonObject> AttachObj = Value->AsObject();
                if (!AttachObj.IsValid())
                {
                    continue;
                }
                const FString AttachRel = JsonString(AttachObj, TEXT("asset_path"));
                const FString AttachBone = JsonString(AttachObj, TEXT("attach_to_bone"));
                USkeletalMesh* AttachMesh = LoadExistingAsset<USkeletalMesh>(ObjectPathFor(AttachRel));
                if (!AttachMesh)
                {
                    AddWarning(FString::Printf(TEXT("Blueprint '%s': attachment mesh '%s' not found"), *Name, *AttachRel));
                    continue;
                }
                if (!RootNode)
                {
                    AddWarning(FString::Printf(TEXT("Blueprint '%s': attachment '%s' has no base mesh to attach to"), *Name, *AttachRel));
                    continue;
                }
                USCS_Node* Node = CreateSkeletalMeshNode(ConstructionScript, AttachRel, AttachMesh);
                if (!Node)
                {
                    continue;
                }
                if (USkeletalMeshComponent* Template = Cast<USkeletalMeshComponent>(Node->ComponentTemplate))
                {
                    Template->SetRelativeTransform(FTransform::Identity);
                    Template->SetAbsolute(false, false, true);
                    Template->VisibilityBasedAnimTickOption = EVisibilityBasedAnimTickOption::AlwaysTickPoseAndRefreshBones;
                }
                Node->AttachToName = AttachBone.IsEmpty() ? NAME_None : FName(*AttachBone);
                RootNode->AddChildNode(Node);
            }
        }

        ConstructionScript->ValidateSceneRootNodes();
        return BaseTemplate;
    };

    if (UBlueprint* ExistingBlueprint = LoadExistingAsset<UBlueprint>(ObjectPathFor(AssetRel)))
    {
        if (!ShouldOverwrite(TEXT("blueprints")))
        {
            GeneratePreviewMesh(/*bForceReplace=*/false);
            return;
        }
        const bool bReparented = EnsureWitcherImportedActorParent(ExistingBlueprint);
        if (USkeletalMeshComponent* BaseTemplate = RebuildBlueprintComponents(ExistingBlueprint))
        {
            // Followers are already rebuilt; only the driver needs anim setup.
            ConfigureImportedBaseTemplate(BaseTemplate, AnimSequence);
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(ExistingBlueprint);
            FKismetEditorUtilities::CompileBlueprint(ExistingBlueprint);
            ExistingBlueprint->MarkPackageDirty();
            ImportedAssets.AddUnique(ExistingBlueprint->GetPathName());
            GeneratePreviewMesh(ShouldOverwrite(TEXT("retarget_assets")) || ShouldOverwrite(TEXT("blueprints")));
            AddWarning(FString::Printf(TEXT("Blueprint '%s' already exists; rebuilt its skeletal components%s"),
                *AssetRel, bReparented ? TEXT(" and runtime actor parent") : TEXT("")));
            return;
        }
        AddWarning(FString::Printf(TEXT("Blueprint '%s' already exists; could not rebuild skeletal components"), *AssetRel));
        return;
    }

    const FString PackageName = FString::Printf(TEXT("%s/%s"), *ContentRoot, *AssetRel);
    UPackage* Package = CreatePackage(*PackageName);
    UBlueprint* Blueprint = FKismetEditorUtilities::CreateBlueprint(
        AWitcherImportedActor::StaticClass(), Package, FName(*Name), BPTYPE_Normal,
        UBlueprint::StaticClass(), UBlueprintGeneratedClass::StaticClass());
    if (!Blueprint)
    {
        AddWarning(FString::Printf(TEXT("Could not create blueprint '%s'"), *AssetRel));
        return;
    }
    FAssetRegistryModule::AssetCreated(Blueprint);

    USkeletalMeshComponent* BaseTemplate = RebuildBlueprintComponents(Blueprint);
    ConfigureImportedBaseTemplate(BaseTemplate, AnimSequence);
    GeneratePreviewMesh(/*bForceReplace=*/true);

    FKismetEditorUtilities::CompileBlueprint(Blueprint);
    Blueprint->MarkPackageDirty();
    ImportedAssets.Add(Blueprint->GetPathName());
}
