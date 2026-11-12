"""Action-owned animation events and NLA time mapping."""
import json
import math
from contextlib import contextmanager

from ..CR2W.cutscene_event_schema import event_declared_field_names, event_inherits_from, get_event_class

FIELDS = ('event_type', 'event_name', 'track_name', 'frame', 'duration', 'sound_event', 'source_game', 'source_file', 'payload')
DEFAULT_TRACK = '__Default'
# Vanilla events can start a fraction of a frame before the clip; they still fire at its start.
START_SLACK = 0.5
_track_sync_depth = 0


@contextmanager
def defer_track_sync(action):
    global _track_sync_depth
    _track_sync_depth += 1
    try:
        yield
    finally:
        _track_sync_depth -= 1
        if not _track_sync_depth:
            sync_tracks(action)


def sync_tracks(action):
    if _track_sync_depth or not action.is_editable:
        return
    keys = [key for key, _ in track_rows(action, with_keys=True)]
    known = {(t.source_file, t.name) for t in action.witcher_event_tracks}
    for source, name in keys:
        if (source, name) not in known:
            track = action.witcher_event_tracks.add()
            track.name, track.source_file = name, source
            known.add((source, name))
    if not action.witcher_track_layout_initialized:
        for index, key in enumerate(keys):
            current = next(i for i, t in enumerate(action.witcher_event_tracks) if (t.source_file, t.name) == key)
            action.witcher_event_tracks.move(current, index)
        action.witcher_track_layout_initialized = True


def fps(scene):
    return scene.render.fps / (scene.render.fps_base or 1.0)


def values(event):
    return {name: getattr(event, name) for name in FIELDS}


def store(action, records):
    with defer_track_sync(action):
        action.witcher_events.clear()
        for record in records:
            event = action.witcher_events.add()
            for name in FIELDS:
                if name in record:
                    setattr(event, name, record[name])
    action.witcher_events_known = True
    action.witcher_event_index = 0
    action['witcher_event_baseline'] = ''


def import_events(action, entries, rate, offset=0.0, end=None, auto_tracks='NONE', keep_outside=False):
    records = []
    for entry in entries or []:
        if isinstance(entry, dict):
            from ..CR2W.cutscene_event_schema import build_event_data
            data = entry
            event_type = data.get('event_type', data.get('type_name', ''))
            if not event_type or 'raw_fields' not in data:
                raise ValueError('Unsupported JSON event data; import the native animation set to retain its fields')
            entry = build_event_data(data.get('source_game', 'W3'), event_type, data['raw_fields'])
            entry.field_types = data.get('field_types', {})
            entry.source_file = data.get('source_file', '')
        first = float(entry.start_time) * rate - offset
        duration = float(entry.duration) * rate
        if end is not None and not keep_outside and (first > end + 1e-4 or first + duration < -1e-4):
            continue
        raw = dict(getattr(entry, 'raw_fields', {}) or {})
        records.append(dict(event_type=entry.type_name, event_name=entry.event_name,
                            track_name=entry.track_name, frame=first, duration=duration,
                            sound_event=raw.get('soundEventName', ''), source_game=getattr(entry, 'source_game', 'W3'),
                            source_file=getattr(entry, 'source_file', ''),
                            payload=json.dumps({'raw_fields': raw, 'field_types': getattr(entry, 'field_types', {})})))
    action.witcher_event_tracks.clear()
    action.witcher_track_layout_initialized = False
    store(action, records)
    action['witcher_event_baseline'] = json.dumps([values(e) for e in action.witcher_events], sort_keys=True)
    action['witcher_event_range'] = [0.0, float(end if end is not None else action.frame_range[1])]
    if auto_tracks != 'NONE':
        create_tracks(action, auto_tracks)


