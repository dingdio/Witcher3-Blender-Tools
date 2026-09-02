from __future__ import annotations

from . import cutscene_bake

_OPTIONAL_WARN_ONLY = {"No cutscene camera actor is assigned."}
_SETUP_WARN_ONLY = _OPTIONAL_WARN_ONLY | {
    "Camera has no cutscene NLA strips.",
}


def issue(severity, message, tab="", object_name="", frame=-1, line=-1):
    return {
        "severity": severity,
        "message": message,
        "tab": tab,
        "object_name": object_name,
        "frame": int(frame),
        "line": int(line),
    }


def _dedupe(issues):
    by_message = {}
    for item in issues:
        previous = by_message.get(item["message"])
        if previous is None or (previous["severity"] != "ERROR" and item["severity"] == "ERROR"):
            by_message[item["message"]] = item
    return sorted(by_message.values(), key=lambda item: item["severity"] != "ERROR")


def issues_to_lines(issues):
    errors = [item["message"] for item in issues if item["severity"] == "ERROR"]
    warnings = [item["message"] for item in issues if item["severity"] != "ERROR"]
    lines = [f"ERROR {m}" for m in errors] + [f"WARN {m}" for m in warnings]
    return lines, errors, warnings


def store_report(scene, issues):
    lines, errors, warnings = issues_to_lines(issues)
    scene.witcher_cutscene_validation_report = "\n".join(lines or ["OK export-ready"])
    rows = getattr(scene, "witcher_cutscene_validation_issues", None)
    if rows is not None:
        rows.clear()
        for item in issues:
            row = rows.add()
            row.severity = item["severity"]
            row.message = item["message"]
            row.tab = item["tab"]
            row.object_name = item["object_name"]
            row.frame = item["frame"]
            row.line = item["line"]
        scene.witcher_cutscene_validation_issue_index = 0
    return lines, errors, warnings


def collect_setup_issues(context, warn_only=_SETUP_WARN_ONLY):
    from ..ui import ui_anims

    issues = []
    for severity, message, tab, object_name in ui_anims._scratch_validation_issues(context):
        if severity == "ERROR" and message in warn_only:
            severity = "WARN"
        issues.append(issue(severity, message, tab, object_name))
    return _dedupe(issues)


def validate_cutscene_setup(context):
    """Validate scene setup before export-owned baking."""
    return issues_to_lines(collect_setup_issues(context))


def _shot_issues(scene):
    issues = []
    ranges = cutscene_bake.shot_ranges(scene)
    for idx, cam, start, end in ranges:
        if end <= start:
            issues.append(issue(
                "WARN",
                f"Shot '{cam.name}' at frame {start} is skipped: a shot needs at least 2 frames before the next shot or the scene end.",
                "CAMERA", cam.name, frame=start,
            ))
    if ranges and cutscene_bake.shots_stale(scene):
        issues.append(issue(
            "WARN",
            "Shots are not on the camera rig yet; run Shots → Rig (Camera tab) or export with Bake on.",
            "CAMERA",
        ))
    return issues


