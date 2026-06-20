#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

using namespace WitcherImportInternal;

namespace
{
void HideEngineActorInEditor(AActor* Actor)
{
    if (Actor)
    {
        Actor->SetIsTemporarilyHiddenInEditor(true);
    }
}

void MarkEngineHidden(AActor* Actor)
{
    if (Actor)
    {
        Actor->Tags.Add(WitcherPlacementTags::EngineHidden());
        Actor->SetActorHiddenInGame(true);
        Actor->SetIsTemporarilyHiddenInEditor(true);
    }
}

void MarkDefaultHidden(AActor* Actor)
{
    if (Actor)
    {
        Actor->Tags.Add(WitcherPlacementTags::DefaultHidden());
        Actor->SetActorHiddenInGame(true);
        Actor->SetIsTemporarilyHiddenInEditor(true);
    }
}

void ConfigureVisualPlacement(UPrimitiveComponent* Component)
{
    if (!Component)
    {
        return;
    }
    Component->SetCollisionEnabled(ECollisionEnabled::NoCollision);
}

void ConfigureHiddenCollision(UPrimitiveComponent* Component)
{
    if (!Component)
    {
        return;
    }
    Component->SetVisibility(false, true);
    Component->SetHiddenInGame(true, true);
    Component->SetCastShadow(false);
    Component->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
    Component->SetCollisionProfileName(FName(TEXT("BlockAll")));
}

void ConfigureLightCommon(ULightComponent* Component, const TSharedPtr<FJsonObject>& LightEntry)
{
    if (!Component || !LightEntry.IsValid())
    {
        return;
    }
    Component->SetMobility(EComponentMobility::Movable);
    Component->SetIntensity(static_cast<float>(JsonNumber(LightEntry, TEXT("intensity"), 0.0)));
    Component->SetLightColor(JsonColor(LightEntry, TEXT("color")));
}

void ConfigurePointLight(UPointLightComponent* Component, const TSharedPtr<FJsonObject>& LightEntry)
{
    ConfigureLightCommon(Component, LightEntry);
    if (!Component || !LightEntry.IsValid())
    {
        return;
    }
    const float AttenuationRadius = static_cast<float>(JsonNumber(LightEntry, TEXT("attenuation_radius"), 0.0));
    if (AttenuationRadius > 0.0f)
    {
        Component->SetAttenuationRadius(AttenuationRadius);
    }
    const float SourceRadius = static_cast<float>(JsonNumber(LightEntry, TEXT("source_radius"), 0.0));
    if (SourceRadius > 0.0f)
    {
        Component->SetSourceRadius(SourceRadius);
    }
}
}

UStaticMesh* FWitcherImportContext::FindPlacementMesh(const FString& AssetRel)
{
    UStaticMesh* Mesh = Cast<UStaticMesh>(MeshesByAssetRel.FindRef(AssetRel).Get());
    if (!Mesh)
    {
        Mesh = LoadExistingAsset<UStaticMesh>(ObjectPathFor(AssetRel));
    }
    return Mesh;
}

bool FWitcherImportContext::ArePlacementLayerMeshesReady(const TSharedPtr<FJsonObject>& Layer, const FString& LayerId)
{
    bool bLayerMeshesReady = true;
    auto ValidateRequiredMesh = [&](const TSharedPtr<FJsonObject>& Entry, const TCHAR* Kind) -> bool
    {
        if (!Entry.IsValid())
        {
            return true;
        }
        const FString AssetRel = JsonString(Entry, TEXT("asset_path"));
        const FString EntryName = JsonString(Entry, TEXT("name"), FString(Kind));
        if (AssetRel.IsEmpty())
        {
            AddWarning(FString::Printf(
                TEXT("Placement %s '%s' has no mesh asset path (layer '%s'); layer left unchanged."),
                Kind,
                *EntryName,
                *LayerId));
            return false;
        }
        if (!FindPlacementMesh(AssetRel))
        {
            AddWarning(FString::Printf(
                TEXT("Placement %s '%s' references missing mesh '%s' (layer '%s'); layer left unchanged."),
                Kind,
                *EntryName,
                *AssetRel,
                *LayerId));
            return false;
        }
        return true;
    };

    const TArray<TSharedPtr<FJsonValue>>* PreflightActors = nullptr;
    if (Layer->TryGetArrayField(TEXT("actors"), PreflightActors))
    {
        for (const TSharedPtr<FJsonValue>& ActorValue : *PreflightActors)
        {
            bLayerMeshesReady = ValidateRequiredMesh(ActorValue->AsObject(), TEXT("actor")) && bLayerMeshesReady;
        }
    }
    const TArray<TSharedPtr<FJsonValue>>* PreflightInstancers = nullptr;
    if (Layer->TryGetArrayField(TEXT("instancers"), PreflightInstancers))
    {
        for (const TSharedPtr<FJsonValue>& InstancerValue : *PreflightInstancers)
        {
            bLayerMeshesReady = ValidateRequiredMesh(InstancerValue->AsObject(), TEXT("instancer")) && bLayerMeshesReady;
        }
    }
    return bLayerMeshesReady;
}