def track_assignments(action, group_by='NAME', scope='DEFAULT'):
    """Plan regrouping without moving events into or out of protected tracks."""
    if group_by not in {'NAME', 'CLASS'}:
        raise ValueError(f'Unknown track grouping: {group_by}')
    if scope not in {'DEFAULT', 'UNLOCKED'}:
        raise ValueError(f'Unknown track scope: {scope}')
    locked = locked_tracks(action)
    assignments, protected = [], 0
    for index, event in enumerate(action.witcher_events):
        current = event.track_name or DEFAULT_TRACK
        if scope == 'DEFAULT' and current != DEFAULT_TRACK:
            continue
        class_name = event.event_type.removeprefix('CExtAnim') or 'Event'
        name = (event.event_name.strip() or event.sound_event.strip()) if group_by == 'NAME' else ''
        if not name or name == DEFAULT_TRACK:
            name = class_name
        if (event.source_file, current) in locked or (event.source_file, name) in locked:
            protected += 1
        elif event.track_name != name:
            assignments.append((index, name))
    return assignments, protected


def create_tracks(action, group_by='NAME', scope='DEFAULT'):
    sync_tracks(action)
    assignments, _ = track_assignments(action, group_by, scope)
    with defer_track_sync(action):
        for index, name in assignments:
            action.witcher_events[index].track_name = name
    return len(assignments)


def locked_tracks(action):
    return {(t.source_file, t.name) for t in action.witcher_event_tracks if t.locked}


def set_track_locked(action, name, source='', locked=True):
    sync_tracks(action)
    if (source, name) not in track_keys(action):
        raise ValueError('The selected track no longer exists')
    for track in action.witcher_event_tracks:
        if (track.source_file, track.name) == (source, name):
            track.locked = locked
            return


def track_keys(action):
    return {('', DEFAULT_TRACK)} | {(t.source_file, t.name) for t in action.witcher_event_tracks} | {
        (e.source_file, e.track_name or DEFAULT_TRACK) for e in action.witcher_events}


def add_track(action, name):
    sync_tracks(action)
    name = name.strip()
    if not name:
        raise ValueError('Enter a track name')
    if name == DEFAULT_TRACK:
        raise ValueError('__Default already exists and is permanent')
    if any(existing == name for _, existing in track_keys(action)):
        raise ValueError('This clip already has a track with that name')
    action.witcher_event_tracks.add().name = name
    return name


def delete_track(action, name, source='', delete_events=False):
    sync_tracks(action)
    if not name or name == DEFAULT_TRACK:
        raise ValueError('__Default is permanent and cannot be deleted')
    if (source, name) not in track_keys(action):
        raise ValueError('The selected track no longer exists')
    indices = [i for i, e in enumerate(action.witcher_events) if e.track_name == name and e.source_file == source]
    selected = action.witcher_event_index
    for index in reversed(indices):
        if delete_events:
            action.witcher_events.remove(index)
        else:
            action.witcher_events[index].track_name = ''
    for index in reversed(range(len(action.witcher_event_tracks))):
        track = action.witcher_event_tracks[index]
        if (track.source_file, track.name) == (source, name):
            action.witcher_event_tracks.remove(index)
    if indices:
        action.witcher_events_known = True
    if delete_events:
        selected -= sum(i < selected for i in indices)
    action.witcher_event_index = max(0, min(selected, len(action.witcher_events) - 1))
    action.witcher_event_track_index = min(action.witcher_event_track_index, len(action.witcher_event_tracks) - 1)
    return len(indices)


def move_track(action, source, name, direction):
    sync_tracks(action)
    if (source, name) == ('', DEFAULT_TRACK):
        raise ValueError('__Default always stays at the top')
    keys = [(t.source_file, t.name) for t in action.witcher_event_tracks]
    if (source, name) not in keys:
        raise ValueError('The selected track no longer exists')
    current = keys.index((source, name))
    target = {'UP': current - 1, 'DOWN': current + 1, 'TOP': 1, 'BOTTOM': len(keys) - 1}[direction]
    target = max(1, min(target, len(keys) - 1))
    action.witcher_event_tracks.move(current, target)
    action.witcher_event_track_index = target


