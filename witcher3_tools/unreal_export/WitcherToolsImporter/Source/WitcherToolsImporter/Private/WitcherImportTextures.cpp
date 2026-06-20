#include "WitcherImportContext.h"
#include "WitcherImportContextInternal.h"

using namespace WitcherImportInternal;

namespace
{
void ApplyTextureImportSettings(UTexture* Texture, const TSharedPtr<FJsonObject>& TextureObject)
{
    if (!Texture)
    {
        return;
    }
    Texture->PreEditChange(nullptr);
    Texture->SRGB = JsonBool(TextureObject, TEXT("srgb"), false);
    const FString Compression = JsonString(TextureObject, TEXT("compression"));
    if (UTexture2D* Texture2D = Cast<UTexture2D>(Texture))
    {
        if (Compression == TEXT("normalmap"))
        {
            Texture2D->CompressionSettings = TC_Normalmap;
        }
        else if (Compression == TEXT("normal_rgba"))
        {
            Texture2D->CompressionSettings = TC_Default;
            Texture2D->CompressionNoAlpha = false;
            Texture2D->CompressionForceAlpha = true;
        }
        else if (Compression == TEXT("masks"))
        {
            Texture2D->CompressionSettings = TC_Masks;
        }
        else if (Compression == TEXT("indexmap"))
        {
            Texture2D->CompressionSettings = TC_Grayscale;
            Texture2D->MipGenSettings = TMGS_NoMipmaps;
            Texture2D->Filter = TF_Nearest;
        }
        else if (Compression == TEXT("controlmap"))
        {
            Texture2D->CompressionSettings = TC_VectorDisplacementmap;
            Texture2D->MipGenSettings = TMGS_NoMipmaps;
            Texture2D->Filter = TF_Nearest;
        }
        else
        {
            Texture2D->CompressionSettings = TC_Default;
        }
    }
    Texture->PostEditChange();
    Texture->MarkPackageDirty();
}

bool TextureMatchesImportSettings(UTexture* Texture, const TSharedPtr<FJsonObject>& TextureObject)
{
    if (!Texture)
    {
        return false;
    }
    if (Texture->SRGB != JsonBool(TextureObject, TEXT("srgb"), false))
    {
        return false;
    }
    const FString Compression = JsonString(TextureObject, TEXT("compression"));
    if (UTexture2D* Texture2D = Cast<UTexture2D>(Texture))
    {
        if (Compression == TEXT("normalmap"))
        {
            return Texture2D->CompressionSettings == TC_Normalmap;
        }
        if (Compression == TEXT("normal_rgba"))
        {
            return Texture2D->CompressionSettings == TC_Default &&
                !Texture2D->CompressionNoAlpha &&
                Texture2D->CompressionForceAlpha;
        }
        if (Compression == TEXT("masks"))
        {
            return Texture2D->CompressionSettings == TC_Masks;
        }
        if (Compression == TEXT("indexmap"))
        {
            return Texture2D->CompressionSettings == TC_Grayscale;
        }
        if (Compression == TEXT("controlmap"))
        {
            return Texture2D->CompressionSettings == TC_VectorDisplacementmap;
        }
        return Texture2D->CompressionSettings == TC_Default;
    }
    return true;
}
}

void FWitcherImportContext::ImportTextures()
{
    const TArray<TSharedPtr<FJsonValue>>* Textures = nullptr;
    if (!Manifest->TryGetArrayField(TEXT("textures"), Textures))
    {
        return;
    }
    for (const TSharedPtr<FJsonValue>& Value : *Textures)
    {
        ImportTexture(Value->AsObject());
    }
}

UTexture* FWitcherImportContext::ImportTexture(const TSharedPtr<FJsonObject>& TextureObject)
{
    if (!TextureObject.IsValid())
    {
        return nullptr;
    }
    const FString DepotRel = JsonString(TextureObject, TEXT("depot_path"));
    if (DepotRel.IsEmpty())
    {
        return nullptr;
    }
    if (const TWeakObjectPtr<UTexture>* Cached = TexturesByDepot.Find(DepotRel))
    {
        return Cached->Get();
    }

    const FString AssetRel = ClassSafeAssetRel(DepotRel, UTexture::StaticClass(), TEXT("_tex"));

    if (UTexture* Existing = LoadExistingAsset<UTexture>(ObjectPathFor(AssetRel)))
    {
        if (!ShouldOverwrite(TEXT("textures")) && TextureMatchesImportSettings(Existing, TextureObject))
        {
            TexturesByDepot.Add(DepotRel, Existing);
            return Existing;
        }
    }

    const FString SourceFile = ResolveBundleFile(JsonString(TextureObject, TEXT("file")));
    if (!FPaths::FileExists(SourceFile))
    {
        AddWarning(FString::Printf(TEXT("Texture file missing: %s"), *SourceFile));
        return nullptr;
    }

    UAssetImportTask* Task = NewObject<UAssetImportTask>();
    Task->Filename = SourceFile;
    Task->DestinationPath = PackagePathFor(AssetRel);
    Task->DestinationName = AssetRelName(AssetRel);
    Task->bAutomated = true;
    Task->bSave = false;
    Task->bReplaceExisting = true;
    Task->Factory = NewObject<UTextureFactory>();

    FAssetToolsModule& AssetToolsModule = FModuleManager::LoadModuleChecked<FAssetToolsModule>(TEXT("AssetTools"));
    TArray<UAssetImportTask*> Tasks;
    Tasks.Add(Task);
    AssetToolsModule.Get().ImportAssetTasks(Tasks);

    UTexture* Texture = nullptr;
    if (Task->ImportedObjectPaths.Num() > 0)
    {
        Texture = Cast<UTexture>(StaticLoadObject(UTexture::StaticClass(), nullptr, *Task->ImportedObjectPaths[0]));
    }
    if (!Texture)
    {
        AddWarning(FString::Printf(TEXT("Failed to import texture: %s"), *SourceFile));
        return nullptr;
    }

    ApplyTextureImportSettings(Texture, TextureObject);
    ImportedAssets.Add(Texture->GetPathName());
    TexturesByDepot.Add(DepotRel, Texture);
    return Texture;
}

UTexture* FWitcherImportContext::FindTexture(const FString& DepotRel)
{
    if (DepotRel.IsEmpty())
    {
        return nullptr;
    }
    if (const TWeakObjectPtr<UTexture>* Cached = TexturesByDepot.Find(DepotRel))
    {
        return Cached->Get();
    }
    UTexture* Existing = LoadExistingAsset<UTexture>(ObjectPathFor(DepotRel));
    if (!Existing)
    {
        Existing = LoadExistingAsset<UTexture>(ObjectPathFor(DepotRel + TEXT("_tex")));
    }
    if (Existing)
    {
        TexturesByDepot.Add(DepotRel, Existing);
    }
    return Existing;
}
