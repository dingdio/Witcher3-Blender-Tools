"""REDkit animation sources and editor event labels."""
import copy
import json
import logging
import os
from functools import lru_cache

from ..CR2W.common_blender import _get_redkit_depot_roots, _resolve_redkit_repo_file
from ..repo_paths import is_under_root, source_roots

log = logging.getLogger(__name__)


def depot_roots():
    return [root for root in _get_redkit_depot_roots() if os.path.isdir(root)]


def depot_file(context, path):
    roots = depot_roots()
    if not roots:
        return ''
    if os.path.isabs(path):
        for root in roots + source_roots(context, 'w3'):
            if is_under_root(path, root):
                path = os.path.relpath(path, root)
                break
        else:
            return ''
    return _resolve_redkit_repo_file(path, roots)


def is_depot_file(path):
    return bool(path) and any(is_under_root(path, root) for root in depot_roots())


@lru_cache(maxsize=16)
def _read_events(path, stamp, size, clip):
    from ..CR2W.dc_anims import getCR2W, open_cr2w_read_stream, _read_cutscene_entry_events
    file = getCR2W(open_cr2w_read_stream(path), anim_name=clip or None, do_read_anim_buffer=True)
    chunks = file.CHUNKS.CHUNKS
    if not clip:
        chunk = next(c for c in chunks if c.name == 'CExtAnimEventsFile')
        return _read_cutscene_entry_events(chunk, file), []
    root = next(c for c in chunks if c.name == 'CSkeletalAnimationSet')
    links = root.GetVariableByName('extAnimEvents')
    paths = [h.DepotPath for h in getattr(links, 'Handles', ()) if h.DepotPath]
    for ptr in root.GetVariableByName('animations').value:
        entry = chunks[ptr - 1]
        anim = chunks[entry.GetVariableByName('animation').Value - 1]
        if anim.GetVariableByName('name').Index.String == clip:
            return _read_cutscene_entry_events(entry, file), paths
    return [], []


def read_events(path, clip=''):
    stat = os.stat(path)
    return _read_events(path, stat.st_mtime_ns, stat.st_size, clip)


def clip_events(context, path, clip):
    entries, links = read_events(path, clip)
    result = list(entries)
    for link in links:
        resolved = depot_file(context, link)
        if not resolved:
            log.warning('REDkit event file not found: %s', link)
            continue
        external, _ = read_events(resolved)
        for source in external:
            if source.animation_name == clip:
                event = copy.copy(source)
                event.source_file = link
                result.append(event)
    return result


def event_key(event):
    raw = {k: v for k, v in event.raw_fields.items()
           if k not in {'animationName', 'trackName', 'eventName', 'startTime', 'duration'}}
    return (event.type_name, event.event_name, round(event.start_time, 5), round(event.duration, 5),
            json.dumps(raw, sort_keys=True))


def restore_tracks(entries, reference):
    lookup = {}
    for event in reference:
        lookup.setdefault(event_key(event), set()).add((event.track_name, getattr(event, 'source_file', '')))
    restored = []
    for original in entries:
        event = copy.copy(original)
        matches = lookup.get(event_key(event), set())
        if not event.track_name and len(matches) == 1:
            event.track_name, event.source_file = next(iter(matches))
            event.raw_fields = dict(event.raw_fields, trackName=event.track_name)
        restored.append(event)
    return restored


def prepare_entry(context, path, entry):
    if not path or not str(path).lower().endswith('.w2anims'):
        return entry
    direct = is_depot_file(path)
    if not direct and not getattr(context.scene, 'witcher_anim_redkit_tracks', False):
        return entry
    reference = depot_file(context, path)
    if not reference:
        return entry
    try:
        reference_events = clip_events(context, reference, entry.animation.name)
        result = copy.copy(entry)
        result.entries = (reference_events if direct else restore_tracks(entry.entries, reference_events))
        return result
    except (OSError, ValueError, AttributeError, IndexError, StopIteration) as exc:
        log.warning('REDkit event metadata unavailable for %s: %s', entry.animation.name, exc)
        return entry
