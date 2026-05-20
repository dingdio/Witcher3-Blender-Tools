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
from xml.sax.saxutils import escape


TOOL_EXE = "w3speech-phoneme-extractor.exe"
LIPSYNC_CREATOR_EXE = "w3speech-lipsync-creator.exe"
CONVERTER_EXE = "w3speech-converter.exe"
WWISE_CONSOLE_EXE = "WwiseConsole.exe"
TOOL_DATA_DIR = "data"
TOOL_DLL = "espeak_lib.dll"
TOOL_REPO_LIPSYNC_DIR = "repo.lipsync"
ENV_TOOLS_DIR = "W3_RADISH_LIPSYNC_TOOLS"
ENV_WWISE_BIN = "W3_WWISE_BIN_NEXT_GEN"
TARGET_WWISE_VERSION = (2021, 1, 14, 8108)
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
    redkit_audio_file: Path = None
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


def _as_wwise_console_path(path):
    path = Path(path)
    if path.is_file():
        return path
    candidate = path / WWISE_CONSOLE_EXE
    if candidate.is_file():
        return candidate
    return None


def _available_windows_drive_roots():
    if os.name != "nt":
        return []
    roots = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        try:
            if root.exists():
                roots.append(root)
        except OSError:
            continue
    return roots


def _candidate_audiokinetic_roots():
    roots = []
    for root_var in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        root = normalize_path(os.environ.get(root_var, ""))
        if root:
            roots.append(Path(root) / "Audiokinetic")

    for drive_root in _available_windows_drive_roots():
        roots.extend([
            drive_root / "Program Files (x86)" / "Audiokinetic",
            drive_root / "Program Files" / "Audiokinetic",
        ])

    seen = set()
    for root in roots:
        key = str(root).casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            if root.is_dir():
                yield root
        except OSError:
            continue


