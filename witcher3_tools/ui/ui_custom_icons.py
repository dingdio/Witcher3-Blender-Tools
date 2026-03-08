from pathlib import Path
custom_icons = {}

def load_icon(loader, filename, name):
    script_path = Path(__file__).parent
    icon_path = script_path / 'icons' / filename
    loader.load(name, str(icon_path), 'IMAGE')

def register_custom_icon():
    import bpy.utils.previews
    pcoll = bpy.utils.previews.new()
    load_icon(pcoll, 'w_icon.png', "witcher_icon")
    custom_icons["main"] = pcoll

def unregister_custom_icon():
    import bpy.utils.previews
    for pcoll in custom_icons.values():
        bpy.utils.previews.remove(pcoll)
    custom_icons.clear()
    

def register():
    register_custom_icon()
    
def unregister():
    unregister_custom_icon()