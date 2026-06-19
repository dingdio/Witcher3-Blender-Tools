#include "WitcherVisibilityPanel.h"
#include "WitcherPlacementTags.h"

#include "Editor.h"
#include "EngineUtils.h"
#include "Engine/World.h"
#include "GameFramework/Actor.h"
#include "Framework/Application/SlateApplication.h"
#include "Framework/Docking/TabManager.h"
#include "Widgets/Docking/SDockTab.h"
#include "Widgets/SBoxPanel.h"
#include "Widgets/SCompoundWidget.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Input/SCheckBox.h"
#include "Widgets/Layout/SBorder.h"
#include "Widgets/Text/STextBlock.h"

#define LOCTEXT_NAMESPACE "WitcherVisibilityPanel"

namespace
{
const FName WitcherVisibilityTabId(TEXT("WitcherLayerVisibility"));

enum class EVisibilityScope
{
    Editor,
    Runtime,
};

UWorld* EditorWorld()
{
    return GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
}

class SWitcherVisibilityPanel : public SCompoundWidget
{
public:
    SLATE_BEGIN_ARGS(SWitcherVisibilityPanel) {}
    SLATE_END_ARGS()

    void Construct(const FArguments& InArgs)
    {
        Categories.Add({
            WitcherPlacementTags::Mesh(),
            LOCTEXT("RegularMeshes", "Mesh"),
            LOCTEXT("RegularMeshesTip", "Mesh placements excluding RED engine-hidden and default-hidden groups"),
            0, 0, 0, true, true, true, true
        });
        Categories.Add({
            WitcherPlacementTags::Collision(),
            LOCTEXT("Collision", "Collision"),
            LOCTEXT("CollisionTip", "Collision-only placement actors"),
            0, 0, 0, false, false, true, false
        });
        Categories.Add({
            WitcherPlacementTags::Light(),
            LOCTEXT("Lights", "Lights"),
            LOCTEXT("LightsTip", "Imported point and spot lights"),
            0, 0, 0, false, false, true, true
        });
        Categories.Add({
            WitcherPlacementTags::EngineHidden(),
            LOCTEXT("EngineHidden", "Engine Hidden Meshes"),
            LOCTEXT("EngineHiddenTip", "Per-placement sector objects whose RED engine visibility flag is off"),
            0, 0, 0, false, false, false, false
        });
        Categories.Add({
            WitcherPlacementTags::DefaultHidden(),
            LOCTEXT("DefaultHidden", "Default Hidden Groups"),
            LOCTEXT("DefaultHiddenTip", "Layers under RED LayerGroups that are hidden on world start"),
            0, 0, 0, false, false, false, false
        });
        RefreshState(/*bSyncSettings=*/true);

        // Imports spawn actors after this panel is built
        RegisterActiveTimer(
            1.0f,
            FWidgetActiveTimerDelegate::CreateLambda(
                [this](double, float) -> EActiveTimerReturnType
                {
                    RefreshState(/*bSyncSettings=*/false);
                    return EActiveTimerReturnType::Continue;
                }));

        ChildSlot
        [
            SNew(SBorder).Padding(8.0f)
            [
                SNew(SVerticalBox)
                + SVerticalBox::Slot().AutoHeight().Padding(0.0f, 0.0f, 0.0f, 6.0f)
                [
                    SNew(STextBlock).Text(LOCTEXT("Header", "Imported Witcher layer visibility"))
                ]
                + SVerticalBox::Slot().AutoHeight().Padding(0.0f, 0.0f, 0.0f, 8.0f)
                [
                    BuildSection(
                        EVisibilityScope::Editor,
                        LOCTEXT("EditorSection", "Editor Viewport"),
                        LOCTEXT("EditorSectionTip", "Temporary editor-only visibility for inspecting imported actors"))
                ]
                + SVerticalBox::Slot().AutoHeight()
                [
                    BuildSection(
                        EVisibilityScope::Runtime,
                        LOCTEXT("RuntimeSection", "Runtime / PIE"),
                        LOCTEXT("RuntimeSectionTip", "Actor Hidden In Game state used by Play-In-Editor and saved maps"))
                ]
            ]
        ];
    }

private:
    struct FCategory
    {
        FName Tag;
        FText Label;
        FText Tooltip;
        int32 Count;
        int32 EditorVisibleCount;
        int32 RuntimeVisibleCount;
        bool bExcludeEngineHidden;
        bool bExcludeDefaultHidden;
        bool bEditorVisible;
        bool bRuntimeVisible;
    };
    TArray<FCategory> Categories;

