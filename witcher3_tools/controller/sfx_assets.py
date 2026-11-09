"""Resolve game soundbanks and cache decoded WAVs."""
import logging
import os
import subprocess
import tempfile

from ..CR2W.witcher_cache.SoundCache import wwise_bank as wb

log = logging.getLogger(__name__)
H = wb.fnv1_32
GROUND = H("ground_materials")
ARMOUR = H("armour_type_movement")
_BATCH = 200        # under the Windows command-line limit
_LIBRARY = (None, None)


def game_library():
    """The Library over the vanilla sound caches, rebuilt when the SoundManager is (game path change, cache refresh)."""
    global _LIBRARY
    from ..CR2W.witcher_cache.SoundCache import LoadSoundManager
    manager = LoadSoundManager()
    if _LIBRARY[0] is not manager:
        _LIBRARY = (manager, Library(manager.FileList))
    return _LIBRARY[1]


def wav_cache_dir() -> str:
    from ..extension_paths import get_cache_root
    return os.path.join(get_cache_root(create=True), "player_sfx")


class Library:
    """Every bank of the given sound cache items merged into one graph; a wem resolves to (file, offset, size)."""

    def __init__(self, items):
        self.graph, self.loose, self.errors = wb.SoundGraph(), {}, []
        files = {}
        try:
            for item in reversed(list(items)):      # later caches win
                name = (item.RawName or item.Name).replace("/", "\\").rsplit("\\", 1)[-1].lower()
                stem, ext = os.path.splitext(name)
                if ext == ".wem" and stem.isdigit():
                    self.loose.setdefault(int(stem), (item.ParentFile, item.PageOffset, item.Size))
                elif ext == ".bnk":
                    if item.ParentFile not in files:
                        files[item.ParentFile] = open(item.ParentFile, "rb")

                    def read(at, length, f=files[item.ParentFile], base=item.PageOffset):
                        f.seek(base + at)
                        return f.read(length)
                    bank = wb.parse_bank(read, item.Size)
                    self.errors += [(name, *error) for error in bank.errors]
                    self.graph.add((item.ParentFile, item.PageOffset), bank)
        finally:
            for f in files.values():
                f.close()
        if self.errors:
            log.warning("%d soundbank objects could not be decoded (first: %s)", len(self.errors), self.errors[0])
        self.surfaces = sorted({state for node in self.graph.nodes.values()
                                if node.kind == wb.SWITCH and node.group == GROUND for state in node.switches})

    def locate(self, wem):
        # Prefetched bank media may contain only the stream header.
        if wem in self.loose:
            return self.loose[wem]
        if wem in self.graph.media:
            (path, page), offset, size = self.graph.media[wem]
            return path, page + offset, size
        return None

    def switches(self, events_on_attach=(), armour=""):
        """Read spawn switches and the torso armour's movement sound type."""
        out = {}
        for name in events_on_attach:
            for action_id in self.graph.events.get(H(name), ()):
                action = self.graph.actions.get(action_id)
                if action is not None and action.type >> 8 in (wb.SET_SWITCH, wb.SET_STATE):
                    out[action.group] = action.state
        if armour and armour != "default":
            out[ARMOUR] = H(armour)
        return out

    def pool(self, events, switches) -> list:
        """Every wem the events can play under `switches`, over every ground material."""
        out = {}
        for event in events:
            for surface in (None, *self.surfaces):
                state = dict(switches) if surface is None else {**switches, GROUND: surface}
                out.update(dict.fromkeys(self.graph.sources(H(event), state)))
        return list(out)


def template_emitter_events(path) -> list:
    """eventsOnAttach of every CSoundEmitterComponent in an entity template, cooked (flat compiled data) or not."""
    from ..CR2W.CR2W_file import read_CR2W
    from ..CR2W import dc_entity
    cr2w = read_CR2W(path)
    events = []
    for source in (cr2w, dc_entity._flat_compiled_file(cr2w)):
        for chunk in getattr(getattr(source, "CHUNKS", None), "CHUNKS", None) or []:
            if chunk.Type == "CSoundEmitterComponent":
                for prop in chunk.PROPS:
                    if prop.theName == "eventsOnAttach":
                        events += [e.String for e in getattr(prop, "elements", ()) if getattr(e, "String", "")]
    return list(dict.fromkeys(events))


def _is_wav(path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        return head[:4] == b"RIFF" and head[8:] == b"WAVE" and os.path.getsize(path) > 44
    except OSError:
        return False


def decode(library, wems, cache_dir, vgmstream) -> dict:
    """{wem: wav path} for every wem that decodes; ones not cached yet are extracted and decoded in batches."""
    os.makedirs(cache_dir, exist_ok=True)
    out, todo = {}, []
    for wem in dict.fromkeys(wems):
        wav = os.path.join(cache_dir, f"{wem}.wav")
        if _is_wav(wav):
            out[wem] = wav
        elif library.locate(wem) is not None:
            todo.append(wem)
    if not todo:
        return out
    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        for wem in todo:
            path, offset, size = library.locate(wem)
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read(size)
            with open(os.path.join(tmp, f"{wem}.wem"), "wb") as f:
                f.write(data)
        for i in range(0, len(todo), _BATCH):
            # ?f uses input names relative to cwd.
            subprocess.run([vgmstream, "-i", "-o", "?f.wav", *(f"{wem}.wem" for wem in todo[i:i + _BATCH])],
                           cwd=tmp, capture_output=True, timeout=300,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for wem in todo:
            wav = os.path.join(tmp, f"{wem}.wem.wav")
            if _is_wav(wav):
                out[wem] = os.path.join(cache_dir, f"{wem}.wav")
                os.replace(wav, out[wem])
            else:
                log.warning("vgmstream could not decode wem %s", wem)
    return out
