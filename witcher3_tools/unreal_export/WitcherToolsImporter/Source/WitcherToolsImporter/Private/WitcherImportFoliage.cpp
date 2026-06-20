#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

using namespace WitcherImportInternal;

namespace
{
FString NormalizeUnrealRelativePath(FString Path)
{
    Path = Path.TrimStartAndEnd();
    if (Path.StartsWith(TEXT("\"")) && Path.EndsWith(TEXT("\"")) && Path.Len() >= 2)
    {
        Path = Path.Mid(1, Path.Len() - 2);
    }
    Path.ReplaceInline(TEXT("\\"), TEXT("/"));
    while (Path.Contains(TEXT("//")))
    {
        Path.ReplaceInline(TEXT("//"), TEXT("/"));
    }
    while (Path.StartsWith(TEXT("/")))
    {
        Path.RemoveAt(0);
    }
    while (Path.EndsWith(TEXT("/")))
    {
        Path.LeftChopInline(1);
    }
    return Path;
}

FString ParentFolderOfRelativePath(const FString& Path)
{
    const FString Normalized = NormalizeUnrealRelativePath(Path);
    int32 SlashIndex = INDEX_NONE;
    return Normalized.FindLastChar(TEXT('/'), SlashIndex)
        ? Normalized.Left(SlashIndex)
        : FString();
}

bool RelativePathHasSegment(const FString& Path, const FString& Segment)
{
    TArray<FString> Segments;
    NormalizeUnrealRelativePath(Path).ParseIntoArray(Segments, TEXT("/"), true);
    for (const FString& ExistingSegment : Segments)
    {
        if (ExistingSegment.Equals(Segment, ESearchCase::IgnoreCase))
        {
            return true;
        }
    }
    return false;
}

FString FoliageFolderForCell(const TSharedPtr<FJsonObject>& Cell, const FString& LayerId)
{
    FString Folder = NormalizeUnrealRelativePath(JsonString(Cell, TEXT("folder")));
    if (Folder.IsEmpty())
    {
        Folder = ParentFolderOfRelativePath(LayerId);
    }
    if (Folder.IsEmpty())
    {
        return TEXT("source_foliage");
    }
    if (!RelativePathHasSegment(Folder, TEXT("source_foliage")))
    {
        Folder /= TEXT("source_foliage");
    }
    return Folder;
}

FString FoliageTypeRelForMesh(const FString& MeshAssetRel, const FString& FoliageFolder)
{
    const FString Folder = NormalizeUnrealRelativePath(FoliageFolder).IsEmpty()
        ? FString(TEXT("source_foliage"))
        : NormalizeUnrealRelativePath(FoliageFolder);
    const FString MeshRel = NormalizeUnrealRelativePath(MeshAssetRel);
    return MeshRel.IsEmpty()
        ? FString::Printf(TEXT("%s/FoliageType"), *Folder)
        : FString::Printf(TEXT("%s/%s_FoliageType"), *Folder, *MeshRel);
}

enum class EWitcherFoliageProfile : uint8
{
    GroundCover,
    SmallWoody,
    LargeTree,
};

bool FoliagePathHasToken(const FString& MeshAssetRel, const TCHAR* Token)
{
    FString Path = NormalizeUnrealRelativePath(MeshAssetRel).ToLower();
    if (Path.IsEmpty())
    {
        return false;
    }
    Path = FString::Printf(TEXT("/%s/"), *Path);
    return Path.Contains(FString::Printf(TEXT("/%s/"), Token)) || Path.Contains(Token);
}

EWitcherFoliageProfile GuessFoliageProfile(UStaticMesh* Mesh, const FString& MeshAssetRel)
{
    const double HeightCm = Mesh ? Mesh->GetBounds().BoxExtent.Z * 2.0 : 0.0;

    if (FoliagePathHasToken(MeshAssetRel, TEXT("grass")) ||
        FoliagePathHasToken(MeshAssetRel, TEXT("flower")) ||
        FoliagePathHasToken(MeshAssetRel, TEXT("herb")) ||
        (HeightCm > 0.0 && HeightCm <= 180.0))
    {
        return EWitcherFoliageProfile::GroundCover;
    }

    if (FoliagePathHasToken(MeshAssetRel, TEXT("bush")) ||
        FoliagePathHasToken(MeshAssetRel, TEXT("shrub")) ||
        FoliagePathHasToken(MeshAssetRel, TEXT("reed")) ||
        (HeightCm > 0.0 && HeightCm <= 650.0))
    {
        return EWitcherFoliageProfile::SmallWoody;
    }

    return EWitcherFoliageProfile::LargeTree;
}

void ApplyGeneratedFoliageSettings(
    UFoliageType_InstancedStaticMesh* FoliageType,
    UStaticMesh* Mesh,
    const FString& MeshAssetRel)
{
    if (!FoliageType)
    {
        return;
    }

    const EWitcherFoliageProfile Profile = GuessFoliageProfile(Mesh, MeshAssetRel);

    FoliageType->bEvaluateWorldPositionOffset = true;
    FoliageType->bEnableCullDistanceScaling = true;
    FoliageType->bEnableDiscardOnLoad = false;
    FoliageType->ShadowCacheInvalidationBehavior = EShadowCacheInvalidationBehavior::Rigid;

    switch (Profile)
    {
    case EWitcherFoliageProfile::GroundCover:
        FoliageType->CullDistance = FInt32Interval(9000, 14000);
        FoliageType->WorldPositionOffsetDisableDistance = 7000;
        FoliageType->CastShadow = false;
        FoliageType->bCastDynamicShadow = false;
        FoliageType->bCastStaticShadow = false;
        FoliageType->bCastContactShadow = false;
        FoliageType->bCastShadowAsTwoSided = false;
        FoliageType->bAffectDynamicIndirectLighting = false;
        FoliageType->bAffectDistanceFieldLighting = false;
        FoliageType->bReceivesDecals = false;
        FoliageType->bUseAsOccluder = false;
        FoliageType->bVisibleInRayTracing = false;
        FoliageType->bVisibleInReflections = false;
        FoliageType->bEnableDensityScaling = true;
        FoliageType->BodyInstance.SetCollisionProfileName(FName(TEXT("NoCollision")));
        FoliageType->BodyInstance.SetCollisionEnabled(ECollisionEnabled::NoCollision);
        break;

    case EWitcherFoliageProfile::SmallWoody:
        FoliageType->CullDistance = FInt32Interval(25000, 45000);
        FoliageType->WorldPositionOffsetDisableDistance = 20000;
        FoliageType->CastShadow = true;
        FoliageType->bCastDynamicShadow = true;
        FoliageType->bCastStaticShadow = true;
        FoliageType->bCastContactShadow = false;
        FoliageType->bCastShadowAsTwoSided = false;
        FoliageType->bAffectDynamicIndirectLighting = false;
        FoliageType->bAffectDistanceFieldLighting = false;
        FoliageType->bReceivesDecals = false;
        FoliageType->bUseAsOccluder = false;
        FoliageType->bVisibleInRayTracing = false;
        FoliageType->bVisibleInReflections = false;
        FoliageType->bEnableDensityScaling = false;
        FoliageType->BodyInstance.SetCollisionProfileName(FName(TEXT("BlockAll")));
        FoliageType->BodyInstance.SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
        break;

    case EWitcherFoliageProfile::LargeTree:
        FoliageType->CullDistance = FInt32Interval(100000, 180000);
        FoliageType->WorldPositionOffsetDisableDistance = 50000;
        FoliageType->CastShadow = true;
        FoliageType->bCastDynamicShadow = true;
        FoliageType->bCastStaticShadow = true;
        FoliageType->bCastContactShadow = false;
        FoliageType->bCastShadowAsTwoSided = false;
        FoliageType->bAffectDynamicIndirectLighting = false;
        FoliageType->bAffectDistanceFieldLighting = true;
        FoliageType->bReceivesDecals = false;
        FoliageType->bUseAsOccluder = false;
        FoliageType->bVisibleInRayTracing = true;
        FoliageType->bVisibleInReflections = true;
        FoliageType->bEnableDensityScaling = false;
        FoliageType->BodyInstance.SetCollisionProfileName(FName(TEXT("BlockAll")));
        FoliageType->BodyInstance.SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
        break;
    }

    ++FoliageType->ChangeCount;
    FoliageType->UpdateGuid = FGuid::NewGuid();
}

bool JsonVector2D(const TSharedPtr<FJsonObject>& Object, const FString& Field, FVector2D& OutValue)
{
    const TArray<TSharedPtr<FJsonValue>>* Values = nullptr;
    double X = 0.0;
    double Y = 0.0;
    if (Object.IsValid()
        && Object->TryGetArrayField(Field, Values)
        && Values->Num() >= 2
        && (*Values)[0].IsValid()
        && (*Values)[1].IsValid()
        && (*Values)[0]->TryGetNumber(X)
        && (*Values)[1]->TryGetNumber(Y))
    {
        OutValue = FVector2D(X, Y);
        return true;
    }
    return false;
}
}

