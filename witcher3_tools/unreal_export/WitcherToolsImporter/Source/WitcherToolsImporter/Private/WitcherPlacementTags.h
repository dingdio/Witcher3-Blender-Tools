#pragma once

#include "CoreMinimal.h"

namespace WitcherPlacementTags
{
    // Mutually exclusive import kinds used for broad visibility controls.
    inline const FName& Mesh()
    {
        static const FName Tag(TEXT("WitcherKind:Mesh"));
        return Tag;
    }

    inline const FName& Collision()
    {
        static const FName Tag(TEXT("WitcherKind:Collision"));
        return Tag;
    }

    inline const FName& Light()
    {
        static const FName Tag(TEXT("WitcherKind:Light"));
        return Tag;
    }

    // Cross-cutting RED visibility groups. EngineHidden is a per-sector-object
    // flag; DefaultHidden is inherited from the source LayerGroup initial state.
    inline const FName& EngineHidden()
    {
        static const FName Tag(TEXT("WitcherGroup:EngineHidden"));
        return Tag;
    }

    inline const FName& DefaultHidden()
    {
        static const FName Tag(TEXT("WitcherGroup:DefaultHidden"));
        return Tag;
    }
}