def _validate_authored_cutscene_dialogue(context):
    scene = context.scene
    dialogue = list(getattr(scene, "witcher_cutscene_dialog_lines", []) or [])
    if not dialogue:
        return []

    try:
        from ..lipsync import redkit_project

        has_redkit_project = bool(redkit_project.get_active_project_path(context))
    except Exception:
        has_redkit_project = False
    try:
        has_id_space = int(getattr(scene, "witcher_cutscene_dialog_id_space", -1)) >= 0
    except (TypeError, ValueError):
        has_id_space = False

    frame_start, frame_end = cutscene_bake.effective_frame_range(scene)
    issues = []
    speaker_ranges = {}
    for index, line in enumerate(dialogue, 1):
        label = f"Dialogue line {index}"

        def add(severity, message, index=index):
            issues.append(issue(severity, message, "DIALOGS", line=index - 1))

        speaker = str(getattr(line, "speaker", "") or "").strip()
        if not speaker:
            add("ERROR", f"{label} has no speaker.")
        if not str(getattr(line, "text", "") or "").strip():
            add("ERROR", f"{label} has no text.")

        start = int(getattr(line, "start_frame", 0) or 0)
        end = int(getattr(line, "end_frame", 0) or 0)
        if end <= start:
            add("ERROR", f"{label} must end after it starts.")
        if start < frame_start or end > frame_end:
            add("WARN", f"{label} frames {start}-{end} are outside the cutscene range {frame_start}-{frame_end}.")

        speaker_key = speaker.casefold()
        for other_index, other_start, other_end in speaker_ranges.get(speaker_key, []):
            if start < other_end and other_start < end:
                add("WARN", f"Dialogue lines {other_index} and {index} overlap for speaker '{speaker}'.")
        if speaker_key:
            speaker_ranges.setdefault(speaker_key, []).append((index, start, end))

        tier = str(getattr(line, "tier", "SUBTITLE") or "SUBTITLE")
        if tier == "GAME":
            game_line_id = str(getattr(line, "game_line_id", "") or "").strip()
            if not game_line_id.isdigit() or not 0 < int(game_line_id) <= 0xFFFFFFFF:
                add("ERROR", f"{label} has no valid numeric Game Line ID.")
        allocated_id = str(getattr(line, "allocated_line_id", "") or "").strip()
        if tier != "GAME" and allocated_id:
            from ..exporters import export_cutscene

            state, text = export_cutscene.authored_dialog_line_id_status(context, index - 1)
            if state == 'ERROR':
                add("ERROR", f"{label} ID {allocated_id} {text}.")
        elif tier != "GAME" and not (has_redkit_project or has_id_space):
            add("WARN", f"{label} has no allocated ID and no REDkit project or dialogue ID space is configured.")
    return issues


def collect_issues(context, allowed_missing_prop_templates=()):
    scene = context.scene
    issues = collect_setup_issues(context, _OPTIONAL_WARN_ONLY)
    details = []
    cutscene_bake.validate_cutscene_for_export(
        context,
        allowed_missing_prop_templates=allowed_missing_prop_templates,
        details=details,
    )
    issues += [issue("ERROR", message, tab, object_name) for message, object_name, tab in details]
    issues += _shot_issues(scene)
    fps = float(scene.render.fps) / float(scene.render.fps_base or 1.0)
    if abs(fps - 30.0) > 1e-6:
        issues.append(issue(
            "WARN", f"Scene runs at {fps:g} fps; game cutscenes run at 30 fps and export samples at the scene rate", "TEMPLATE",
        ))
    start, _end = cutscene_bake.effective_frame_range(scene)
    if start < int(scene.frame_start):
        issues.append(issue(
            "WARN",
            f"Scene range starts at {int(scene.frame_start)} but the earliest clip starts at {start}; "
            f"Bake samples from {start}, use the range button next to Bake to match",
            "TEMPLATE",
        ))
    issues += [
        issue("WARN", f"{label}: track '{track_name}' is unmuted, Bake folds it in too", "ANIMS", object_name)
        for label, track_name, object_name in cutscene_bake.bake_inputs(scene)["foreign"]
    ]
    issues += _validate_authored_cutscene_dialogue(context)
    from ..ui import ui_cutscene
    events = list(getattr(scene, "witcher_cutscene_event_items", []) or [])
    partial = sorted({str(e.event_type) for e in events if ui_cutscene.partial_event_fields(e.event_type)})
    if partial:
        count = sum(1 for e in events if ui_cutscene.partial_event_fields(e.event_type))
        issues.append(issue(
            "WARN",
            f"{count} event(s) export with partial fields ({', '.join(partial[:3])}{', …' if len(partial) > 3 else ''})",
            "EVENTS",
        ))
    return _dedupe(issues)


def validate_cutscene(context, allowed_missing_prop_templates=()):
    return issues_to_lines(collect_issues(context, allowed_missing_prop_templates))