def export_events(action, rate, first, last, name=None):
    if not getattr(action, 'witcher_events_known', False):
        return None
    out = []
    for event in action.witcher_events:
        start, end = event.frame, event.frame + event.duration
        if start > last + 1e-4 or end < first - START_SLACK:
            continue
        if not all(math.isfinite(v) for v in (start, end)) or event.duration < 0:
            raise ValueError(f"Invalid time for event '{event.event_name}'")
        if get_event_class(event.source_game, event.event_type) is None:
            raise ValueError(f"Unknown event class '{event.event_type}' on event '{event.event_name}'")
        data = json.loads(event.payload or '{}')
        raw = dict(data.get('raw_fields', {}))
        raw.update(eventName=event.event_name, animationName=name or action.name, trackName=event.track_name)
        if 'soundEventName' in raw or event.sound_event or 'soundEventName' in event_declared_field_names(event.source_game, event.event_type):
            raw['soundEventName'] = event.sound_event
        start = max(start, first)
        if end > last + 1e-4:
            end = last
        raw['startTime'] = (start - first) / rate
        if event_inherits_from(event.source_game, event.event_type, 'CExtAnimDurationEvent') or 'duration' in raw:
            raw['duration'] = max(0.0, end - start) / rate
        out.append(dict(event_type=event.event_type, source_game=event.source_game,
                        source_file=event.source_file,
                        raw_fields=raw, field_types=data.get('field_types', {})))
    return out


def copy_events(source, target):
    if source is None or source == target:
        return
    if getattr(source, 'witcher_events_known', False):
        store(target, [values(event) for event in source.witcher_events])
        for key in ('witcher_event_baseline', 'witcher_event_range'):
            if key in source:
                target[key] = source[key]
    target.witcher_event_tracks.clear()
    for track in source.witcher_event_tracks:
        item = target.witcher_event_tracks.add()
        item.name, item.source_file = track.name, track.source_file
        item.locked = track.locked
    target.witcher_track_layout_initialized = source.witcher_track_layout_initialized
    target.witcher_event_track_index = source.witcher_event_track_index


def track_rows(action, group_unnamed=False, with_keys=False, show_empty=True):
    # CEdTimeline::RecreateTracks creates and pins this row even with no events.
    default = ('', DEFAULT_TRACK)
    rows, order = {default: []}, []
    containers = {'': []}
    for index, event in enumerate(action.witcher_events):
        containers.setdefault(event.source_file, []).append((index, event))

    def track_key(key):
        source, name = key
        if source:
            filename = source.replace('\\', '/').rsplit('/', 1)[-1]
            stem, _, extension = filename.rpartition('.')
            name = '{' + stem + ' - ' + extension + '}.' + name
        # GreaterTrack compares case-sensitively, putting group separators first.
        return name.replace('.', '\0')

    for source, items in containers.items():
        # The main entry sorts events by start time on load; linked files follow it.
        if not source:
            items = sorted(items, key=lambda item: item[1].frame)
        for index, event in items:
            name = event.track_name
            if not name or name == DEFAULT_TRACK:
                name = event.event_type.removeprefix('CExtAnim') if group_unnamed else DEFAULT_TRACK
            key = source, name
            if key not in rows:
                rows[key] = []
                order.append(key)
                # Plain AddTrack appends to the draw list. Track::InsertTrack sorts
                # that list when adding a child, including linked event-file groups.
                if source or '.' in name:
                    order.sort(key=track_key)
            rows[key].append((index, event))
    for track in action.witcher_event_tracks:
        key = track.source_file, track.name
        if key not in rows:
            rows[key] = []
            order.append(key)
            if track.source_file or '.' in track.name:
                order.sort(key=track_key)
    if action.witcher_track_layout_initialized:
        saved = [(t.source_file, t.name) for t in action.witcher_event_tracks]
        saved_set = set(saved)
        order = [key for key in saved if key != default] + [key for key in order if key not in saved_set]
    return [(key if with_keys else key[1], rows[key]) for key in [default] + order
            if show_empty or key == default or rows[key]]


