import csv
import os
import re
import shutil
import subprocess
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path


TOOL_EXE = "w3speech-phoneme-extractor.exe"
LIPSYNC_CREATOR_EXE = "w3speech-lipsync-creator.exe"
CONVERTER_EXE = "w3speech-converter.exe"
TOOL_DATA_DIR = "data"
TOOL_DLL = "espeak_lib.dll"
TOOL_REPO_LIPSYNC_DIR = "repo.lipsync"
ENV_TOOLS_DIR = "W3_RADISH_LIPSYNC_TOOLS"
MAX_RADISH_LINE_ID = 2147483647
GENERATED_LINE_ID_BASE = 1800000000
GENERATED_LINE_ID_RANGE = 300000000

STRING_COLUMNS = (
    "ID",
    "RESOURCE",
    "PROPERTY",
    "VOICEOVER",
    "KEY",
    "BR",
    "CZ",
    "RU",
    "AR",
    "TR",
    "CN",
    "PL",
    "IT",
    "FR",
    "DE",
    "ZH",
    "ESMX",
    "EN",
    "KR",
    "ES",
    "JP",
    "HU",
)


class RadishLipsyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class RadishJob:
    workspace: Path
    wav_file: Path
    strings_file: Path
    actor_mapping_file: Path
    line_id: str
    text: str
    speaker: str
    language: str


@dataclass(frozen=True)
class RadishResult:
    phoneme_file: Path
    stdout: str
    stderr: str
    lipsyncanim_file: Path = None
    redkit_lipsync_file: Path = None
    redkit_output_dir: Path = None

    @property
    def compact_log(self):
        text = "\n".join(part for part in (self.stdout, self.stderr) if part).strip()
        return text[-4000:] if len(text) > 4000 else text


def normalize_path(path):
    if not path:
        return ""
    return os.path.expandvars(os.path.expanduser(str(path).strip().strip('"')))


def _candidate_tool_dirs(explicit_path=""):
    explicit = normalize_path(explicit_path)
    if explicit:
        yield Path(explicit)

    env_path = normalize_path(os.environ.get(ENV_TOOLS_DIR, ""))
    if env_path:
        yield Path(env_path)


def validate_tools_dir(path):
    root = Path(path)
    missing = []
    if not (root / TOOL_EXE).is_file():
        missing.append(TOOL_EXE)
    if not (root / TOOL_DATA_DIR).is_dir():
        missing.append(TOOL_DATA_DIR)
    if not (root / TOOL_DLL).is_file():
        missing.append(TOOL_DLL)
    return missing


def validate_full_tools_dir(path, include_converter=True):
    root = Path(path)
    missing = validate_tools_dir(root)
    if not (root / LIPSYNC_CREATOR_EXE).is_file():
        missing.append(LIPSYNC_CREATOR_EXE)
    if include_converter and not (root / CONVERTER_EXE).is_file():
        missing.append(CONVERTER_EXE)
    if not (root / TOOL_REPO_LIPSYNC_DIR).is_dir():
        missing.append(TOOL_REPO_LIPSYNC_DIR)
    return missing


def find_tools_dir(explicit_path=""):
    checked = []
    for candidate in _candidate_tool_dirs(explicit_path):
        try:
            root = candidate.resolve()
        except OSError:
            root = candidate
        checked.append(str(root))
        if not validate_tools_dir(root):
            return root

    raise RadishLipsyncError(
        "Radish lipsync tools were not found. Set Radish Tools Path in add-on preferences "
        f"or {ENV_TOOLS_DIR}. Expected {TOOL_EXE}, {TOOL_DLL}, and {TOOL_DATA_DIR}/. "
        f"Checked: {'; '.join(checked) if checked else 'no configured paths'}"
    )