void FWitcherImportContext::TagPlacementActor(const FPlacementLayer& Layer, AActor* Actor, const FString& ActorName, const FString& ActorFolder)
{
    const FName LayerTag = Layer.LayerTag;
    if (!Actor)
    {
        return;
    }
    Actor->Tags.Add(LayerTag);
    if (ActorFolder.EndsWith(TEXT("/Collision")))
    {
        Actor->Tags.Add(WitcherPlacementTags::Collision());
    }
    else if (ActorFolder.EndsWith(TEXT("/Lights")))
    {
        Actor->Tags.Add(WitcherPlacementTags::Light());
    }
    else
    {
        Actor->Tags.Add(WitcherPlacementTags::Mesh());
    }
    Actor->SetActorLabel(ActorName);
    if (!ActorFolder.IsEmpty())
    {
        Actor->SetFolderPath(FName(*ActorFolder));
    }
    ImportedAssets.Add(Actor->GetPathName());
}

void FWitcherImportContext::ImportPlacementActors(const FPlacementLayer& Layer, const TArray<TSharedPtr<FJsonValue>>& Actors, FPlacementLayerStats& Stats)
{
    UWorld* World = Layer.World;
    const FString& LayerId = Layer.LayerId;
    const FString& MeshFolder = Layer.MeshFolder;
    const FString& CollisionFolder = Layer.CollisionFolder;
    int32& ActorCount = Stats.Actors;
    int32& CollisionActorCount = Stats.CollisionActors;
    auto PlaceActorCommon = [&](AActor* Actor, const FString& ActorName, const FString& ActorFolder)
    {
        TagPlacementActor(Layer, Actor, ActorName, ActorFolder);
    };

    for (const TSharedPtr<FJsonValue>& ActorValue : Actors)
    {
        const TSharedPtr<FJsonObject> ActorEntry = ActorValue->AsObject();
        if (!ActorEntry.IsValid())
        {
            continue;
        }
        const FString AssetRel = JsonString(ActorEntry, TEXT("asset_path"));
        const bool bCollisionOnly = JsonBool(ActorEntry, TEXT("collision_only"), false);
        const bool bEngineHidden = JsonBool(ActorEntry, TEXT("engine_hidden"), false);
        const bool bDefaultHidden = JsonBool(ActorEntry, TEXT("default_hidden"), false);
        UStaticMesh* Mesh = FindPlacementMesh(AssetRel);
        if (!Mesh)
        {
            AddWarning(FString::Printf(TEXT("Placement mesh not found for '%s' (layer '%s')."), *AssetRel, *LayerId));
            continue;
        }
        const FString ActorName = JsonString(ActorEntry, TEXT("name"), TEXT("Placement"));
        const TSharedPtr<FJsonObject>* TransformPtr = nullptr;
        ActorEntry->TryGetObjectField(TEXT("transform"), TransformPtr);
        const TSharedPtr<FJsonObject> Transform = TransformPtr ? *TransformPtr : nullptr;
        const FVector Location = JsonVector(Transform, TEXT("location"), FVector::ZeroVector);
        const FQuat Rotation = JsonQuat(Transform, TEXT("rotation"));
        const FVector ScaleVec = JsonVector(Transform, TEXT("scale"), FVector::OneVector);
        if (!IsUsablePlacementTransform(Location, Rotation, ScaleVec))
        {
            AddWarning(FString::Printf(
                TEXT("Placement actor '%s' has an invalid or zero-scale transform (layer '%s'); skipped."),
                *ActorName,
                *LayerId));
            continue;
        }

        AStaticMeshActor* MeshActor = World->SpawnActor<AStaticMeshActor>();
        if (!MeshActor)
        {
            continue;
        }
        if (UStaticMeshComponent* Component = MeshActor->GetStaticMeshComponent())
        {
            Component->SetMobility(EComponentMobility::Movable);
            Component->SetStaticMesh(Mesh);
            if (bCollisionOnly)
            {
                ConfigureHiddenCollision(Component);
            }
            else
            {
                ConfigureVisualPlacement(Component);
            }
        }
        MeshActor->SetActorTransform(FTransform(Rotation, Location, ScaleVec));
        PlaceActorCommon(MeshActor, ActorName, bCollisionOnly ? CollisionFolder : MeshFolder);
        if (bCollisionOnly)
        {
            MeshActor->SetActorHiddenInGame(true);
            HideEngineActorInEditor(MeshActor);
            if (bDefaultHidden)
            {
                MarkDefaultHidden(MeshActor);
            }
            ++CollisionActorCount;
            continue;
        }
        if (bEngineHidden)
        {
            MarkEngineHidden(MeshActor);
        }
        if (bDefaultHidden)
        {
            MarkDefaultHidden(MeshActor);
        }
        ++ActorCount;

        const FString CollisionAssetRel = JsonString(ActorEntry, TEXT("collision_asset_path"));
        if (!CollisionAssetRel.IsEmpty())
        {
            UStaticMesh* CollisionMesh = FindPlacementMesh(CollisionAssetRel);
            if (!CollisionMesh)
            {
                AddWarning(FString::Printf(
                    TEXT("Placement collision mesh not found for '%s' (layer '%s')."),
                    *CollisionAssetRel,
                    *LayerId));
                continue;
            }
            AStaticMeshActor* CollisionActor = World->SpawnActor<AStaticMeshActor>();
            if (!CollisionActor)
            {
                continue;
            }
            if (UStaticMeshComponent* CollisionComponent = CollisionActor->GetStaticMeshComponent())
            {
                CollisionComponent->SetMobility(EComponentMobility::Movable);
                CollisionComponent->SetStaticMesh(CollisionMesh);
                ConfigureHiddenCollision(CollisionComponent);
            }
            CollisionActor->SetActorTransform(FTransform(Rotation, Location, ScaleVec));
            CollisionActor->SetActorHiddenInGame(true);
            PlaceActorCommon(CollisionActor, ActorName + TEXT("_Collision"), CollisionFolder);
            HideEngineActorInEditor(CollisionActor);
            if (bDefaultHidden)
            {
                MarkDefaultHidden(CollisionActor);
            }
            ++CollisionActorCount;
        }
    }
}