    TSharedRef<SWidget> BuildSection(EVisibilityScope Scope, FText Title, FText Tooltip)
    {
        TSharedRef<SVerticalBox> Rows = SNew(SVerticalBox);
        for (int32 Index = 0; Index < Categories.Num(); ++Index)
        {
            Rows->AddSlot().AutoHeight().Padding(4.0f, 2.0f)
            [
                SNew(SHorizontalBox)
                + SHorizontalBox::Slot().AutoWidth().VAlign(VAlign_Center)
                [
                    SNew(SCheckBox)
                    .IsChecked_Lambda([this, Index, Scope]() { return IsCategoryChecked(Index, Scope); })
                    .OnCheckStateChanged_Lambda([this, Index, Scope](ECheckBoxState NewState) { OnCategoryToggled(NewState, Index, Scope); })
                    .IsEnabled_Lambda([this, Index]() { return Categories.IsValidIndex(Index) && Categories[Index].Count > 0; })
                ]
                + SHorizontalBox::Slot().FillWidth(1.0f).VAlign(VAlign_Center).Padding(6.0f, 0.0f)
                [
                    SNew(STextBlock)
                    .Text(Categories[Index].Label)
                    .ToolTipText(Categories[Index].Tooltip)
                ]
                + SHorizontalBox::Slot().AutoWidth().VAlign(VAlign_Center)
                [
                    SNew(STextBlock).Text_Lambda([this, Index]() { return GetCategoryCountText(Index); })
                ]
            ];
        }

        return SNew(SBorder).Padding(6.0f)
        [
            SNew(SVerticalBox)
            + SVerticalBox::Slot().AutoHeight().Padding(0.0f, 0.0f, 0.0f, 4.0f)
            [
                SNew(STextBlock)
                .Text(Title)
                .ToolTipText(Tooltip)
            ]
            + SVerticalBox::Slot().AutoHeight()
            [
                Rows
            ]
            + SVerticalBox::Slot().AutoHeight().Padding(0.0f, 8.0f, 0.0f, 0.0f)
            [
                SNew(SHorizontalBox)
                + SHorizontalBox::Slot().AutoWidth().Padding(0.0f, 0.0f, 6.0f, 0.0f)
                [
                    SNew(SButton)
                    .Text(LOCTEXT("ShowAll", "Show All"))
                    .OnClicked_Lambda([this, Scope]() { return OnShowAll(Scope); })
                ]
                + SHorizontalBox::Slot().AutoWidth().Padding(0.0f, 0.0f, 6.0f, 0.0f)
                [
                    SNew(SButton)
                    .Text(LOCTEXT("Defaults", "Defaults"))
                    .OnClicked_Lambda([this, Scope]() { return OnDefaults(Scope); })
                ]
                + SHorizontalBox::Slot().AutoWidth()
                [
                    SNew(SButton)
                    .Text(LOCTEXT("Refresh", "Refresh"))
                    .OnClicked(this, &SWitcherVisibilityPanel::OnRefresh)
                ]
            ]
        ];
    }

    bool MatchesCategory(const AActor* Actor, const FCategory& Cat) const
    {
        if (!Actor || !Actor->Tags.Contains(Cat.Tag))
        {
            return false;
        }
        if (Cat.bExcludeEngineHidden && Actor->Tags.Contains(WitcherPlacementTags::EngineHidden()))
        {
            return false;
        }
        if (Cat.bExcludeDefaultHidden && Actor->Tags.Contains(WitcherPlacementTags::DefaultHidden()))
        {
            return false;
        }
        return true;
    }

    bool IsManagedActor(const AActor* Actor) const
    {
        for (const FCategory& Cat : Categories)
        {
            if (MatchesCategory(Actor, Cat))
            {
                return true;
            }
        }
        return false;
    }

    void RefreshState(bool bSyncSettings)
    {
        for (FCategory& Cat : Categories)
        {
            Cat.Count = 0;
            Cat.EditorVisibleCount = 0;
            Cat.RuntimeVisibleCount = 0;
        }
        if (UWorld* World = EditorWorld())
        {
            for (TActorIterator<AActor> It(World); It; ++It)
            {
                for (FCategory& Cat : Categories)
                {
                    if (MatchesCategory(*It, Cat))
                    {
                        ++Cat.Count;
                        if (!It->IsTemporarilyHiddenInEditor())
                        {
                            ++Cat.EditorVisibleCount;
                        }
                        if (!It->IsHidden())
                        {
                            ++Cat.RuntimeVisibleCount;
                        }
                    }
                }
            }
        }

        if (bSyncSettings)
        {
            for (FCategory& Cat : Categories)
            {
                Cat.bEditorVisible = Cat.Count == 0 || Cat.EditorVisibleCount > 0;
                Cat.bRuntimeVisible = Cat.Count == 0 || Cat.RuntimeVisibleCount > 0;
            }
        }
    }

