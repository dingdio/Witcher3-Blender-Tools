#include "WitcherImportContextInternal.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"

DEFINE_LOG_CATEGORY(LogWitcherImportContext);

namespace WitcherImportInternal
{
FString JsonString(const TSharedPtr<FJsonObject>& Object, const FString& Field, const FString& DefaultValue)
{
    if (!Object.IsValid())
    {
        return DefaultValue;
    }
    FString Value;
    return Object->TryGetStringField(Field, Value) ? Value : DefaultValue;
}

int32 JsonInt(const TSharedPtr<FJsonObject>& Object, const FString& Field, int32 DefaultValue)
{
    if (!Object.IsValid())
    {
        return DefaultValue;
    }
    return Object->HasTypedField<EJson::Number>(Field) ? Object->GetIntegerField(Field) : DefaultValue;
}

bool JsonBool(const TSharedPtr<FJsonObject>& Object, const FString& Field, bool DefaultValue)
{
    if (!Object.IsValid())
    {
        return DefaultValue;
    }
    return Object->HasTypedField<EJson::Boolean>(Field) ? Object->GetBoolField(Field) : DefaultValue;
}

double JsonNumber(const TSharedPtr<FJsonObject>& Object, const FString& Field, double DefaultValue)
{
    if (!Object.IsValid())
    {
        return DefaultValue;
    }
    return Object->HasTypedField<EJson::Number>(Field) ? Object->GetNumberField(Field) : DefaultValue;
}

FLinearColor JsonColor(const TSharedPtr<FJsonObject>& Object, const FString& Field)
{
    FLinearColor Color(1.0f, 1.0f, 1.0f, 1.0f);
    const TArray<TSharedPtr<FJsonValue>>* Values = nullptr;
    if (Object.IsValid() && Object->TryGetArrayField(Field, Values) && Values->Num() >= 3)
    {
        Color.R = static_cast<float>((*Values)[0]->AsNumber());
        Color.G = static_cast<float>((*Values)[1]->AsNumber());
        Color.B = static_cast<float>((*Values)[2]->AsNumber());
        Color.A = Values->Num() > 3 ? static_cast<float>((*Values)[3]->AsNumber()) : 1.0f;
    }
    return Color;
}

FVector JsonVector(const TSharedPtr<FJsonObject>& Object, const FString& Field, const FVector& DefaultValue)
{
    const TArray<TSharedPtr<FJsonValue>>* Values = nullptr;
    if (Object.IsValid() && Object->TryGetArrayField(Field, Values) && Values->Num() >= 3)
    {
        return FVector(
            (*Values)[0]->AsNumber(),
            (*Values)[1]->AsNumber(),
            (*Values)[2]->AsNumber());
    }
    return DefaultValue;
}

FQuat JsonQuat(const TSharedPtr<FJsonObject>& Object, const FString& Field)
{
    const TArray<TSharedPtr<FJsonValue>>* Values = nullptr;
    if (Object.IsValid() && Object->TryGetArrayField(Field, Values) && Values->Num() >= 4)
    {
        FQuat Quat(
            (*Values)[0]->AsNumber(),
            (*Values)[1]->AsNumber(),
            (*Values)[2]->AsNumber(),
            (*Values)[3]->AsNumber());
        Quat.Normalize();
        return Quat;
    }
    return FQuat::Identity;
}

bool IsFiniteVector(const FVector& Value)
{
    return FMath::IsFinite(Value.X) && FMath::IsFinite(Value.Y) && FMath::IsFinite(Value.Z);
}

bool IsFiniteQuat(const FQuat& Value)
{
    return FMath::IsFinite(Value.X) && FMath::IsFinite(Value.Y) &&
        FMath::IsFinite(Value.Z) && FMath::IsFinite(Value.W);
}

bool IsUsablePlacementTransform(const FVector& Location, const FQuat& Rotation, const FVector& ScaleVec)
{
    constexpr double MinScale = 1.0e-6;
    return IsFiniteVector(Location) &&
        IsFiniteQuat(Rotation) &&
        IsFiniteVector(ScaleVec) &&
        FMath::Abs(ScaleVec.X) > MinScale &&
        FMath::Abs(ScaleVec.Y) > MinScale &&
        FMath::Abs(ScaleVec.Z) > MinScale;
}

FString AssetRelName(const FString& AssetRel)
{
    FString Name = AssetRel;
    int32 SlashIndex = INDEX_NONE;
    if (AssetRel.FindLastChar(TEXT('/'), SlashIndex))
    {
        Name = AssetRel.Mid(SlashIndex + 1);
    }
    return Name;
}
}
