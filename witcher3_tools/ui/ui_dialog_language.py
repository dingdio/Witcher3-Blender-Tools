import logging
import sys

import bpy

from .. import dialog_language

log = logging.getLogger(__name__)

_LANGUAGE_REFRESHING = False


def _clear_language_cache_status(*cache_names):
    root_package = (__package__ or "witcher3_tools.ui").rsplit(".", 1)[0]
    root_module = sys.modules.get(root_package)
    status = getattr(root_module, "CACHE_STATUS", None)
    if isinstance(status, dict):
        for cache_name in cache_names:
            status.pop(cache_name, None)


def _tag_dialog_language_redraw(context):
    screen = getattr(context, "screen", None) if context is not None else None
    if screen is None:
        try:
            screen = bpy.context.screen
        except Exception:
            screen = None
    for area in getattr(screen, "areas", []) or []:
        try:
            area.tag_redraw()
        except Exception:
            pass


def refresh_dialog_language_consumers(context, refresh_audio=False):
    scene = getattr(context, "scene", None) if context is not None else None
    text_language = dialog_language.get_active_text_language(context)
    voice_language = dialog_language.get_active_voice_language(context)
    dialog_language.set_active_dialog_languages(
        text_language=text_language,
        voice_language=voice_language,
        reset_string_manager=False,
    )

    ui_package = __package__ or "witcher3_tools.ui"
    root_package = ui_package.rsplit(".", 1)[0]
    refreshers = (
        (f"{ui_package}.ui_cutscene", "refresh_cutscene_dialog_language"),
        (f"{ui_package}.ui_scene", "refresh_w2scene_dialog_language"),
        (f"{ui_package}.ui_voice", "refresh_voice_dialog_language"),
        (f"{root_package}.strings_browser.ui_strings_browser", "refresh_strings_browser_dialog_language"),
    )
    for module_path, func_name in refreshers:
        try:
            module = __import__(module_path, fromlist=[func_name])
            refresh_func = getattr(module, func_name, None)
            if callable(refresh_func):
                refresh_func(context, refresh_audio=refresh_audio)
        except Exception:
            log.warning("Dialog language refresh failed in %s.%s", module_path, func_name, exc_info=True)

    _tag_dialog_language_redraw(context)
    return scene


def _sync_voice_to_text_when_available(context, scene, text_language):
    if scene is None or not hasattr(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP):
        return False

    current_voice = dialog_language.get_active_voice_language(context)
    voice_languages = set(dialog_language.supported_voice_languages())
    if voice_languages and current_voice not in voice_languages:
        fallback = "en" if "en" in voice_languages else next(iter(voice_languages))
        if fallback != current_voice:
            setattr(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP, fallback)
            return False

    return False


def _on_text_language_update(self, context):
    global _LANGUAGE_REFRESHING
    if _LANGUAGE_REFRESHING:
        return
    _LANGUAGE_REFRESHING = True
    try:
        scene = getattr(context, "scene", None) if context is not None else None
        text_language = dialog_language.get_active_text_language(context)
        _sync_voice_to_text_when_available(context, scene, text_language)
        voice_language = dialog_language.get_active_voice_language(context)
        dialog_language.set_active_dialog_languages(
            text_language=text_language,
            voice_language=voice_language,
            reset_string_manager=True,
        )
        _clear_language_cache_status("string_cache.pkl")
        refresh_dialog_language_consumers(context, refresh_audio=False)
    finally:
        _LANGUAGE_REFRESHING = False


def _on_voice_language_update(self, context):
    global _LANGUAGE_REFRESHING
    if _LANGUAGE_REFRESHING:
        return
    _LANGUAGE_REFRESHING = True
    try:
        dialog_language.set_active_dialog_languages(
            text_language=dialog_language.get_active_text_language(context),
            voice_language=dialog_language.get_active_voice_language(context),
            reset_string_manager=False,
        )
        _clear_language_cache_status("speech_cache.pkl")
        refresh_dialog_language_consumers(context, refresh_audio=True)
    finally:
        _LANGUAGE_REFRESHING = False