void FWitcherImportContext::ImportPlacementInstancers(const FPlacementLayer& Layer, const TArray<TSharedPtr<FJsonValue>>& Instancers, FPlacementLayerStats& Stats)
{
    UWorld* World = Layer.World;
    const FString& LayerId = Layer.LayerId;
    const FString& MeshFolder = Layer.MeshFolder;
    const FString& CollisionFolder = Layer.CollisionFolder;
    int32& InstanceCount = Stats.Instances;
    int32& CollisionActorCount = Stats.CollisionActors;
    auto PlaceActorCommon = [&](AActor* Actor, const FString& ActorName, const FString& ActorFolder)
    {
        TagPlacementActor(Layer, Actor, ActorName, ActorFolder);
    };

    for (const TSharedPtr<FJsonValue>& InstancerValue : Instancers)
    {
        const TSharedPtr<FJsonObject> InstancerEntry = InstancerValue->AsObject();
        if (!InstancerEntry.IsValid())
        {
            continue;
        }
        const FString AssetRel = JsonString(InstancerEntry, TEXT("asset_path"));
        const bool bCollisionOnly = JsonBool(InstancerEntry, TEXT("collision_only"), false);
        const bool bEngineHidden = JsonBool(InstancerEntry, TEXT("engine_hidden"), false);
        const bool bDefaultHidden = JsonBool(InstancerEntry, TEXT("default_hidden"), false);
        UStaticMesh* Mesh = FindPlacementMesh(AssetRel);
        if (!Mesh)
        {
            AddWarning(FString::Printf(TEXT("Instancer mesh not found for '%s' (layer '%s')."), *AssetRel, *LayerId));
            continue;
        }
        const TArray<TSharedPtr<FJsonValue>>* Instances = nullptr;
        if (!InstancerEntry->TryGetArrayField(TEXT("instances"), Instances) || Instances->Num() == 0)
        {
            continue;
        }
        const FString InstancerName = JsonString(InstancerEntry, TEXT("name"), TEXT("Instancer"));
        UStaticMesh* CollisionMesh = nullptr;
        const FString CollisionAssetRel = bCollisionOnly
            ? FString()
            : JsonString(InstancerEntry, TEXT("collision_asset_path"));
        if (!CollisionAssetRel.IsEmpty())
        {
            CollisionMesh = FindPlacementMesh(CollisionAssetRel);
            if (!CollisionMesh)
            {
                AddWarning(FString::Printf(
                    TEXT("Instancer collision mesh not found for '%s' (layer '%s')."),
                    *CollisionAssetRel,
                    *LayerId));
            }
        }

        AActor* Container = World->SpawnActor<AActor>();
        if (!Container)
        {
            continue;
        }
        USceneComponent* Root = NewObject<USceneComponent>(Container, TEXT("Root"));
        Container->SetRootComponent(Root);
        Root->SetMobility(EComponentMobility::Movable);
        Container->AddInstanceComponent(Root);
        Root->RegisterComponent();
        // ISM avoids HISM per-cluster culling that popped distant rocks in late.
        UInstancedStaticMeshComponent* Hism =
            NewObject<UInstancedStaticMeshComponent>(Container);
        Hism->SetupAttachment(Root);
        Hism->SetMobility(EComponentMobility::Movable);
        Hism->SetStaticMesh(Mesh);
        if (bCollisionOnly)
        {
            ConfigureHiddenCollision(Hism);
        }
        else
        {
            ConfigureVisualPlacement(Hism);
        }
        Container->AddInstanceComponent(Hism);
        Hism->RegisterComponent();

        AActor* CollisionContainer = nullptr;
        UInstancedStaticMeshComponent* CollisionHism = nullptr;
        if (CollisionMesh)
        {
            CollisionContainer = World->SpawnActor<AActor>();
            if (CollisionContainer)
            {
                USceneComponent* CollisionRoot = NewObject<USceneComponent>(CollisionContainer, TEXT("Root"));
                CollisionContainer->SetRootComponent(CollisionRoot);
                CollisionRoot->SetMobility(EComponentMobility::Movable);
                CollisionContainer->AddInstanceComponent(CollisionRoot);
                CollisionRoot->RegisterComponent();

                CollisionHism = NewObject<UInstancedStaticMeshComponent>(CollisionContainer);
                CollisionHism->SetupAttachment(CollisionRoot);
                CollisionHism->SetMobility(EComponentMobility::Movable);
                CollisionHism->SetStaticMesh(CollisionMesh);
                ConfigureHiddenCollision(CollisionHism);
                CollisionContainer->AddInstanceComponent(CollisionHism);
                CollisionHism->RegisterComponent();
                CollisionContainer->SetActorHiddenInGame(true);
            }
        }

        for (const TSharedPtr<FJsonValue>& InstanceValue : *Instances)
        {
            const TSharedPtr<FJsonObject> Instance = InstanceValue->AsObject();
            if (!Instance.IsValid())
            {
                continue;
            }
            const FVector Location = JsonVector(Instance, TEXT("location"), FVector::ZeroVector);
            const FQuat Rotation = JsonQuat(Instance, TEXT("rotation"));
            const FVector ScaleVec = JsonVector(Instance, TEXT("scale"), FVector::OneVector);
            if (!IsUsablePlacementTransform(Location, Rotation, ScaleVec))
            {
                AddWarning(FString::Printf(
                    TEXT("Placement instancer '%s' has an invalid or zero-scale instance transform (layer '%s'); instance skipped."),
                    *InstancerName,
                    *LayerId));
                continue;
            }
            const FTransform InstanceTransform(Rotation, Location, ScaleVec);
            Hism->AddInstance(InstanceTransform, /*bWorldSpace=*/true);
            if (CollisionHism)
            {
                CollisionHism->AddInstance(InstanceTransform, /*bWorldSpace=*/true);
            }
            ++InstanceCount;
        }
        PlaceActorCommon(Container, InstancerName, bCollisionOnly ? CollisionFolder : MeshFolder);
        if (bCollisionOnly)
        {
            Container->SetActorHiddenInGame(true);
            HideEngineActorInEditor(Container);
            if (bDefaultHidden)
            {
                MarkDefaultHidden(Container);
            }
            ++CollisionActorCount;
        }
        else
        {
            if (bEngineHidden)
            {
                MarkEngineHidden(Container);
            }
            if (bDefaultHidden)
            {
                MarkDefaultHidden(Container);
            }
            if (CollisionContainer)
            {
                PlaceActorCommon(CollisionContainer, InstancerName + TEXT("_Collision"), CollisionFolder);
                HideEngineActorInEditor(CollisionContainer);
                if (bDefaultHidden)
                {
                    MarkDefaultHidden(CollisionContainer);
                }
                ++CollisionActorCount;
            }
        }
    }
}