UFoliageType_InstancedStaticMesh* FWitcherImportContext::EnsureFoliageType(
    UStaticMesh* Mesh,
    const FString& MeshAssetRel,
    const FString& FoliageFolder)
{
    // Keep generated FoliageTypes grouped by source layer.
    const FString FtRel = FoliageTypeRelForMesh(MeshAssetRel, FoliageFolder);
    UFoliageType_InstancedStaticMesh* FoliageType =
        LoadExistingAsset<UFoliageType_InstancedStaticMesh>(ObjectPathFor(FtRel));
    const bool bExisting = FoliageType != nullptr;
    if (bExisting && !ShouldOverwrite(TEXT("meshes")))
    {
        ApplyGeneratedFoliageSettings(FoliageType, FoliageType->GetStaticMesh() ? FoliageType->GetStaticMesh() : Mesh, MeshAssetRel);
        FoliageType->MarkPackageDirty();
        ImportedAssets.AddUnique(FoliageType->GetPathName());
        return FoliageType;
    }
    if (!FoliageType)
    {
        const FString PackageName = FString::Printf(TEXT("%s/%s"), *ContentRoot, *FtRel);
        UPackage* Package = CreatePackage(*PackageName);
        FoliageType = NewObject<UFoliageType_InstancedStaticMesh>(
            Package, *AssetRelName(FtRel), RF_Public | RF_Standalone);
    }
    if (!FoliageType)
    {
        return nullptr;
    }
    FoliageType->SetStaticMesh(Mesh);
    ApplyGeneratedFoliageSettings(FoliageType, Mesh, MeshAssetRel);
    if (!bExisting)
    {
        FAssetRegistryModule::AssetCreated(FoliageType);
    }
    FoliageType->MarkPackageDirty();
    ImportedAssets.AddUnique(FoliageType->GetPathName());
    return FoliageType;
}

