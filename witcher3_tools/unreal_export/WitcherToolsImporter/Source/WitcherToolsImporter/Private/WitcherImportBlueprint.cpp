#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

using namespace WitcherImportInternal;

namespace
{
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

    FKismetEditorUtilities::CompileBlueprint(Blueprint);
    Blueprint->MarkPackageDirty();
    ImportedAssets.Add(Blueprint->GetPathName());
}
