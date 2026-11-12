"""Typed controls layered onto the shared inherited CR2W field layout."""
import json
from functools import lru_cache
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, IntVectorProperty, StringProperty
from ..CR2W.event_enums import EVENT_ENUMS
from .ui_cr2w_fields import WITCH_OT_ImportedFieldInfo


@lru_cache(maxsize=None)
def enum_items(kind, current):
    names = [name for name, _ in EVENT_ENUMS.get(kind, ())]
    if current not in names:
        names.insert(0, current)
    return [(name or '__EMPTY__', name or '(empty)', name) for name in names]


def enum_current(self):
    if self.kind == 'SEnumVariant':
        return next((name for name, value in EVENT_ENUMS.get(self.enum_type, ()) if value == self.int_value), str(self.int_value))
    return self.text_value


def enum_get(self):
    return next((i for i, e in enumerate(enum_items(self.enum_type, enum_current(self))) if e[0] == (enum_current(self) or '__EMPTY__')), 0)


def enum_set(self, value):
    identifier = enum_items(self.enum_type, enum_current(self))[value][0]
    self.text_value = '' if identifier == '__EMPTY__' else identifier
    if self.kind == 'SEnumVariant':
        self.int_value = dict(EVENT_ENUMS.get(self.enum_type, ())).get(self.text_value, self.int_value)


class WITCH_PG_EventFieldEditor(bpy.types.PropertyGroup):
    kind: StringProperty()
    snapshot: StringProperty()
    text_value: StringProperty(name='Value')
    bool_value: BoolProperty(name='Value')
    int_value: IntProperty(name='Value')
    float_value: FloatProperty(name='Value', precision=5)
    vector_value: FloatVectorProperty(name='Value', size=4, precision=5)
    color_value: IntVectorProperty(name='Value', size=4, min=0, max=255)
    components: StringProperty()
    enum_type: StringProperty(name='Enum Type', search=lambda self, context, text: sorted(EVENT_ENUMS))
    enum_choice: EnumProperty(name='Value', items=lambda self, context: enum_items(self.enum_type, enum_current(self)), get=enum_get, set=enum_set)


def typed_value(editor):
    if editor.kind in {'Vector', 'Vector3', 'EulerAngles', 'Color'}:
        values = editor.color_value if editor.kind == 'Color' else editor.vector_value
        return dict(zip(editor.components.split(','), values))
    if editor.kind == 'SEnumVariant':
        return {'enumType': editor.enum_type, 'enumValue': editor.int_value}
    if editor.kind == 'Bool':
        return editor.bool_value
    if editor.kind in {'Float', 'CFloat'}:
        return editor.float_value
    if editor.kind.startswith(('Int', 'Uint')):
        bits = int(editor.kind[4:] if editor.kind.startswith('Uint') else editor.kind[3:])
        lo, hi = (0, 2**bits - 1) if editor.kind.startswith('Uint') else (-2**(bits-1), 2**(bits-1)-1)
        if not lo <= editor.int_value <= hi:
            raise ValueError(f'{editor.kind} value must be between {lo} and {hi}')
        return editor.int_value
    return editor.text_value


