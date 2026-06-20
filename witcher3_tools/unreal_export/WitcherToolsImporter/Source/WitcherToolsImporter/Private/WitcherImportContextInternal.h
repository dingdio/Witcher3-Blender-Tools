#pragma once

#include "CoreMinimal.h"
#include "UObject/UObjectGlobals.h"

#include "WitcherMeshBuffer.h"
#include "WitcherPlacementTags.h"

#include "Framework/Notifications/NotificationManager.h"
#include "HAL/PlatformTime.h"
#include "Misc/ScopeExit.h"
#include "Misc/ScopedSlowTask.h"
#include "Templates/Function.h"
#include "Animation/AnimSequence.h"
#include "Animation/Skeleton.h"
#include "AssetCompilingManager.h"
#include "AssetImportTask.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetToolsModule.h"
#include "Components/InstancedStaticMeshComponent.h"
#include "Components/LightComponent.h"
#include "Components/PointLightComponent.h"
#include "Components/PrimitiveComponent.h"
#include "Components/SceneComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "Components/SpotLightComponent.h"
#include "Components/StaticMeshComponent.h"
#include "EngineUtils.h"
#include "FoliageType_InstancedStaticMesh.h"
#include "FoliageModule.h"
#include "InstancedFoliageActor.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "Engine/SCS_Node.h"
#include "Engine/SimpleConstructionScript.h"
#include "Engine/SkeletalMesh.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/Texture.h"
#include "Engine/Texture2D.h"
#include "Engine/Texture2DArray.h"
#include "Engine/PointLight.h"
#include "Engine/SpotLight.h"
#include "Editor.h"
#include "EditorModeManager.h"
#include "EditorModes.h"
#include "EditorFramework/AssetImportData.h"
#include "Landscape.h"
#include "LandscapeInfo.h"
#include "LandscapeProxy.h"
#include "LandscapeImportHelper.h"
#include "LandscapeLayerInfoObject.h"
#include "Materials/MaterialExpressionAppendVector.h"
#include "Materials/MaterialExpressionLandscapeVisibilityMask.h"
#include "Materials/MaterialExpressionComponentMask.h"
#include "Materials/MaterialExpressionCustom.h"
#include "Materials/MaterialExpressionConstant.h"
#include "Materials/MaterialExpressionConstant2Vector.h"
#include "Materials/MaterialExpressionConstant3Vector.h"
#include "Materials/MaterialExpressionConstant4Vector.h"
#include "Materials/MaterialExpressionDotProduct.h"
#include "Materials/MaterialExpressionDivide.h"
#include "Materials/MaterialExpressionLinearInterpolate.h"
#include "Materials/MaterialExpressionMax.h"
#include "Materials/MaterialExpressionMultiply.h"
#include "Materials/MaterialExpressionOneMinus.h"
#include "Materials/MaterialExpressionPower.h"
#include "Materials/MaterialExpressionSaturate.h"
#include "Materials/MaterialExpressionSquareRoot.h"
#include "Materials/MaterialExpressionSubtract.h"
#include "Materials/MaterialExpressionTextureObject.h"
#include "Materials/MaterialExpressionTextureSample.h"
#include "Materials/MaterialExpressionVertexNormalWS.h"
#include "Materials/MaterialExpressionWorldPosition.h"
#include "Factories/FbxAnimSequenceImportData.h"
#include "Factories/FbxImportUI.h"
#include "Factories/FbxSkeletalMeshImportData.h"
#include "Factories/FbxStaticMeshImportData.h"
#include "Factories/Factory.h"
#include "Factories/MaterialFactoryNew.h"
#include "Factories/TextureFactory.h"
#include "FileHelpers.h"
#include "GameFramework/Actor.h"
#include "HAL/IConsoleManager.h"
#include "IAssetTools.h"
#include "PhysicsEngine/BodyInstance.h"
#include "SceneTypes.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "MaterialEditingLibrary.h"
#include "Materials/Material.h"
#include "Materials/MaterialExpressionScalarParameter.h"
#include "Materials/MaterialExpressionSpeedTree.h"
#include "Materials/MaterialExpressionTextureBase.h"
#include "Materials/MaterialExpressionTextureSampleParameter2D.h"
#include "Materials/MaterialExpressionVectorParameter.h"
#include "Materials/MaterialInstanceConstant.h"
#include "Materials/MaterialInterface.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Modules/ModuleManager.h"
#include "PhysicsEngine/BodySetup.h"
#include "PhysicsEngine/SphylElem.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "UObject/UnrealType.h"
#include "UObject/UObjectIterator.h"
#include "WitcherImportedActor.h"

DECLARE_LOG_CATEGORY_EXTERN(LogWitcherImportContext, Log, All);

namespace WitcherImportInternal
{
    FString JsonString(const TSharedPtr<FJsonObject>& Object, const FString& Field, const FString& DefaultValue = TEXT(""));
    int32 JsonInt(const TSharedPtr<FJsonObject>& Object, const FString& Field, int32 DefaultValue = 0);
    bool JsonBool(const TSharedPtr<FJsonObject>& Object, const FString& Field, bool DefaultValue = false);
    double JsonNumber(const TSharedPtr<FJsonObject>& Object, const FString& Field, double DefaultValue = 0.0);
    FLinearColor JsonColor(const TSharedPtr<FJsonObject>& Object, const FString& Field);
    FVector JsonVector(const TSharedPtr<FJsonObject>& Object, const FString& Field, const FVector& DefaultValue);
    FQuat JsonQuat(const TSharedPtr<FJsonObject>& Object, const FString& Field);
    bool IsFiniteVector(const FVector& Value);
    bool IsFiniteQuat(const FQuat& Value);
    bool IsUsablePlacementTransform(const FVector& Location, const FQuat& Rotation, const FVector& ScaleVec);
    FString AssetRelName(const FString& AssetRel);

    template <typename T>
    T* LoadExistingAsset(const FString& ObjectPath)
    {
        return Cast<T>(StaticLoadObject(T::StaticClass(), nullptr, *ObjectPath, nullptr, LOAD_NoWarn | LOAD_Quiet));
    }
}