void FWitcherImportContext::ImportPlacementLights(const FPlacementLayer& Layer, const TArray<TSharedPtr<FJsonValue>>& Lights, FPlacementLayerStats& Stats)
{
    UWorld* World = Layer.World;
    const FString& LightFolder = Layer.LightFolder;
    int32& LightCount = Stats.Lights;
    auto PlaceActorCommon = [&](AActor* Actor, const FString& ActorName, const FString& ActorFolder)
    {
        TagPlacementActor(Layer, Actor, ActorName, ActorFolder);
    };

    for (const TSharedPtr<FJsonValue>& LightValue : Lights)
    {
        const TSharedPtr<FJsonObject> LightEntry = LightValue->AsObject();
        if (!LightEntry.IsValid())
        {
            continue;
        }
        const TSharedPtr<FJsonObject>* TransformPtr = nullptr;
        LightEntry->TryGetObjectField(TEXT("transform"), TransformPtr);
        const TSharedPtr<FJsonObject> Transform = TransformPtr ? *TransformPtr : nullptr;
        const FVector Location = JsonVector(Transform, TEXT("location"), FVector::ZeroVector);
        const FString LightType = JsonString(LightEntry, TEXT("type"), TEXT("point")).ToLower();
        const FString LightName = JsonString(LightEntry, TEXT("name"), LightType == TEXT("spot") ? TEXT("SpotLight") : TEXT("PointLight"));
        const bool bDefaultHidden = JsonBool(LightEntry, TEXT("default_hidden"), false);

        if (LightType == TEXT("spot"))
        {
            ASpotLight* SpotActor = World->SpawnActor<ASpotLight>();
            if (!SpotActor)
            {
                continue;
            }
            FVector Direction = JsonVector(LightEntry, TEXT("direction"), FVector::ForwardVector);
            if (!Direction.Normalize())
            {
                Direction = FVector::ForwardVector;
            }
            SpotActor->SetActorLocation(Location);
            SpotActor->SetActorRotation(Direction.Rotation());
            if (USpotLightComponent* Component = SpotActor->FindComponentByClass<USpotLightComponent>())
            {
                ConfigurePointLight(Component, LightEntry);
                const float OuterCone = static_cast<float>(JsonNumber(LightEntry, TEXT("outer_cone_angle"), 44.0));
                const float InnerCone = static_cast<float>(JsonNumber(LightEntry, TEXT("inner_cone_angle"), 0.0));
                Component->SetOuterConeAngle(FMath::Max(0.0f, OuterCone));
                Component->SetInnerConeAngle(FMath::Clamp(InnerCone, 0.0f, FMath::Max(0.0f, OuterCone)));
            }
            PlaceActorCommon(SpotActor, LightName, LightFolder);
            if (bDefaultHidden)
            {
                MarkDefaultHidden(SpotActor);
            }
            ++LightCount;
        }
        else
        {
            APointLight* PointActor = World->SpawnActor<APointLight>();
            if (!PointActor)
            {
                continue;
            }
            PointActor->SetActorLocation(Location);
            if (UPointLightComponent* Component = PointActor->FindComponentByClass<UPointLightComponent>())
            {
                ConfigurePointLight(Component, LightEntry);
            }
            PlaceActorCommon(PointActor, LightName, LightFolder);
            if (bDefaultHidden)
            {
                MarkDefaultHidden(PointActor);
            }
            ++LightCount;
        }
    }
}