def prepare(field, value, editor):
    kind = field.type_text
    if kind not in {'Bool', 'Float', 'CFloat', 'Int8', 'Int16', 'Int32', 'Uint8', 'Uint16', 'Uint32', 'CName', 'String', 'StringAnsi', 'SEnumVariant', 'Vector', 'Vector3', 'EulerAngles', 'Color'} and kind not in EVENT_ENUMS and not kind.startswith(('handle:', 'soft:', 'ptr:')):
        return
    # Keep unsupported/large values editable as text instead of clamping them in a Blender int control.
    if kind.startswith(('Int', 'Uint')) and value is not None and not -2147483648 <= value <= 2147483647:
        return
    editor.kind = kind
    if kind in {'Vector', 'Vector3', 'EulerAngles', 'Color'}:
        defaults = {'Vector': ('X', 'Y', 'Z', 'W'), 'Vector3': ('X', 'Y', 'Z'), 'EulerAngles': ('Pitch', 'Yaw', 'Roll'), 'Color': ('Red', 'Green', 'Blue', 'Alpha')}
        raw = value or dict.fromkeys(defaults[kind], 255 if kind == 'Color' else 0)
        if len(raw) > 4:
            editor.kind = ''
            return
        editor.components = ','.join(raw)
        values = list(raw.values()) + [0] * (4 - len(raw))
        if kind == 'Color':
            editor.color_value = values
        else:
            editor.vector_value = values
    elif kind == 'SEnumVariant':
        raw = value or {}
        editor.enum_type = raw.get('enumType', 'ERotationRate')
        editor.int_value = raw.get('enumValue', 0)
        editor.text_value = next((n for n, v in EVENT_ENUMS.get(editor.enum_type, ()) if v == editor.int_value), str(editor.int_value))
    elif kind == 'Bool':
        editor.bool_value = bool(value)
    elif kind in {'Float', 'CFloat'}:
        editor.float_value = value or 0
    elif kind.startswith(('Int', 'Uint')):
        editor.int_value = value or 0
    else:
        editor.text_value = str(value) if value is not None else ''
        editor.enum_type = kind
    editor.snapshot = json.dumps(typed_value(editor), sort_keys=True)


def sync(field, editor):
    if editor is None or not editor.kind:
        return
    if not field.is_set:
        field.value_text = '<unset>'
        return
    value = typed_value(editor)
    if field.value_text == '<unset>' or json.dumps(value, sort_keys=True) != editor.snapshot:
        # Keep unedited members of structured values, including future engine extensions.
        if isinstance(value, dict) and field.value_text != '<unset>':
            value = dict(json.loads(field.value_text), **value)
        field.value_text = value if isinstance(value, str) else json.dumps(value)


def draw_field(layout, field, editor):
    if editor is None or not editor.kind:
        return False
    row = layout.row(align=True)
    row.prop(field, 'is_set', text='', icon='CHECKBOX_HLT' if field.is_set else 'CHECKBOX_DEHLT')
    value_row = row.row(align=True)
    value_row.enabled = field.is_set
    kind = editor.kind
    if kind in {'SEnumVariant', 'Vector', 'Vector3', 'EulerAngles', 'Color'}:
        value_row.prop(field, 'show_children', text=field.field_name, icon='TRIA_DOWN' if field.show_children else 'TRIA_RIGHT', emboss=False)
    else:
        prop = ('bool_value' if kind == 'Bool' else 'float_value' if kind in {'Float', 'CFloat'} else
                'int_value' if kind.startswith(('Int', 'Uint')) else 'enum_choice' if kind in EVENT_ENUMS else 'text_value')
        value_row.prop(editor, prop, text=field.field_name)
    info = row.operator(WITCH_OT_ImportedFieldInfo.bl_idname, text='', icon='INFO')
    info.field_name, info.type_text, info.value_text = field.field_name, kind, field.value_text
    if kind in {'Vector', 'Vector3', 'EulerAngles', 'Color'} and field.show_children:
        box = layout.box()
        box.enabled = field.is_set
        for i, name in enumerate(editor.components.split(',')):
            box.prop(editor, 'color_value' if kind == 'Color' else 'vector_value', index=i, text=name)
    if kind == 'SEnumVariant' and field.show_children:
        box = layout.box()
        box.enabled = field.is_set
        box.prop(editor, 'enum_type')
        if editor.enum_type in EVENT_ENUMS:
            box.prop(editor, 'enum_choice', text='Named Value')
        box.prop(editor, 'int_value', text='Numeric Value')
    return True


def register():
    bpy.utils.register_class(WITCH_PG_EventFieldEditor)


def unregister():
    bpy.utils.unregister_class(WITCH_PG_EventFieldEditor)
