"""Viewport track display for the selected animation's events."""
import bpy
import math
import tempfile
from pathlib import Path

from . import events

_handler = None
_geometry = {}
_ui_icons = {}
_ICON_PATHS = {
    'PINNED': (((4, 13), (10, 13), (9, 8), (12, 5), (2, 5), (5, 8), (4, 13)), ((7, 5), (7, 0))),
    'SOUND': (((5, 3), (5, 12), (13, 14), (13, 5)), ((5, 10), (13, 12)),
              ((5, 3), (4, 1), (1, 1), (0, 2), (1, 4), (4, 4), (5, 3)),
              ((13, 5), (12, 3), (9, 3), (8, 4), (9, 6), (12, 6), (13, 5))),
    'LINKED': (((5, 6), (3, 6), (1, 8), (1, 10), (5, 14), (7, 14), (9, 12), (9, 10)),
               ((5, 4), (5, 2), (7, 0), (9, 0), (13, 4), (13, 6), (11, 8), (9, 8)), ((5, 10), (9, 4))),
    'NLA': (((1, 11), (13, 11)), ((1, 7), (13, 7)), ((1, 3), (13, 3)),
            ((3, 9), (3, 13), (6, 13), (6, 9), (3, 9)),
            ((8, 5), (8, 9), (11, 9), (11, 5), (8, 5))),
    'EFFECT': (((7, 14), (9, 9), (14, 7), (9, 5), (7, 0), (5, 5), (0, 7), (5, 9), (7, 14)),),
    'SYNC': (((1, 9), (12, 9), (9, 12)), ((12, 9), (9, 6)),
             ((13, 4), (2, 4), (5, 7)), ((2, 4), (5, 1))),
    'SCRIPT': (((4, 11), (1, 7), (4, 3)), ((10, 11), (13, 7), (10, 3)), ((8, 13), (6, 1))),
    'TIME': (((7, 14), (12, 12), (14, 7), (12, 2), (7, 0), (2, 2), (0, 7), (2, 12), (7, 14)),
             ((7, 11), (7, 7), (10, 5))),
    'KEY_HLT': (((7, 13), (13, 7), (7, 1), (1, 7), (7, 13)),),
    'LOCKED': (((2, 1), (12, 1), (12, 8), (2, 8), (2, 1)),
               ((4, 8), (4, 11), (5, 13), (9, 13), (10, 11), (10, 8)), ((7, 4), (7, 6))),
}


def icon_geometry(kind):
    if kind not in _geometry:
        points = []
        for path in _ICON_PATHS[kind]:
            for a, b in zip(path, path[1:]):
                dx, dy = b[0] - a[0], b[1] - a[1]
                width = 0.65 / math.hypot(dx, dy)
                nx, ny = -dy * width, dx * width
                quad = ((a[0] + nx, a[1] + ny), (a[0] - nx, a[1] - ny),
                        (b[0] - nx, b[1] - ny), (b[0] + nx, b[1] + ny))
                points.extend(quad[i] for i in (0, 1, 2, 0, 2, 3))
        # Blender's custom UI icons use byte coordinates; the overlay uses these same vertices.
        coords = bytes(max(0, min(255, round((v + 1) * 255 / 16))) for point in points for v in point)
        vertices = tuple((coords[i] * 16 / 255 - 1, coords[i + 1] * 16 / 255 - 1) for i in range(0, len(coords), 2))
        _geometry[kind] = coords, vertices
    return _geometry[kind]


def icon_value(kind):
    if kind not in _ui_icons:
        coords, vertices = icon_geometry(kind)
        # Blender doubles geometry icons for toolbars; fit a normal label's 16px cell.
        coords = bytes(round(v / 2 + (64 if i % 2 else 0)) for i, v in enumerate(coords))
        # Blender automatically inverts geometry icons for light themes.
        # The file API avoids Blender 4.5's new_triangles/free crash (#156532).
        with tempfile.TemporaryDirectory(prefix='w3_event_icon_') as folder:
            path = Path(folder) / 'icon.dat'
            path.write_bytes(b'VCO\0\0\xff\0\0' + coords + bytes((220, 220, 220, 255)) * len(vertices))
            _ui_icons[kind] = bpy.app.icons.new_triangles_from_file(str(path))
    return _ui_icons[kind]


def event_icon(event):
    kind = event.event_type
    if event.sound_event or any(word in kind for word in ('Sound', 'Footstep', 'Music')):
        return 'SOUND'
    if 'Effect' in kind:
        return 'EFFECT'
    if 'Sync' in kind:
        return 'SYNC'
    if events.event_inherits_from(event.source_game, kind, 'CExtAnimScriptEvent') or events.event_inherits_from(
            event.source_game, kind, 'CExtAnimScriptDurationEvent'):
        return 'SCRIPT'
    return 'TIME' if 'Duration' in kind or events.event_inherits_from(event.source_game, kind, 'CExtAnimDurationEvent') else 'KEY_HLT'