def _baseline(action):
    baseline = action.get('witcher_event_baseline', '')
    if not baseline:
        return None
    records = json.loads(baseline)
    for record in records:
        record.setdefault('source_file', '')
    return records


def modified(action, first, last):
    return _baseline(action) != [values(e) for e in action.witcher_events] or any(
        abs(a - b) > 1e-4 for a, b in zip(action.get('witcher_event_range', (first, last)), (first, last)))


def event_sources(action, first, last):
    before, after = _baseline(action) or [], [values(e) for e in action.witcher_events]
    sources = {e['source_file'] for e in before + after if e['source_file']}
    retimed = any(abs(a - b) > 1e-4 for a, b in zip(action.get('witcher_event_range', (first, last)), (first, last)))
    result = {}
    for source in sources:
        old = [e for e in before if e['source_file'] == source]
        new = [e for e in after if e['source_file'] == source]
        raw = json.loads((old or new)[0]['payload']).get('raw_fields', {})
        result[source] = {'changed': retimed or old != new, 'animation': raw.get('animationName', '')}
    return result


def nla_strips(owner):
    data = owner.animation_data
    if data is None or not data.use_nla:
        return []
    solo = any(track.is_solo for track in data.nla_tracks)
    return [strip for track in data.nla_tracks if not track.mute and (not solo or track.is_solo)
            for strip in track.strips if strip.type == 'CLIP' and strip.action and not strip.mute]


def clip(owner, frame, selected=False):
    data = owner.animation_data if owner is not None else None
    if data is None:
        return None, None
    strips = nla_strips(owner)
    if selected:
        chosen = [strip for strip in strips if strip.select]
        if len(chosen) == 1:
            return chosen[0].action, chosen[0]
    if data.action is not None:
        return data.action, None
    active = [strip for strip in strips if strip.frame_start <= frame <= strip.frame_end]
    if active:
        return active[-1].action, active[-1]
    return (strips[-1].action, strips[-1]) if strips and selected else (None, None)


def action_frame(strip, frame):
    if strip is None:
        return float(frame)
    if strip.use_animated_time:
        return float(strip.strip_time)
    length = strip.action_frame_end - strip.action_frame_start
    elapsed = (frame - strip.frame_start) / strip.scale
    if length > 0 and elapsed > 0:
        remainder = elapsed % length
        elapsed = length if remainder < 1e-4 and abs(frame - strip.frame_end) < 1e-4 else remainder
    return strip.action_frame_end - elapsed if strip.use_reverse else strip.action_frame_start + elapsed


def timeline_events(owner, first, last):
    data = owner.animation_data
    if data is None:
        return None
    if data.action is not None and getattr(data.action, 'witcher_events_known', False):
        return [values(e) for e in data.action.witcher_events]
    records, known = [], False
    for strip in nla_strips(owner):
        if not getattr(strip.action, 'witcher_events_known', False):
            continue
        known = True
        for record in strip_events(strip, first, last):
            if record not in records:
                records.append(record)
    return records if known else None


def strip_events(strip, first, last):
    if strip.use_animated_time:
        raise ValueError('Event timing requires a strip with fixed playback speed')
    length = strip.action_frame_end - strip.action_frame_start
    if length <= 0:
        return
    for repeat in range(math.ceil(strip.repeat)):
        base = strip.frame_start + repeat * length * strip.scale
        for event in strip.action.witcher_events:
            a = max(strip.action_frame_start, event.frame)
            b = min(strip.action_frame_end, event.frame + event.duration)
            if a > b + 1e-4:
                continue
            if strip.use_reverse:
                a, b = strip.action_frame_end - b, strip.action_frame_end - a
            else:
                a, b = a - strip.action_frame_start, b - strip.action_frame_start
            a, b = base + a * strip.scale, base + b * strip.scale
            lo, hi = max(first, strip.frame_start), min(last, strip.frame_end)
            if a > hi + 1e-4 or b < lo - 1e-4:
                continue
            record = values(event)
            record.update(frame=max(lo, a), duration=max(0.0, min(hi, b) - max(lo, a)))
            yield record
