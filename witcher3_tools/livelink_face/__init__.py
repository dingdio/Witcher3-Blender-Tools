from . import ui_livelink


def register():
    ui_livelink.register()


def unregister():
    ui_livelink.unregister()


def draw_morph_panel(layout, context):
    ui_livelink.draw_morph_panel(layout, context)