def find_full_tools_dir(explicit_path="", include_converter=True):
    checked = []
    for candidate in _candidate_tool_dirs(explicit_path):
        try:
            root = candidate.resolve()
        except OSError:
            root = candidate
        checked.append(str(root))
        if not validate_full_tools_dir(root, include_converter=include_converter):
            return root

    required = f"{TOOL_EXE}, {LIPSYNC_CREATOR_EXE}, and {TOOL_REPO_LIPSYNC_DIR}/"
    if include_converter:
        required = f"{required}, plus {CONVERTER_EXE}"
    raise RadishLipsyncError(
        "Full Radish lipsync tools were not found. Set Radish Tools Path in add-on preferences "
        f"or {ENV_TOOLS_DIR}. Expected {required}. "
        f"Checked: {'; '.join(checked) if checked else 'no configured paths'}"
    )


def get_tool_status(explicit_path=""):
    for candidate in _candidate_tool_dirs(explicit_path):
        try:
            root = candidate.resolve()
        except OSError:
            root = candidate
        missing = validate_tools_dir(root)
        if not missing:
            return root, []
    return None, [TOOL_EXE, TOOL_DLL, TOOL_DATA_DIR]


def get_full_tool_status(explicit_path="", include_converter=True):
    for candidate in _candidate_tool_dirs(explicit_path):
        try:
            root = candidate.resolve()
        except OSError:
            root = candidate
        missing = validate_full_tools_dir(root, include_converter=include_converter)
        if not missing:
            return root, []
    missing = [TOOL_EXE, TOOL_DLL, TOOL_DATA_DIR, LIPSYNC_CREATOR_EXE, TOOL_REPO_LIPSYNC_DIR]
    if include_converter:
        missing.append(CONVERTER_EXE)
    return None, missing


def normalize_speaker(speaker):
    speaker = str(speaker or "").strip().upper()
    speaker = re.sub(r"[^A-Z0-9_]+", "_", speaker)
    return speaker or "GRLT"


def normalize_line_id(line_id):
    line_id = re.sub(r"\D+", "", str(line_id or ""))
    if line_id and int(line_id) > MAX_RADISH_LINE_ID:
        return ""
    return line_id


def make_line_id():
    return str(GENERATED_LINE_ID_BASE + (time.time_ns() % GENERATED_LINE_ID_RANGE))


def _csv_language_column(language):
    lang = str(language or "en").strip().upper()
    return lang if lang in STRING_COLUMNS else "EN"


def voiceover_name(speaker, line_id):
    return f"{normalize_speaker(speaker)}_{normalize_line_id(line_id) or str(line_id).strip()}"


def _write_strings_csv(path, line_id, text, speaker, language):
    row = {column: "" for column in STRING_COLUMNS}
    row["ID"] = line_id
    row["RESOURCE"] = 'CStoryScene "witcher_blender_tools_lipsync.w2scene"'
    row["PROPERTY"] = "Line text"
    row["VOICEOVER"] = voiceover_name(speaker, line_id)
    row["KEY"] = line_id
    row[_csv_language_column(language)] = text

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STRING_COLUMNS, delimiter=";", lineterminator="\n")
        writer.writeheader()
        writer.writerow(row)


