from __future__ import annotations

from . import cutscene_bake

_OPTIONAL_WARN_ONLY = {"No cutscene camera actor is assigned."}
_SETUP_WARN_ONLY = _OPTIONAL_WARN_ONLY | {
    "Camera has no cutscene NLA strips.",
}


def _validate_scene_setup(context, warn_only):
    from ..ui import ui_anims

    _lines, errors, warnings = ui_anims._scratch_validation_lines(context)
    warnings = [m for m in errors if m in warn_only] + list(warnings)
    errors = [m for m in errors if m not in warn_only]
    errors = list(dict.fromkeys(errors))
    warnings = [m for m in dict.fromkeys(warnings) if m not in errors]
    lines = [f"ERROR {m}" for m in errors] + [f"WARN {m}" for m in warnings]
    return lines, errors, warnings


def validate_cutscene_setup(context):
    """Validate scene setup before export-owned baking."""
    return _validate_scene_setup(context, _SETUP_WARN_ONLY)


def validate_cutscene(context, allowed_missing_prop_templates=()):
    """Combine setup and bake-contract validation."""
    _lines, errors, warnings = _validate_scene_setup(context, _OPTIONAL_WARN_ONLY)
    errors = list(errors)
    warnings = list(warnings)
    errors.extend(cutscene_bake.validate_cutscene_for_export(
        context,
        allowed_missing_prop_templates=allowed_missing_prop_templates,
    ))
    from ..ui import ui_cutscene
    partial = sorted({str(e.event_type) for e in getattr(context.scene, "witcher_cutscene_event_items", [])
                      if ui_cutscene.partial_event_fields(e.event_type)})
    if partial:
        count = sum(1 for e in context.scene.witcher_cutscene_event_items if ui_cutscene.partial_event_fields(e.event_type))
        warnings.append(f"{count} event(s) export with partial fields ({', '.join(partial[:3])}{', …' if len(partial) > 3 else ''})")
    errors = list(dict.fromkeys(errors))
    warnings = [m for m in dict.fromkeys(warnings) if m not in errors]
    lines = [f"ERROR {m}" for m in errors] + [f"WARN {m}" for m in warnings]
    return lines, errors, warnings