void FWitcherImportContext::ImportPlacementEntities(const FPlacementLayer& Layer, const TArray<TSharedPtr<FJsonValue>>& Entities, FPlacementLayerStats& Stats)
{
    UWorld* World = Layer.World;
    const FString& LayerId = Layer.LayerId;
    const FString& EntityFolder = Layer.EntityFolder;
    int32& EntityCount = Stats.Entities;
    int32& EntityComponentCount = Stats.EntityComponents;
    auto PlaceActorCommon = [&](AActor* Actor, const FString& ActorName, const FString& ActorFolder)
    {
        TagPlacementActor(Layer, Actor, ActorName, ActorFolder);
    };

    for (const TSharedPtr<FJsonValue>& EntityValue : Entities)
    {
        const TSharedPtr<FJsonObject> EntityEntry = EntityValue->AsObject();
        if (!EntityEntry.IsValid())
        {
            continue;
        }
        const TArray<TSharedPtr<FJsonValue>>* Components = nullptr;
        if (!EntityEntry->TryGetArrayField(TEXT("components"), Components) || Components->Num() == 0)
        {
            continue;
        }
        const FString EntityName = JsonString(EntityEntry, TEXT("name"), TEXT("Entity"));
        const bool bDefaultHidden = JsonBool(EntityEntry, TEXT("default_hidden"), false);

        const TSharedPtr<FJsonObject>* EntityTransformPtr = nullptr;
        EntityEntry->TryGetObjectField(TEXT("transform"), EntityTransformPtr);
        const TSharedPtr<FJsonObject> EntityTransform = EntityTransformPtr ? *EntityTransformPtr : nullptr;
        const FVector EntityLocation = JsonVector(EntityTransform, TEXT("location"), FVector::ZeroVector);
        const FQuat EntityRotation = JsonQuat(EntityTransform, TEXT("rotation"));
        const FVector EntityScale = JsonVector(EntityTransform, TEXT("scale"), FVector::OneVector);

        int32 NumEngineHiddenComps = 0;
        int32 NumVisibleComps = 0;
        for (const TSharedPtr<FJsonValue>& ComponentValue : *Components)
        {
            const TSharedPtr<FJsonObject> ComponentEntry = ComponentValue->AsObject();
            if (!ComponentEntry.IsValid())
            {
                continue;
            }
            if (JsonBool(ComponentEntry, TEXT("engine_hidden"), false))
            {
                ++NumEngineHiddenComps;
            }
            else
            {
                ++NumVisibleComps;
            }
        }

        // Split engine-hidden components so actor-level visibility tools can count them.
        auto BuildEntitySubsetActor = [&](bool bEngineHiddenSubset) -> AActor*
        {
            AActor* EntityActor = World->SpawnActor<AActor>();
            if (!EntityActor)
            {
                return nullptr;
            }
            USceneComponent* Root = NewObject<USceneComponent>(EntityActor, TEXT("Root"));
            EntityActor->SetRootComponent(Root);
            Root->SetMobility(EComponentMobility::Movable);
            EntityActor->AddInstanceComponent(Root);
            Root->RegisterComponent();
            if (IsUsablePlacementTransform(EntityLocation, EntityRotation, EntityScale))
            {
                EntityActor->SetActorTransform(FTransform(EntityRotation, EntityLocation, EntityScale));
            }

            int32 PlacedComponents = 0;
            for (const TSharedPtr<FJsonValue>& ComponentValue : *Components)
            {
                const TSharedPtr<FJsonObject> ComponentEntry = ComponentValue->AsObject();
                if (!ComponentEntry.IsValid())
                {
                    continue;
                }
                if (JsonBool(ComponentEntry, TEXT("engine_hidden"), false) != bEngineHiddenSubset)
                {
                    continue;
                }
                const FString AssetRel = JsonString(ComponentEntry, TEXT("asset_path"));
                UStaticMesh* Mesh = FindPlacementMesh(AssetRel);
                if (!Mesh)
                {
                    AddWarning(FString::Printf(
                        TEXT("Entity '%s' component mesh not found for '%s' (layer '%s')."),
                        *EntityName, *AssetRel, *LayerId));
                    continue;
                }
                const TSharedPtr<FJsonObject>* CompTransformPtr = nullptr;
                ComponentEntry->TryGetObjectField(TEXT("transform"), CompTransformPtr);
                const TSharedPtr<FJsonObject> CompTransform = CompTransformPtr ? *CompTransformPtr : nullptr;
                const FVector CompLocation = JsonVector(CompTransform, TEXT("location"), FVector::ZeroVector);
                const FQuat CompRotation = JsonQuat(CompTransform, TEXT("rotation"));
                const FVector CompScale = JsonVector(CompTransform, TEXT("scale"), FVector::OneVector);
                if (!IsUsablePlacementTransform(CompLocation, CompRotation, CompScale))
                {
                    AddWarning(FString::Printf(
                        TEXT("Entity '%s' component '%s' has an invalid transform (layer '%s'); skipped."),
                        *EntityName, *AssetRel, *LayerId));
                    continue;
                }

                const FString ComponentName = JsonString(ComponentEntry, TEXT("name"), TEXT("Mesh"));
                UStaticMeshComponent* MeshComponent =
                    NewObject<UStaticMeshComponent>(EntityActor, MakeUniqueObjectName(EntityActor, UStaticMeshComponent::StaticClass(), FName(*ComponentName)));
                MeshComponent->SetupAttachment(Root);
                MeshComponent->SetMobility(EComponentMobility::Movable);
                MeshComponent->SetStaticMesh(Mesh);
                ConfigureVisualPlacement(MeshComponent);
                EntityActor->AddInstanceComponent(MeshComponent);
                MeshComponent->RegisterComponent();
                MeshComponent->SetRelativeTransform(FTransform(CompRotation, CompLocation, CompScale));
                ++PlacedComponents;
                ++EntityComponentCount;

                // W3 collision ships as a separate mesh at the same relative transform.
                const FString CompCollisionRel = JsonString(ComponentEntry, TEXT("collision_asset_path"));
                if (!CompCollisionRel.IsEmpty())
                {
                    if (UStaticMesh* CollisionMesh = FindPlacementMesh(CompCollisionRel))
                    {
                        UStaticMeshComponent* CollisionComponent =
                            NewObject<UStaticMeshComponent>(EntityActor, MakeUniqueObjectName(EntityActor, UStaticMeshComponent::StaticClass(), FName(*(ComponentName + TEXT("_Collision")))));
                        CollisionComponent->SetupAttachment(Root);
                        CollisionComponent->SetMobility(EComponentMobility::Movable);
                        CollisionComponent->SetStaticMesh(CollisionMesh);
                        ConfigureHiddenCollision(CollisionComponent);
                        EntityActor->AddInstanceComponent(CollisionComponent);
                        CollisionComponent->RegisterComponent();
                        CollisionComponent->SetRelativeTransform(FTransform(CompRotation, CompLocation, CompScale));
                    }
                    else
                    {
                        AddWarning(FString::Printf(
                            TEXT("Entity '%s' collision mesh not found for '%s' (layer '%s')."),
                            *EntityName, *CompCollisionRel, *LayerId));
                    }
                }
            }

            if (PlacedComponents == 0)
            {
                World->DestroyActor(EntityActor);
                return nullptr;
            }
            return EntityActor;
        };

        bool bSpawnedEntity = false;
        if (NumVisibleComps > 0)
        {
            if (AActor* VisibleEntity = BuildEntitySubsetActor(/*bEngineHiddenSubset=*/false))
            {
                PlaceActorCommon(VisibleEntity, EntityName, EntityFolder);
                if (bDefaultHidden)
                {
                    MarkDefaultHidden(VisibleEntity);
                }
                bSpawnedEntity = true;
            }
        }
        if (NumEngineHiddenComps > 0)
        {
            if (AActor* HiddenEntity = BuildEntitySubsetActor(/*bEngineHiddenSubset=*/true))
            {
                PlaceActorCommon(HiddenEntity, EntityName + TEXT("_EngineHidden"), EntityFolder);
                MarkEngineHidden(HiddenEntity);
                if (bDefaultHidden)
                {
                    MarkDefaultHidden(HiddenEntity);
                }
                bSpawnedEntity = true;
            }
        }
        if (bSpawnedEntity)
        {
            ++EntityCount;
        }
    }
}