def draw_dialog_language_selector(
    layout,
    context,
    text="Language",
    *,
    heading="",
    icon='WORLD',
    use_property_split=True,
):
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None:
        return False

    props = []
    if hasattr(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP):
        props.append((dialog_language.DIALOG_TEXT_LANGUAGE_PROP, "Text"))
    if hasattr(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP):
        props.append((dialog_language.DIALOG_VOICE_LANGUAGE_PROP, "Voice"))
    if not props and hasattr(scene, dialog_language.DIALOG_LEGACY_LANGUAGE_PROP):
        props.append((dialog_language.DIALOG_LEGACY_LANGUAGE_PROP, text))

    if not props:
        return False

    if heading:
        layout.label(text=heading, icon=icon)

    col = layout.column(align=True)
    col.use_property_split = use_property_split
    col.use_property_decorate = False
    for prop_name, label in props:
        col.prop(scene, prop_name, text=label)
    return True


def _migrate_legacy_language():
    try:
        scene = bpy.context.scene
    except Exception:
        scene = None
    if scene is None:
        return

    legacy = ""
    try:
        legacy = getattr(scene, dialog_language.DIALOG_LEGACY_LANGUAGE_PROP)
    except Exception:
        try:
            legacy = scene.get(dialog_language.DIALOG_LEGACY_LANGUAGE_PROP, "")
        except Exception:
            legacy = ""

    def _has_saved_prop(prop_name):
        try:
            return prop_name in scene.keys()
        except Exception:
            return False

    if legacy and hasattr(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP):
        try:
            if not _has_saved_prop(dialog_language.DIALOG_TEXT_LANGUAGE_PROP):
                setattr(scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP, dialog_language.normalize_dialog_language(legacy))
        except Exception:
            pass
    if legacy and hasattr(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP):
        try:
            voice = dialog_language.normalize_dialog_language(legacy)
            if voice not in set(dialog_language.supported_voice_languages()):
                voice = "en"
            if not _has_saved_prop(dialog_language.DIALOG_VOICE_LANGUAGE_PROP):
                setattr(scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP, voice)
        except Exception:
            pass


def register():
    if not hasattr(bpy.types.Scene, dialog_language.DIALOG_TEXT_LANGUAGE_PROP):
        setattr(
            bpy.types.Scene,
            dialog_language.DIALOG_TEXT_LANGUAGE_PROP,
            bpy.props.EnumProperty(
                name="Text/Subtitles",
                description="Language used for dialog text and viewport subtitles",
                items=dialog_language.dialog_text_language_enum_items(),
                default="en",
                update=_on_text_language_update,
            ),
        )

    if not hasattr(bpy.types.Scene, dialog_language.DIALOG_VOICE_LANGUAGE_PROP):
        setattr(
            bpy.types.Scene,
            dialog_language.DIALOG_VOICE_LANGUAGE_PROP,
            bpy.props.EnumProperty(
                name="Voice/Lipsync",
                description="Language used for speech audio and lipsync imports",
                items=dialog_language.dialog_voice_language_enum_items(),
                default="en",
                update=_on_voice_language_update,
            ),
        )

    _migrate_legacy_language()
    if hasattr(bpy.types.Scene, dialog_language.DIALOG_LEGACY_LANGUAGE_PROP):
        delattr(bpy.types.Scene, dialog_language.DIALOG_LEGACY_LANGUAGE_PROP)
    dialog_language.set_active_dialog_languages(
        text_language=dialog_language.get_active_text_language(),
        voice_language=dialog_language.get_active_voice_language(),
        reset_string_manager=False,
    )


def unregister():
    for prop_name in (
        dialog_language.DIALOG_TEXT_LANGUAGE_PROP,
        dialog_language.DIALOG_VOICE_LANGUAGE_PROP,
        dialog_language.DIALOG_LEGACY_LANGUAGE_PROP,
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