    ECheckBoxState IsCategoryChecked(int32 Index, EVisibilityScope Scope) const
    {
        if (!Categories.IsValidIndex(Index))
        {
            return ECheckBoxState::Unchecked;
        }
        const FCategory& Cat = Categories[Index];
        const bool bVisible = Scope == EVisibilityScope::Editor ? Cat.bEditorVisible : Cat.bRuntimeVisible;
        return bVisible
            ? ECheckBoxState::Checked
            : ECheckBoxState::Unchecked;
    }

    FText GetCategoryCountText(int32 Index) const
    {
        return FText::AsNumber(Categories.IsValidIndex(Index) ? Categories[Index].Count : 0);
    }

    bool ShouldHideActor(const AActor* Actor, EVisibilityScope Scope) const
    {
        for (const FCategory& Cat : Categories)
        {
            if (MatchesCategory(Actor, Cat))
            {
                const bool bVisible = Scope == EVisibilityScope::Editor ? Cat.bEditorVisible : Cat.bRuntimeVisible;
                if (!bVisible)
                {
                    return true;
                }
            }
        }
        return false;
    }

    void ApplyVisibility(EVisibilityScope Scope)
    {
        if (UWorld* World = EditorWorld())
        {
            for (TActorIterator<AActor> It(World); It; ++It)
            {
                if (!IsManagedActor(*It))
                {
                    continue;
                }
                const bool bHidden = ShouldHideActor(*It, Scope);
                if (Scope == EVisibilityScope::Editor)
                {
                    It->SetIsTemporarilyHiddenInEditor(bHidden);
                }
                else
                {
                    It->SetActorHiddenInGame(bHidden);
                }
            }
        }
        if (GEditor)
        {
            GEditor->RedrawLevelEditingViewports();
        }
    }

    void OnCategoryToggled(ECheckBoxState NewState, int32 Index, EVisibilityScope Scope)
    {
        if (!Categories.IsValidIndex(Index))
        {
            return;
        }
        const bool bVisible = NewState == ECheckBoxState::Checked;
        if (Scope == EVisibilityScope::Editor)
        {
            Categories[Index].bEditorVisible = bVisible;
        }
        else
        {
            Categories[Index].bRuntimeVisible = bVisible;
        }
        ApplyVisibility(Scope);
        RefreshState(/*bSyncSettings=*/false);
    }

    FReply OnShowAll(EVisibilityScope Scope)
    {
        for (FCategory& Cat : Categories)
        {
            if (Scope == EVisibilityScope::Editor)
            {
                Cat.bEditorVisible = true;
            }
            else
            {
                Cat.bRuntimeVisible = true;
            }
        }
        ApplyVisibility(Scope);
        RefreshState(/*bSyncSettings=*/false);
        return FReply::Handled();
    }

    FReply OnDefaults(EVisibilityScope Scope)
    {
        for (FCategory& Cat : Categories)
        {
            const bool bVisibleByDefault =
                Cat.Tag != WitcherPlacementTags::Collision()
                && Cat.Tag != WitcherPlacementTags::EngineHidden()
                && Cat.Tag != WitcherPlacementTags::DefaultHidden();
            if (Scope == EVisibilityScope::Editor)
            {
                Cat.bEditorVisible = bVisibleByDefault;
            }
            else
            {
                Cat.bRuntimeVisible = bVisibleByDefault;
            }
        }
        ApplyVisibility(Scope);
        RefreshState(/*bSyncSettings=*/false);
        return FReply::Handled();
    }

    FReply OnRefresh()
    {
        RefreshState(/*bSyncSettings=*/true);
        return FReply::Handled();
    }
};

TSharedRef<SDockTab> SpawnVisibilityTab(const FSpawnTabArgs&)
{
    return SNew(SDockTab)
        .TabRole(ETabRole::NomadTab)
        [
            SNew(SWitcherVisibilityPanel)
        ];
}
}

namespace WitcherVisibilityPanel
{
void Register()
{
    if (!FSlateApplication::IsInitialized())
    {
        return;
    }
    FGlobalTabmanager::Get()->RegisterNomadTabSpawner(
            WitcherVisibilityTabId,
            FOnSpawnTab::CreateStatic(&SpawnVisibilityTab))
        .SetDisplayName(LOCTEXT("TabTitle", "Witcher Layer Visibility"))
        .SetTooltipText(LOCTEXT("TabTooltip", "Show or hide imported Witcher layer groups (collision, meshes, lights)"))
        .SetMenuType(ETabSpawnerMenuType::Hidden);
}

void Unregister()
{
    if (FSlateApplication::IsInitialized())
    {
        FGlobalTabmanager::Get()->UnregisterNomadTabSpawner(WitcherVisibilityTabId);
    }
}

void OpenTab()
{
    if (FSlateApplication::IsInitialized())
    {
        FGlobalTabmanager::Get()->TryInvokeTab(WitcherVisibilityTabId);
    }
}
}

#undef LOCTEXT_NAMESPACE