def _wwise_version_tuple(path):
    version_re = re.compile(r"^Wwise\s*(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)
    for part in Path(path).parts:
        match = version_re.match(part)
        if not match:
            continue
        values = [int(value) if value is not None else 0 for value in match.groups()]
        return tuple(values)
    return None


def _wwise_version_sort_key(path):
    version = _wwise_version_tuple(path)
    if not version:
        return (9999, 9999, 9999, 999999, str(path).casefold())
    target = TARGET_WWISE_VERSION
    return (
        abs(version[0] - target[0]),
        abs(version[1] - target[1]),
        abs(version[2] - target[2]),
        abs(version[3] - target[3]) if len(version) > 3 else target[3],
        str(path).casefold(),
    )


def _sorted_wwise_consoles(candidates):
    consoles = []
    seen = set()
    for candidate in candidates:
        console = _as_wwise_console_path(candidate)
        if not console:
            continue
        key = str(console).casefold()
        if key in seen:
            continue
        seen.add(key)
        consoles.append(console)
    return sorted(consoles, key=_wwise_version_sort_key)


def _wwise_bin_from_template_settings(tools_dir):
    if not tools_dir:
        return None
    settings_path = Path(tools_dir).parent / "lipsync.project-template" / "_settings_.bat"
    if not settings_path.is_file():
        return None
    pattern = re.compile(r"^\s*SET\s+DIR_WWISE_BIN_NEXT_GEN\s*=\s*(.+?)\s*$", re.IGNORECASE)
    try:
        lines = settings_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        match = pattern.match(line)
        if match:
            return Path(normalize_path(match.group(1)))
    return None


def _candidate_wwise_consoles(explicit_path="", tools_dir=None):
    explicit = normalize_path(explicit_path)
    if explicit:
        console = _as_wwise_console_path(Path(explicit))
        if console:
            yield console
            return

    candidates = []

    for env_name in (ENV_WWISE_BIN, "WWISE_BIN", "WWISECONSOLE"):
        env_path = normalize_path(os.environ.get(env_name, ""))
        if env_path:
            candidates.append(Path(env_path))

    template_path = _wwise_bin_from_template_settings(tools_dir)
    if template_path:
        candidates.append(template_path)

    path_exe = shutil.which(WWISE_CONSOLE_EXE)
    if path_exe:
        candidates.append(Path(path_exe))

    common_candidates = []
    for audio_root in _candidate_audiokinetic_roots():
        common_candidates.extend(audio_root.glob("Wwise*/Authoring/x64/Release/bin/WwiseConsole.exe"))
    candidates.extend(common_candidates)

    for console in _sorted_wwise_consoles(candidates):
        yield console


def auto_detect_wwise_console(tools_dir=None):
    candidates = []

    for env_name in (ENV_WWISE_BIN, "WWISE_BIN", "WWISECONSOLE"):
        env_path = normalize_path(os.environ.get(env_name, ""))
        if env_path:
            candidates.append(Path(env_path))

    template_path = _wwise_bin_from_template_settings(tools_dir)
    if template_path:
        candidates.append(template_path)

    path_exe = shutil.which(WWISE_CONSOLE_EXE)
    if path_exe:
        candidates.append(Path(path_exe))

    for audio_root in _candidate_audiokinetic_roots():
        candidates.extend(audio_root.glob("Wwise*/Authoring/x64/Release/bin/WwiseConsole.exe"))

    sorted_consoles = _sorted_wwise_consoles(candidates)
    return sorted_consoles[0] if sorted_consoles else None


def find_wwise_console(explicit_path="", tools_dir=None):
    checked = []
    for candidate in _candidate_wwise_consoles(explicit_path, tools_dir=tools_dir):
        checked.append(str(candidate))
        return candidate

    explicit = normalize_path(explicit_path)
    if explicit:
        checked.append(explicit)
    template_path = _wwise_bin_from_template_settings(tools_dir)
    if template_path:
        checked.append(str(template_path))
    raise RadishLipsyncError(
        "WwiseConsole was not found. WEM generation requires Audiokinetic Wwise 2021.1.x "
        "(Radish recommends v2021.1.14). "
        f"Set Wwise Console in add-on preferences or {ENV_WWISE_BIN}. "
        f"Checked: {'; '.join(checked) if checked else 'configured paths, PATH, and common Program Files locations'}"
    )


def get_wwise_status(explicit_path="", tools_dir=None):
    for candidate in _candidate_wwise_consoles(explicit_path, tools_dir=tools_dir):
        return candidate, []
    return None, [WWISE_CONSOLE_EXE]


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
        "Radish Lipsync 4 REDkit tools were not found. Set Radish Lipsync 4 REDkit in add-on preferences "
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
        "Full Radish Lipsync 4 REDkit tools were not found. Set Radish Lipsync 4 REDkit in add-on preferences "
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


def _safe_workspace_component(value, fallback):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return name or fallback


def _prepare_workspace(work_root, line_id, language, stable_workspace=False):
    work_root = Path(work_root)
    if not stable_workspace:
        workspace = work_root / f"{line_id}_{uuid.uuid4().hex[:8]}"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    workspace = (
        work_root
        / "lines"
        / _safe_workspace_component(language, "en")
        / _safe_workspace_component(line_id, "line")
    )
    try:
        work_root_resolved = work_root.resolve(strict=False)
        workspace_resolved = workspace.resolve(strict=False)
    except OSError:
        work_root_resolved = work_root.absolute()
        workspace_resolved = workspace.absolute()
    if workspace_resolved == work_root_resolved or work_root_resolved not in workspace_resolved.parents:
        raise RadishLipsyncError(f"Refusing to replace unsafe Radish workspace: {workspace}")

    if workspace.exists():
        if workspace.is_dir():
            shutil.rmtree(workspace)
        else:
            workspace.unlink()
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _prepare_job(text, speaker, line_id, language, work_root, stable_workspace=False):
    text = str(text or "").strip()
    if not text:
        raise RadishLipsyncError("Enter voiceline text before generating lipsync.")

    speaker = normalize_speaker(speaker)
    line_id = normalize_line_id(line_id) or make_line_id()
    language = (str(language or "en").strip().lower() or "en")

    workspace = _prepare_workspace(work_root, line_id, language, stable_workspace=stable_workspace)

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


def create_job(wav_path, text, speaker, line_id, language, work_root, stable_workspace=False):
    wav_path = Path(wav_path)
    if not wav_path.is_file():
        raise RadishLipsyncError(f"WAV file does not exist: {wav_path}")
    if wav_path.suffix.lower() != ".wav":
        raise RadishLipsyncError("The lipsync extractor expects a .wav file.")

    job = _prepare_job(text, speaker, line_id, language, work_root, stable_workspace=stable_workspace)
    job_wav = job.workspace / f"{voiceover_name(job.speaker, job.line_id)}.wav"
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


def create_text_job(text, speaker, line_id, language, work_root, stable_workspace=False):
    return _prepare_job(text, speaker, line_id, language, work_root, stable_workspace=stable_workspace)


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


def _find_output_wem_file(job, output_dir):
    audio_dir = Path(output_dir) / "speech" / job.language / "audio"
    expected = audio_dir / f"{voiceover_name(job.speaker, job.line_id)}.wem"
    if expected.is_file():
        return expected
    candidates = sorted(audio_dir.glob(f"*{job.line_id}.wem")) if audio_dir.is_dir() else []
    if candidates:
        return candidates[0]
    candidates = sorted(Path(output_dir).glob(f"**/*{job.line_id}.wem"))
    return candidates[0] if candidates else None


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
        raise RadishLipsyncError(f"Radish Lipsync 4 REDkit folder is missing: {', '.join(missing)}")
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


WWISE_PROJECT_XML = """<?xml version="1.0" encoding="utf-8"?>
<WwiseDocument Type="Project" SchemaVersion="103" WwiseVersion="v2021.1.14" WwiseBuild="8108">
	<ProjectInfo>
		<Project Name="NG-Conversion" ID="{5261EE36-1D35-4A14-A115-71A60549C2D9}">
			<Platforms>
				<Platform Name="Windows" ReferencePlatform="Windows" ID="{7FF96B77-B71F-4427-A1AC-107A531A2D87}"/>
			</Platforms>
			<LanguageList>
				<Language Name="English(US)" ID="{F33E4898-F1C8-4350-AFD8-B961D9A59B09}"/>
				<Language Name="External" ID="{55CBD34B-2069-4062-90A2-E68F37E2D0A8}"/>
			</LanguageList>
			<PropertyList>
				<Property Name="ExternalSourcesInputPath" Type="string">
					<ValueList>
						<Value Platform="Windows">..\\ext-sources.wsources</Value>
					</ValueList>
				</Property>
				<Property Name="ExternalSourcesOutputPath" Type="string">
					<ValueList>
						<Value Platform="Windows">GeneratedSoundBanks\\Windows\\</Value>
					</ValueList>
				</Property>
				<Property Name="SoundBankHeaderFilePath" Type="string" Value="GeneratedSoundBanks\\"/>
				<Property Name="SoundBankPaths" Type="string">
					<ValueList>
						<Value Platform="Windows">GeneratedSoundBanks\\Windows\\</Value>
					</ValueList>
				</Property>
				<Property Name="SoundBankPostGenerateCustomCmdDescription" Type="string">
					<ValueList>
						<Value Platform="Windows">Copy Streamed Files</Value>
					</ValueList>
				</Property>
				<Property Name="SoundBankPostGenerateCustomCmdLines" Type="string">
					<ValueList>
						<Value Platform="Windows">"$(CopyStreamedFilesExePath)" -info "$(InfoFilePath)" -outputpath "$(SoundBankPath)" -banks "$(SoundBankListAsTextFile)" -languages "$(LanguageList)"</Value>
					</ValueList>
				</Property>
				<Property Name="SoundBankPreGenerateCustomCmdDescription" Type="string">
					<ValueList>
						<Value Platform="Windows"></Value>
					</ValueList>
				</Property>
				<Property Name="SoundBankPreGenerateCustomCmdLines" Type="string">
					<ValueList>
						<Value Platform="Windows"></Value>
					</ValueList>
				</Property>
				<Property Name="WwiseVersionWhenCreated" Type="string" Value="Branch=wwise_v2021.1|Build=8108|VersionName=v2021.1.14|Config=Release"/>
			</PropertyList>
			<EnvironmentalSettings>
				<ObsOccCurves>
					<Comment></Comment>
					<ObsOccCurve CurveXType="Obstruction" CurveYType="Volume">
						<PlatformCurve Platform="Linked" EnableCurve="true">
							<Curve Name="" ID="{D2AEE3B1-6A0E-49EB-B381-D830062E4F12}">
								<PropertyList>
									<Property Name="Flags" Type="int32" Value="3"/>
								</PropertyList>
								<PointList>
									<Point>
										<XPos>0</XPos>
										<YPos>0</YPos>
										<Flags>5</Flags>
									</Point>
									<Point>
										<XPos>100</XPos>
										<YPos>-200</YPos>
										<Flags>37</Flags>
									</Point>
								</PointList>
							</Curve>
						</PlatformCurve>
					</ObsOccCurve>
					<ObsOccCurve CurveXType="Obstruction" CurveYType="LPF">
						<PlatformCurve Platform="Linked" EnableCurve="true">
							<Curve Name="" ID="{5864DB64-B956-4DBA-BFAC-25FBB52A141C}">
								<PropertyList>
									<Property Name="Flags" Type="int32" Value="1"/>
								</PropertyList>
								<PointList>
									<Point>
										<XPos>0</XPos>
										<YPos>0</YPos>
										<Flags>5</Flags>
									</Point>
									<Point>
										<XPos>100</XPos>
										<YPos>100</YPos>
										<Flags>37</Flags>
									</Point>
								</PointList>
							</Curve>
						</PlatformCurve>
					</ObsOccCurve>
					<ObsOccCurve CurveXType="Obstruction" CurveYType="HPF">
						<PlatformCurve Platform="Linked" EnableCurve="false">
							<Curve Name="" ID="{2E06C9C7-62C2-4D39-A048-BF330AF15344}">
								<PropertyList>
									<Property Name="Flags" Type="int32" Value="1"/>
								</PropertyList>
								<PointList>
									<Point>
										<XPos>0</XPos>
										<YPos>0</YPos>
										<Flags>5</Flags>
									</Point>
									<Point>
										<XPos>100</XPos>
										<YPos>100</YPos>
										<Flags>37</Flags>
									</Point>
								</PointList>
							</Curve>
						</PlatformCurve>
					</ObsOccCurve>
					<ObsOccCurve CurveXType="Occlusion" CurveYType="Volume">
						<PlatformCurve Platform="Linked" EnableCurve="true">
							<Curve Name="" ID="{3107DE54-E036-42F8-870D-B49CCDBBB7E3}">
								<PropertyList>
									<Property Name="Flags" Type="int32" Value="3"/>
								</PropertyList>
								<PointList>
									<Point>
										<XPos>0</XPos>
										<YPos>0</YPos>
										<Flags>5</Flags>
									</Point>
									<Point>
										<XPos>100</XPos>
										<YPos>-200</YPos>
										<Flags>37</Flags>
									</Point>
								</PointList>
							</Curve>
						</PlatformCurve>
					</ObsOccCurve>
					<ObsOccCurve CurveXType="Occlusion" CurveYType="LPF">
						<PlatformCurve Platform="Linked" EnableCurve="true">
							<Curve Name="" ID="{8020E32B-9337-4A01-AC03-B03764F2DD54}">
								<PropertyList>
									<Property Name="Flags" Type="int32" Value="1"/>
								</PropertyList>
								<PointList>
									<Point>
										<XPos>0</XPos>
										<YPos>0</YPos>
										<Flags>5</Flags>
									</Point>
									<Point>
										<XPos>100</XPos>
										<YPos>100</YPos>
										<Flags>37</Flags>
									</Point>
								</PointList>
							</Curve>
						</PlatformCurve>
					</ObsOccCurve>
					<ObsOccCurve CurveXType="Occlusion" CurveYType="HPF">
						<PlatformCurve Platform="Linked" EnableCurve="false">
							<Curve Name="" ID="{9BE3252E-1142-4248-83E0-FE84E97EF613}">
								<PropertyList>
									<Property Name="Flags" Type="int32" Value="1"/>
								</PropertyList>
								<PointList>
									<Point>
										<XPos>0</XPos>
										<YPos>0</YPos>
										<Flags>5</Flags>
									</Point>
									<Point>
										<XPos>100</XPos>
										<YPos>100</YPos>
										<Flags>37</Flags>
									</Point>
								</PointList>
							</Curve>
						</PlatformCurve>
					</ObsOccCurve>
				</ObsOccCurves>
			</EnvironmentalSettings>
			<DefaultConversion Name="Vorbis Quality High" ID="{6D1B890C-9826-4384-BF07-C15223E9FB56}"/>
		</Project>
	</ProjectInfo>
</WwiseDocument>
"""


WWISE_CONVERSION_WORK_UNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<WwiseDocument Type="WorkUnit" ID="{799FCD6C-E801-4B49-8267-96E7E2343F4D}" SchemaVersion="103">
	<Conversions>
		<WorkUnit Name="Default Work Unit" ID="{799FCD6C-E801-4B49-8267-96E7E2343F4D}" PersistMode="Standalone">
			<ChildrenList>
				<Conversion Name="Default Conversion Settings" ID="{6D1B890C-9826-4384-BF07-C15223E9FB56}">
					<PropertyList>
						<Property Name="Channels" Type="int32">
							<ValueList>
								<Value Platform="Windows">4</Value>
							</ValueList>
						</Property>
						<Property Name="LRMix" Type="Real64">
							<ValueList>
								<Value Platform="Windows">0</Value>
							</ValueList>
						</Property>
						<Property Name="MaxSampleRate" Type="int32">
							<ValueList>
								<Value Platform="Windows">0</Value>
							</ValueList>
						</Property>
						<Property Name="MinSampleRate" Type="int32">
							<ValueList>
								<Value Platform="Windows">0</Value>
							</ValueList>
						</Property>
						<Property Name="SampleRate" Type="int32">
							<ValueList>
								<Value Platform="Windows">0</Value>
							</ValueList>
						</Property>
					</PropertyList>
					<ConversionPluginInfoList>
						<ConversionPluginInfo Platform="Windows">
							<ConversionPlugin Name="" ID="{D628404A-C64E-4DC8-ACB1-B51A81699A72}" PluginName="WEM Opus" CompanyID="0" PluginID="20"/>
						</ConversionPluginInfo>
					</ConversionPluginInfoList>
				</Conversion>
			</ChildrenList>
		</WorkUnit>
	</Conversions>
</WwiseDocument>
"""


def _xml_attr(value):
    return escape(str(value), {'"': "&quot;"})


def _write_wwise_external_sources(conf_dir, source_audio_dir):
    source_audio_dir = Path(source_audio_dir)
    sources = sorted(
        path for path in source_audio_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".wav", ".ogg"}
    )
    if not sources:
        raise RadishLipsyncError(f"No WAV/OGG audio files found for Wwise conversion in {source_audio_dir}")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<ExternalSourcesList SchemaVersion="1" Root="{_xml_attr(source_audio_dir)}">',
    ]
    for source in sources:
        lines.append(
            f'   <Source Path="{_xml_attr(source.name)}" Conversion="Default Conversion Settings" />'
        )
    lines.append("</ExternalSourcesList>")
    path = Path(conf_dir) / "ext-sources.wsources"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _ensure_wwise_conversion_project(workspace, source_audio_dir):
    conf_dir = Path(workspace) / "conf.wwise"
    project_dir = conf_dir / "nextgen"
    conversion_dir = project_dir / "Conversion Settings"
    conversion_dir.mkdir(parents=True, exist_ok=True)
    _write_wwise_external_sources(conf_dir, source_audio_dir)
    project_path = project_dir / "NG-Conversion.wproj"
    project_path.write_text(WWISE_PROJECT_XML, encoding="utf-8")
    (conversion_dir / "Default Work Unit.wwu").write_text(WWISE_CONVERSION_WORK_UNIT_XML, encoding="utf-8")
    return project_path


def _find_output_wem_in_dir(job, output_dir):
    output_dir = Path(output_dir)
    expected = output_dir / f"{voiceover_name(job.speaker, job.line_id)}.wem"
    if expected.is_file():
        return expected
    candidates = sorted(output_dir.glob(f"*{job.line_id}*.wem"))
    if candidates:
        return candidates[0]
    candidates = sorted(output_dir.glob("*.wem"))
    if candidates:
        return candidates[0]
    raise RadishLipsyncError(f"Wwise did not create a .wem audio file in {output_dir}")


def run_wwise_conversion(job, wwise_console, source_audio_dir=None, output_dir=None, timeout=180):
    wwise_console = Path(wwise_console)
    if not wwise_console.is_file():
        raise RadishLipsyncError(f"WwiseConsole does not exist: {wwise_console}")
    source_audio_dir = Path(source_audio_dir) if source_audio_dir else job.workspace
    output_dir = Path(output_dir) if output_dir else job.workspace / "en.wem"
    output_dir.mkdir(parents=True, exist_ok=True)
    project_path = _ensure_wwise_conversion_project(job.workspace, source_audio_dir)
    command = [
        str(wwise_console),
        "convert-external-source",
        str(project_path),
        "--output",
        "WINDOWS",
        str(output_dir),
        "--no-wwise-dat",
    ]
    result = _run_tool_command(command, wwise_console.parent, timeout, "Wwise audio conversion")
    return RadishResult(
        phoneme_file=None,
        stdout=result.stdout,
        stderr=result.stderr,
        redkit_audio_file=_find_output_wem_in_dir(job, output_dir),
    )


def run_lipsync_creator(job, phoneme_file, tools_dir, text_only=False, timeout=120):
    tools_dir = Path(tools_dir)
    missing = validate_full_tools_dir(tools_dir, include_converter=False)
    if missing:
        raise RadishLipsyncError(f"Full Radish Lipsync 4 REDkit folder is missing: {', '.join(missing)}")

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
        raise RadishLipsyncError(f"Full Radish Lipsync 4 REDkit folder is missing: {', '.join(missing)}")

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
        redkit_audio_file=_find_output_wem_file(job, output_dir),
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