void FWitcherImportContext::ImportFoliage()
{
    const TSharedPtr<FJsonObject>* FoliagePtr = nullptr;
    if (!Manifest->TryGetObjectField(TEXT("foliage"), FoliagePtr) || !FoliagePtr)
    {
        return;
    }
    const TArray<TSharedPtr<FJsonValue>>* Cells = nullptr;
    if (!(*FoliagePtr)->TryGetArrayField(TEXT("cells"), Cells))
    {
        return;
    }

    UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
    if (!World)
    {
        AddError(TEXT("Foliage import: no editor world available."));
        return;
    }
    // FoliageTypes should reference fully-built SpeedTree meshes.
    FAssetCompilingManager::Get().FinishAllCompilation();

    // Foliage edit mode can crash during programmatic instance edits.
    const bool bCanTouchEditorModes = !IsRunningCommandlet();
    const bool bFoliageModeWasActive =
        bCanTouchEditorModes && GLevelEditorModeTools().IsModeActive(FBuiltinEditorModes::EM_Foliage);
    if (bFoliageModeWasActive)
    {
        GLevelEditorModeTools().DeactivateMode(FBuiltinEditorModes::EM_Foliage);
    }
    auto RestoreFoliageMode = [bCanTouchEditorModes, bFoliageModeWasActive]()
    {
        if (bCanTouchEditorModes && bFoliageModeWasActive)
        {
            GLevelEditorModeTools().ActivateMode(FBuiltinEditorModes::EM_Foliage);
        }
    };

    // UE 5.8 can dereference a null foliage base component.
    IFoliageEditModuleBase* FoliageEditModule = IFoliageEditModuleBase::Get();
    USceneComponent* NullBaseComponentSentinel = nullptr;
    bool bRegisteredNullBaseIgnore = false;
    if (FoliageEditModule)
    {
        NullBaseComponentSentinel = NewObject<USceneComponent>(GetTransientPackage(), NAME_None, RF_Transient);
        if (NullBaseComponentSentinel && !FoliageEditModule->ShouldIgnoreComponentForBaseID(NullBaseComponentSentinel))
        {
            FoliageEditModule->RegisterComponentBaseIDClassToIgnore(USceneComponent::StaticClass());
            bRegisteredNullBaseIgnore = true;
        }
    }
    ON_SCOPE_EXIT
    {
        if (FoliageEditModule && bRegisteredNullBaseIgnore)
        {
            FoliageEditModule->UnregisterComponentBaseIDClassToIgnore(USceneComponent::StaticClass());
        }
        RestoreFoliageMode();
    };

    int32 TypeCount = 0;
    int32 InstanceCount = 0;
    int32 RemovedInstanceCount = 0;

    auto RemoveExistingFoliageForMeshInBounds = [&](UStaticMesh* Mesh, const FBox& Bounds) -> int32
    {
        int32 RemovedForType = 0;
        for (TActorIterator<AInstancedFoliageActor> It(World); It; ++It)
        {
            It->ForEachFoliageInfo([&](UFoliageType* ExistingType, FFoliageInfo& Info)
            {
                const UFoliageType_InstancedStaticMesh* ExistingMeshType =
                    Cast<UFoliageType_InstancedStaticMesh>(ExistingType);
                if (!ExistingMeshType || ExistingMeshType->GetStaticMesh() != Mesh || !Info.IsInitialized())
                {
                    return true;
                }

                TArray<int32> ExistingInstances;
                Info.GetInstancesInsideBounds(Bounds, ExistingInstances);
                if (ExistingInstances.Num() > 0)
                {
                    Info.RemoveInstances(ExistingInstances, /*RebuildFoliageTree=*/true);
                    RemovedForType += ExistingInstances.Num();
                }
                return true;
            });
        }
        return RemovedForType;
    };

    for (const TSharedPtr<FJsonValue>& CellValue : *Cells)
    {
        const TSharedPtr<FJsonObject> Cell = CellValue->AsObject();
        if (!Cell.IsValid())
        {
            continue;
        }
        const FString LayerId = JsonString(Cell, TEXT("layer_id"), TEXT("foliage"));
        const FString FoliageFolder = FoliageFolderForCell(Cell, LayerId);

        FBox CellBounds(ForceInit);
        bool bHasCellBounds = false;
        const TSharedPtr<FJsonObject>* BoundsObject = nullptr;
        if (Cell->TryGetObjectField(TEXT("bounds"), BoundsObject) && BoundsObject && BoundsObject->IsValid())
        {
            FVector2D MinXY(0.0, 0.0);
            FVector2D MaxXY(0.0, 0.0);
            if (JsonVector2D(*BoundsObject, TEXT("min"), MinXY) && JsonVector2D(*BoundsObject, TEXT("max"), MaxXY))
            {
                CellBounds = FBox(
                    FVector(FMath::Min(MinXY.X, MaxXY.X), FMath::Min(MinXY.Y, MaxXY.Y), -1.0e12),
                    FVector(FMath::Max(MinXY.X, MaxXY.X), FMath::Max(MinXY.Y, MaxXY.Y), 1.0e12)).ExpandBy(1.0);
                bHasCellBounds = true;
            }
            else
            {
                AddWarning(FString::Printf(TEXT("Foliage cell '%s' has invalid bounds; existing instances will not be replaced."), *LayerId));
            }
        }

        const TArray<TSharedPtr<FJsonValue>>* Types = nullptr;
        if (!Cell->TryGetArrayField(TEXT("types"), Types))
        {
            continue;
        }

        for (const TSharedPtr<FJsonValue>& TypeValue : *Types)
        {
            const TSharedPtr<FJsonObject> Type = TypeValue->AsObject();
            if (!Type.IsValid())
            {
                continue;
            }
            const FString AssetRel = JsonString(Type, TEXT("asset_path"));
            const FString TypeName = JsonString(Type, TEXT("name"), TEXT("Foliage"));
            if (AssetRel.IsEmpty())
            {
                AddWarning(FString::Printf(TEXT("Foliage type '%s' has no mesh asset path (cell '%s'); skipped."), *TypeName, *LayerId));
                continue;
            }
            UStaticMesh* Mesh = FindPlacementMesh(AssetRel);
            if (!Mesh)
            {
                AddWarning(FString::Printf(TEXT("Foliage SpeedTree mesh not found for '%s' (cell '%s'); skipped."), *AssetRel, *LayerId));
                continue;
            }
            UFoliageType_InstancedStaticMesh* FoliageType = EnsureFoliageType(Mesh, AssetRel, FoliageFolder);
            if (!FoliageType)
            {
                AddWarning(FString::Printf(TEXT("Could not create a FoliageType for '%s' (cell '%s'); skipped."), *AssetRel, *LayerId));
                continue;
            }
            if (bHasCellBounds)
            {
                RemovedInstanceCount += RemoveExistingFoliageForMeshInBounds(Mesh, CellBounds);
            }

            const TArray<TSharedPtr<FJsonValue>>* Instances = nullptr;
            if (!Type->TryGetArrayField(TEXT("instances"), Instances) || Instances->Num() == 0)
            {
                continue;
            }

            TArray<FTransform> Transforms;
            Transforms.Reserve(Instances->Num());
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
                    AddWarning(FString::Printf(TEXT("Foliage type '%s' has an invalid instance transform (cell '%s'); instance skipped."), *TypeName, *LayerId));
                    continue;
                }
                Transforms.Emplace(Rotation, Location, ScaleVec);
            }

            if (Transforms.Num() > 0)
            {
                // AddInstances is BlueprintCallable but not FOLIAGE_API-exported.
                TMap<AInstancedFoliageActor*, TArray<const FFoliageInstance*>> InstancesByIFA;
                TArray<FFoliageInstance> FoliageInstances;
                FoliageInstances.Reserve(Transforms.Num());
                for (const FTransform& Xform : Transforms)
                {
                    AInstancedFoliageActor* IFA = AInstancedFoliageActor::Get(
                        World, /*bCreateIfNone=*/true, World->PersistentLevel, Xform.GetLocation());
                    if (!IFA)
                    {
                        continue;
                    }
                    if (!FoliageFolder.IsEmpty() && IFA->GetFolderPath().IsNone())
                    {
                        IFA->SetFolderPath(FName(*FoliageFolder));
                    }
                    FFoliageInstance FoliageInstance;
                    FoliageInstance.Location = Xform.GetLocation();
                    FoliageInstance.Rotation = Xform.GetRotation().Rotator();
                    FoliageInstance.DrawScale3D = (FVector3f)Xform.GetScale3D();
                    FoliageInstance.BaseComponent = NullBaseComponentSentinel;
                    const int32 Idx = FoliageInstances.Add(FoliageInstance);
                    InstancesByIFA.FindOrAdd(IFA).Add(&FoliageInstances[Idx]);
                }

                int32 AddedForType = 0;
                for (const TPair<AInstancedFoliageActor*, TArray<const FFoliageInstance*>>& Pair : InstancesByIFA)
                {
                    FFoliageInfo* Info = nullptr;
                    UFoliageType* LocalType = Pair.Key->AddFoliageType(FoliageType, &Info);
                    if (LocalType && Info)
                    {
                        Info->AddInstances(LocalType, Pair.Value);
                        AddedForType += Pair.Value.Num();
                    }
                }
                InstanceCount += AddedForType;
                if (AddedForType > 0)
                {
                    ++TypeCount;
                }
            }
        }
    }

    UE_LOG(LogWitcherImportContext, Log,
        TEXT("Foliage: %d type(s), %d instance(s) placed into the level InstancedFoliageActor; %d existing instance(s) replaced."),
        TypeCount, InstanceCount, RemovedInstanceCount);
}
