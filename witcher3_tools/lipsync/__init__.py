from . import ui_lipsync


def register():
    ui_lipsync.register()


def unregister():
    ui_lipsync.unregister()


def draw_panel(layout, context):
    ui_lipsync.draw_panel(layout, context)