def _write_actor_mapping(path, speaker):
    speaker_key = speaker.lower()
    profile_key = speaker_key.split("_", 1)[0] or speaker_key
    lines = [
        "; Generated by Witcher 3 Blender Tools",
        "grlt:grlt",
    ]
    for actor_key in (profile_key, speaker_key):
        if actor_key and actor_key != "grlt" and f"{actor_key}:{profile_key}" not in lines:
            lines.append(f"{actor_key}:{profile_key}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _prepare_job(text, speaker, line_id, language, work_root):
    text = str(text or "").strip()
    if not text:
        raise RadishLipsyncError("Enter voiceline text before generating lipsync.")

    speaker = normalize_speaker(speaker)
    line_id = normalize_line_id(line_id) or make_line_id()
    language = (str(language or "en").strip().lower() or "en")

    work_root = Path(work_root)
    workspace = work_root / f"{line_id}_{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True, exist_ok=True)

    strings_file = workspace / "LocalEditorStringDataBaseW3_UTF8_mod_export.csv"
    actor_mapping_file = workspace / "actor_mapping.cfg"
    _write_strings_csv(strings_file, line_id, text, speaker, language)
    _write_actor_mapping(actor_mapping_file, speaker)

    return RadishJob(
        workspace=workspace,
        wav_file=None,
        strings_file=strings_file,
        actor_mapping_file=actor_mapping_file,
        line_id=line_id,
        text=text,
        speaker=speaker,
        language=language,
    )


def create_job(wav_path, text, speaker, line_id, language, work_root):
    wav_path = Path(wav_path)
    if not wav_path.is_file():
        raise RadishLipsyncError(f"WAV file does not exist: {wav_path}")
    if wav_path.suffix.lower() != ".wav":
        raise RadishLipsyncError("The lipsync extractor expects a .wav file.")

    job = _prepare_job(text, speaker, line_id, language, work_root)
    job_wav = job.workspace / f"{job.line_id}.wav"
    shutil.copy2(wav_path, job_wav)
    return RadishJob(
        workspace=job.workspace,
        wav_file=job_wav,
        strings_file=job.strings_file,
        actor_mapping_file=job.actor_mapping_file,
        line_id=job.line_id,
        text=job.text,
        speaker=job.speaker,
        language=job.language,
    )


def create_text_job(text, speaker, line_id, language, work_root):
    return _prepare_job(text, speaker, line_id, language, work_root)


def _find_output_phoneme_file(job):
    exact = job.workspace / f"{job.line_id}.phonemes"
    if exact.is_file():
        return exact
    candidates = sorted(job.workspace.glob(f"{job.line_id}*.phonemes"))
    if candidates:
        return candidates[0]
    candidates = sorted(job.workspace.glob("*.phonemes"))
    if candidates:
        return candidates[0]
    raise RadishLipsyncError(f"Extractor did not create a .phonemes file in {job.workspace}")


def _find_output_lipsyncanim_file(job, output_dir):
    output_dir = Path(output_dir)
    exact = output_dir / f"{job.line_id}.lipsyncanim.csv"
    if exact.is_file():
        return exact
    candidates = sorted(output_dir.glob(f"{job.line_id}*.lipsyncanim.csv"))
    if candidates:
        return candidates[0]
    candidates = sorted(output_dir.glob("*.lipsyncanim.csv"))
    if candidates:
        return candidates[0]
    raise RadishLipsyncError(f"Lipsync creator did not create a .lipsyncanim.csv file in {output_dir}")


def _find_output_re_file(job, output_dir):
    lipsync_dir = Path(output_dir) / "speech" / job.language / "lipsync"
    expected = lipsync_dir / f"{voiceover_name(job.speaker, job.line_id)}.re"
    if expected.is_file():
        return expected
    candidates = sorted(lipsync_dir.glob(f"*{job.line_id}.re"))
    if candidates:
        return candidates[0]
    candidates = sorted(Path(output_dir).glob(f"**/*{job.line_id}.re"))
    if candidates:
        return candidates[0]
    raise RadishLipsyncError(f"Converter did not create a .re lipsync file under {output_dir}")


def find_job_audio_file(job):
    if job.wav_file:
        wav_file = Path(job.wav_file)
        if wav_file.is_file():
            return wav_file

    patterns = []
    try:
        patterns.append(f"{int(job.line_id):010d}*.wav")
    except (TypeError, ValueError):
        pass
    patterns.append(f"{job.line_id}*.wav")
    patterns.append("*.wav")

    seen = set()
    for pattern in patterns:
        for candidate in sorted(job.workspace.glob(pattern)):
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                return candidate
    raise RadishLipsyncError(f"Could not find generated WAV audio in {job.workspace}")


def _run_tool_command(command, tools_dir, timeout, label):
    tools_dir = Path(tools_dir)
    env = os.environ.copy()
    env["PATH"] = str(tools_dir) + os.pathsep + env.get("PATH", "")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.run(
            command,
            cwd=str(tools_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise RadishLipsyncError(f"{label} timed out after {timeout} seconds.") from exc
    except OSError as exc:
        raise RadishLipsyncError(f"Could not run {label}: {exc}") from exc

    if proc.returncode != 0:
        log_text = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        raise RadishLipsyncError(f"{label} failed with exit code {proc.returncode}.\n{log_text}")

    return RadishResult(
        phoneme_file=None,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _run_extractor_command(command, tools_dir, timeout):
    tools_dir = Path(tools_dir)
    missing = validate_tools_dir(tools_dir)
    if missing:
        raise RadishLipsyncError(f"Radish tools folder is missing: {', '.join(missing)}")
    return _run_tool_command(command, tools_dir, timeout, "Phoneme extractor")


def run_phoneme_extractor(job, tools_dir, timeout=120):
    tools_dir = Path(tools_dir)
    command = [
        str(tools_dir / TOOL_EXE),
        "--data-dir",
        str(tools_dir / TOOL_DATA_DIR),
        "--extract",
        str(job.workspace),
        "--strings-file",
        str(job.strings_file),
        "--language",
        job.language,
        "--actor-mappings",
        str(job.actor_mapping_file),
    ]
    result = _run_extractor_command(command, tools_dir, timeout)
    return RadishResult(
        phoneme_file=_find_output_phoneme_file(job),
        stdout=result.stdout,
        stderr=result.stderr,
    )


def run_text_phoneme_generator(job, tools_dir, timeout=120):
    tools_dir = Path(tools_dir)
    command = [
        str(tools_dir / TOOL_EXE),
        "--data-dir",
        str(tools_dir / TOOL_DATA_DIR),
        "--generate-from-text-only",
        "--strings-file",
        str(job.strings_file),
        "--output-dir",
        str(job.workspace),
        "--language",
        job.language,
        "--actor-mappings",
        str(job.actor_mapping_file),
    ]
    result = _run_extractor_command(command, tools_dir, timeout)
    return RadishResult(
        phoneme_file=_find_output_phoneme_file(job),
        stdout=result.stdout,
        stderr=result.stderr,
    )


def run_lipsync_creator(job, phoneme_file, tools_dir, text_only=False, timeout=120):
    tools_dir = Path(tools_dir)
    missing = validate_full_tools_dir(tools_dir, include_converter=False)
    if missing:
        raise RadishLipsyncError(f"Full Radish lipsync tools folder is missing: {', '.join(missing)}")

    output_dir = job.workspace / "en.wem"
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(tools_dir / LIPSYNC_CREATOR_EXE),
        "--create-lipsync",
        str(phoneme_file),
        "--output-dir",
        str(output_dir),
        "--repo-dir",
        str(tools_dir / TOOL_REPO_LIPSYNC_DIR),
    ]
    if text_only:
        command.append("--generate-placeholder-wav-audio")
    command.extend([
        "--actor-profiles",
        str(job.actor_mapping_file),
    ])
    result = _run_tool_command(command, tools_dir, timeout, "Lipsync creator")
    return RadishResult(
        phoneme_file=Path(phoneme_file),
        lipsyncanim_file=_find_output_lipsyncanim_file(job, output_dir),
        stdout=result.stdout,
        stderr=result.stderr,
    )


def run_converter(job, lipsyncanim_dir, tools_dir, output_dir=None, timeout=120):
    tools_dir = Path(tools_dir)
    missing = validate_full_tools_dir(tools_dir, include_converter=True)
    if missing:
        raise RadishLipsyncError(f"Full Radish lipsync tools folder is missing: {', '.join(missing)}")

    output_dir = Path(output_dir) if output_dir else job.workspace / "redkit"
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(tools_dir / CONVERTER_EXE),
        "--input-dir",
        str(lipsyncanim_dir),
        "--wav-audio-dir",
        str(job.workspace),
        "--output-dir",
        str(output_dir),
        "--strings-file",
        str(job.strings_file),
        "--language",
        job.language,
    ]
    result = _run_tool_command(command, tools_dir, timeout, "Lipsync converter")
    return RadishResult(
        phoneme_file=None,
        stdout=result.stdout,
        stderr=result.stderr,
        redkit_lipsync_file=_find_output_re_file(job, output_dir),
        redkit_output_dir=output_dir,
    )


def write_silent_wav(path, duration_seconds, sample_rate=16000):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    duration_seconds = max(0.1, float(duration_seconds or 0.0))
    frame_count = max(1, int(duration_seconds * sample_rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frame_count)
    return path