void FWitcherImportContext::ImportPlacements()
{
    const TSharedPtr<FJsonObject>* PlacementsPtr = nullptr;
    if (!Manifest->TryGetObjectField(TEXT("placements"), PlacementsPtr) || !PlacementsPtr)
    {
        return;
    }
    const TArray<TSharedPtr<FJsonValue>>* Layers = nullptr;
    if (!(*PlacementsPtr)->TryGetArrayField(TEXT("layers"), Layers))
    {
        return;
    }

    UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
    if (!World)
    {
        AddError(TEXT("Placement import: no editor world available."));
        return;
    }

    FAssetCompilingManager::Get().FinishAllCompilation();

    for (const TSharedPtr<FJsonValue>& LayerValue : *Layers)
    {
        const TSharedPtr<FJsonObject> Layer = LayerValue->AsObject();
        if (!Layer.IsValid())
        {
            continue;
        }
        const FString LayerId = JsonString(Layer, TEXT("layer_id"), TEXT("placements"));
        const FString Folder = JsonString(Layer, TEXT("folder"));

        FString LayerRoot = JsonString(Layer, TEXT("label"), LayerId);
        LayerRoot.ReplaceInline(TEXT("\\"), TEXT("/"));
        if (!Folder.IsEmpty())
        {
            LayerRoot = Folder + TEXT("/") + LayerRoot;
        }
        const FString SectorRoot = LayerRoot + TEXT("/CSectorData");

        FPlacementLayer LayerCtx;
        LayerCtx.World = World;
        LayerCtx.LayerId = LayerId;
        LayerCtx.LayerTag = FName(*(FString(TEXT("WitcherLayer:")) + LayerId));
        LayerCtx.MeshFolder = SectorRoot + TEXT("/Mesh");
        LayerCtx.CollisionFolder = SectorRoot + TEXT("/Collision");
        LayerCtx.LightFolder = SectorRoot + TEXT("/Lights");
        LayerCtx.EntityFolder = LayerRoot;

        if (!ArePlacementLayerMeshesReady(Layer, LayerId))
        {
            continue;
        }

        // Re-sent layers replace their previous actors without touching other layers.
        for (TActorIterator<AActor> It(World); It; ++It)
        {
            if (It->Tags.Contains(LayerCtx.LayerTag))
            {
                World->DestroyActor(*It);
            }
        }

        FPlacementLayerStats Stats;
        const TArray<TSharedPtr<FJsonValue>>* Actors = nullptr;
        if (Layer->TryGetArrayField(TEXT("actors"), Actors))
        {
            ImportPlacementActors(LayerCtx, *Actors, Stats);
        }
        const TArray<TSharedPtr<FJsonValue>>* Instancers = nullptr;
        if (Layer->TryGetArrayField(TEXT("instancers"), Instancers))
        {
            ImportPlacementInstancers(LayerCtx, *Instancers, Stats);
        }
        const TArray<TSharedPtr<FJsonValue>>* Lights = nullptr;
        if (Layer->TryGetArrayField(TEXT("lights"), Lights))
        {
            ImportPlacementLights(LayerCtx, *Lights, Stats);
        }
        const TArray<TSharedPtr<FJsonValue>>* Entities = nullptr;
        if (Layer->TryGetArrayField(TEXT("entities"), Entities))
        {
            ImportPlacementEntities(LayerCtx, *Entities, Stats);
        }

        UE_LOG(LogWitcherImportContext, Log,
            TEXT("Layer '%s': %d actor(s), %d collision actor(s), %d instanced placement(s), %d light(s), %d entity(ies) (%d component(s))"),
            *LayerId,
            Stats.Actors,
            Stats.CollisionActors,
            Stats.Instances,
            Stats.Lights,
            Stats.Entities,
            Stats.EntityComponents);
    }
}