def track_icon(label, items):
    kinds = {event_icon(event) for _, event in items}
    if len(kinds) == 1:
        return next(iter(kinds))
    return 'PINNED' if not items and label == events.DEFAULT_TRACK else 'NLA'


def event_active(event, current, rate):
    duration = event.duration if event.duration > 0 else max(1.0, rate * 0.1)
    return event.frame <= current < event.frame + duration


def tag_redraw(_self=None, context=None):
    for window in (context or bpy.context).window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def view_range(action, scene):
    start, end = action.frame_range
    span = max(1, end - start) / scene.witcher_tracks_zoom
    start += scene.witcher_tracks_pan
    return start, start + span


def visible_rows(rows, selected, limit, track=''):
    limit = max(2, limit)
    selected_row = next((i for i, (_, items) in enumerate(rows) if any(n == selected for n, _ in items)), 0)
    if track:
        selected_row = next((i for i, (name, _) in enumerate(rows) if (name[1] if isinstance(name, tuple) else name) == track), selected_row)
    offset = max(1, selected_row - limit + 2)
    return rows[:1] + rows[offset:offset + limit - 1]


def draw():
    context = bpy.context
    if not context.scene.witcher_event_tracks or context.region is None:
        return
    from ..ui.ui_anim_events import context_clip
    _, action, strip = context_clip(context)
    if action is None:
        return
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader

    scale = context.preferences.system.ui_scale
    row_height = 20 * scale
    region = context.region
    sides = {r.type: r.width for r in context.area.regions}
    left = sides.get('TOOLS', 0) + 12 * scale
    right = region.width - sides.get('UI', 0) - 12 * scale
    if not context.scene.witcher_tracks_fit:
        right = min(right, left + context.scene.witcher_tracks_width * scale)
    if right - left < 260 * scale:
        return
    label_width = min(180 * scale, (right - left) * 0.3)
    start, end = view_range(action, context.scene)
    current = events.action_frame(strip, context.scene.frame_current_final)
    rows = events.track_rows(action, context.scene.witcher_tracks_group_unnamed, with_keys=True,
                             show_empty=context.scene.witcher_tracks_show_empty)
    locked = events.locked_tracks(action)
    selected = action.witcher_event_index
    top = region.height - 110 * scale
    limit = max(1, int(region.height * 0.4 / row_height))
    focused_track = context.scene.witcher_event_track_filter
    if focused_track and any(key[1] == focused_track and not items for key, items in rows):
        selected = -1
    visible = visible_rows(rows, selected, limit, focused_track)
    height = (len(visible) + 2) * row_height
    margin = context.scene.witcher_tracks_margin * scale
    top = (top - margin if context.scene.witcher_tracks_position == 'TOP'
           else 40 * scale + height + margin)
    top = max(height + 10 * scale, min(top, region.height - 50 * scale))
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    selected_colour = (0.55, 0.9, 1, 1)
    outlines = []
    blf.size(0, 11 * scale)

    def x(frame):
        return left + label_width + (right - left - label_width) * max(0, min(1, (frame - start) / (end - start)))

    def rect(a, b, c, d, colour):
        shader.bind()
        shader.uniform_float('color', colour)
        batch_for_shader(shader, 'TRIS', {'pos': [(a, b), (c, b), (c, d), (a, d)]},
                         indices=((0, 1, 2), (0, 2, 3))).draw(shader)

    def text(a, b, value, width, colour=(1, 1, 1, 1)):
        value = str(value)
        if blf.dimensions(0, value)[0] > width:
            while value and blf.dimensions(0, value + '…')[0] > width:
                value = value[:-1]
            value = value + '…' if value else ''
        blf.position(0, a, b, 0)
        blf.color(0, *colour)
        blf.draw(0, value)

    def icon(a, b, kind, colour):
        vertices = [(a + x * scale, b + y * scale) for x, y in icon_geometry(kind)[1]]
        shader.bind()
        shader.uniform_float('color', colour)
        batch_for_shader(shader, 'TRIS', {'pos': vertices}).draw(shader)

    gpu.state.blend_set('ALPHA')
    try:
        bottom = top - (len(visible) + 2) * row_height
        rect(left - 6 * scale, bottom - 4 * scale, right + 6 * scale, top + row_height, (0.025, 0.025, 0.03, 0.88))
        text(left, top + 3 * scale, f'{action.name}   |   Frame {current:.1f}   |   View {start:.1f} - {end:.1f}', right - left)
        for index, ((source, label), items) in enumerate(visible):
            y = top - (index + 1) * row_height
            label_colour = selected_colour if any(n == selected for n, _ in items) or (not items and label == focused_track) else (1, 1, 1, 1)
            icon(left, y + 2 * scale, track_icon(label, items), label_colour)
            inset = 20 * scale
            if source or (label == events.DEFAULT_TRACK and items):
                icon(left + inset, y + 2 * scale, 'LINKED' if source else 'PINNED', label_colour)
                inset += 18 * scale
            if (source, label) in locked or (items and all((e.source_file, e.track_name or events.DEFAULT_TRACK) in locked for _, e in items)):
                icon(left + inset, y + 2 * scale, 'LOCKED', label_colour)
                inset += 18 * scale
            text(left + inset, y + 4 * scale, label, label_width - inset - 10 * scale, label_colour)
            if not items:
                rect(left + label_width, y, right, y + row_height - 3 * scale, (0.12, 0.14, 0.16, 0.45))
            bars = []
            for number, event in items:
                if event.frame > end or event.frame + max(event.duration, 0) < start:
                    continue
                a = min(x(event.frame), right - 3 * scale)
                b = min(right, max(x(event.frame + event.duration), a + 3 * scale))
                badge = max(left + label_width, min(a if event.duration else a + 4 * scale, right - 16 * scale))
                bars.append((number, event, a, b, badge))
                colour = (0.2, 0.45, 0.65, 0.95) if event.sound_event else (0.3, 0.55, 0.32, 0.95)
                if event_active(event, current, events.fps(context.scene)):
                    colour = (0.75, 0.5, 0.15, 1)
                rect(a, y, b, y + row_height - 3 * scale, colour)
                if number == selected:
                    outlines.append((a, y, b, y + row_height - 3 * scale))
            # Draw every event icon above the bars, even when its label cannot fit.
            for number, event, a, b, badge in sorted(bars, key=lambda item: item[0] == selected):
                active = event_active(event, current, events.fps(context.scene))
                rect(badge, y, badge + 16 * scale, y + row_height - 3 * scale,
                     (0.65, 0.32, 0.03, 1) if active else (0.06, 0.09, 0.12, 1))
                icon(badge + scale, y + scale, event_icon(event), (1, 0.9, 0.45, 1) if active else selected_colour if number == selected else (1, 1, 1, 1))
                if not event.duration:
                    marker = min(x(event.frame), right - 2 * scale)
                    rect(marker, y, marker + 2 * scale, y + row_height - scale,
                         (1, 0.8, 0.2, 1) if active else (0.4, 0.75, 1, 1))
                if number == selected:
                    outlines.append((badge, y, badge + 16 * scale, y + row_height - 3 * scale))
            labels = [(badge, badge + 16 * scale) for _, _, _, _, badge in bars]
            for number, event, a, b, badge in sorted(bars, key=lambda item: (item[0] != selected, item[2])):
                name = event.event_name or event.sound_event or event.event_type
                width = blf.dimensions(0, name)[0]
                lo, hi = badge + 19 * scale, b - 3 * scale
                inside_lo, inside_hi = lo, hi
                outside = width > hi - lo
                if outside:
                    blocked = [(c, d) for n, _, c, d, _ in bars if n != number] + labels
                    edge = max(b, badge + 16 * scale)
                    start_edge = min(a, badge)
                    right_end = min([right] + [c for c, d in blocked if d > edge]) - 4 * scale
                    left_start = max([left + label_width] + [d for c, d in blocked if c < start_edge]) + 4 * scale
                    if right_end - edge >= start_edge - left_start:
                        lo, hi = edge + 4 * scale, right_end
                    else:
                        lo, hi = max(left_start, start_edge - 4 * scale - width), start_edge - 4 * scale
                    if hi - lo < inside_hi - inside_lo:
                        lo, hi, outside = inside_lo, inside_hi, False
                if hi - lo < min(width, 24 * scale):
                    continue
                hi = min(hi, lo + width)
                if any(lo < d and hi > c for c, d in labels):
                    continue
                labels.append((lo - 4 * scale, hi + 4 * scale))
                if number == selected and outside:
                    rect(lo - 2 * scale, y, hi + 2 * scale, y + row_height - 3 * scale, (0.08, 0.13, 0.16, 1))
                    outlines.append((lo - 2 * scale, y, hi + 2 * scale, y + row_height - 3 * scale))
                text(lo, y + 4 * scale, name, hi - lo, selected_colour if number == selected else (1, 1, 1, 1))
        if 0 <= selected < len(action.witcher_events):
            event = action.witcher_events[selected]
            text(left, bottom + 5 * scale, f'Selected: {event.event_name or event.sound_event or event.event_type}',
                 right - left, selected_colour)
        elif focused_track:
            text(left, bottom + 5 * scale, f'Track: {focused_track}', right - left, selected_colour)
        if start <= current <= end:
            rect(x(current), bottom + row_height, min(right, x(current) + 2 * scale), top, (1, 0.6, 0.1, 1))
        for a, b, c, d in outlines:
            rect(a - scale, b - scale, c + scale, b, (1, 1, 1, 1))
            rect(a - scale, d, c + scale, d + scale, (1, 1, 1, 1))
            rect(a - scale, b, a, d, (1, 1, 1, 1))
            rect(c, b, c + scale, d, (1, 1, 1, 1))
    finally:
        gpu.state.blend_set('NONE')


def register():
    global _handler
    if _handler is None:
        _handler = bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _handler
    if _handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handler, 'WINDOW')
        _handler = None
    for value in _ui_icons.values():
        bpy.app.icons.release(value)
    _ui_icons.clear()
    _geometry.clear()
