"""Blender registration lifecycle for the material-node subsystem."""


def register():
    from . import custom, operators, properties, ui

    properties.register()
    operators.register()
    ui.register()
    custom.register()


def unregister():
    from . import custom, operators, properties, ui

    custom.unregister()
    ui.unregister()
    operators.unregister()
    properties.unregister()
