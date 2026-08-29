import hashlib
import logging
import os
import re
import struct
import threading
import urllib.request
from pathlib import Path

from ..extension_paths import get_extension_user_dir, require_online_access


log = logging.getLogger(__name__)

RECOMMENDED_MODEL_NAME = "large-v3-turbo-q5_0"
RECOMMENDED_MODEL_FILE = "ggml-large-v3-turbo-q5_0.bin"
RECOMMENDED_MODEL_SIZE = "547 MiB"
RECOMMENDED_MODEL_SHA1 = "e050f7970618a659205450ad97eb95a18d69c9ee"
RECOMMENDED_MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{RECOMMENDED_MODEL_FILE}"
DEFAULT_MODEL_NAME = RECOMMENDED_MODEL_NAME
DEFAULT_MODEL_FILE = RECOMMENDED_MODEL_FILE
WHISPER_SAMPLE_RATE = 16000
WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
WAVE_SUBTYPE_GUID_TAIL = b"\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"

_model = None
_model_path = None
_model_lock = threading.Lock()


class SpeechToTextError(RuntimeError):
    pass


def package_dir():
    return Path(__file__).resolve().parent


def bundled_model_path():
    return package_dir() / "models" / DEFAULT_MODEL_FILE


def user_model_dir(create=False):
    path = Path(get_extension_user_dir(create=create)) / "whisper_models"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def user_model_path(create=False):
    return user_model_dir(create=create) / DEFAULT_MODEL_FILE


def resolve_model_path(explicit_path=""):
    path = str(explicit_path or "").strip()
    if path:
        expanded = Path(os.path.expandvars(os.path.expanduser(path.strip('"'))))
        if expanded.is_file():
            return expanded
        raise SpeechToTextError(f"Whisper model file does not exist: {expanded}")

    downloaded = user_model_path(create=False)
    if downloaded.is_file():
        return downloaded

    bundled = bundled_model_path()
    if bundled.is_file():
        return bundled
    raise SpeechToTextError("No Whisper model configured.")


def file_sha1(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_recommended_model(path):
    path = Path(path)
    return path.is_file() and file_sha1(path).lower() == RECOMMENDED_MODEL_SHA1


def download_recommended_model(target_path="", progress_callback=None):
    target = Path(target_path) if target_path else user_model_path(create=True)
    target = Path(os.path.expandvars(os.path.expanduser(str(target).strip('"'))))
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_file() and verify_recommended_model(target):
        if progress_callback:
            progress_callback(target.stat().st_size, target.stat().st_size)
        return target

    require_online_access()
    temp_path = target.with_name(target.name + ".download")
    if temp_path.exists():
        temp_path.unlink()

    request = urllib.request.Request(
        RECOMMENDED_MODEL_URL,
        headers={"User-Agent": "witcher-blender-tools"},
    )
    downloaded = 0
    sha1 = hashlib.sha1()

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            total_text = response.headers.get("Content-Length", "")
            total = int(total_text) if total_text.isdigit() else 0
            with open(temp_path, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024 * 8)
                    if not chunk:
                        break
                    handle.write(chunk)
                    sha1.update(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

        actual_sha1 = sha1.hexdigest().lower()
        if actual_sha1 != RECOMMENDED_MODEL_SHA1:
            raise SpeechToTextError(
                f"Downloaded Whisper model failed checksum: expected {RECOMMENDED_MODEL_SHA1}, got {actual_sha1}"
            )

        temp_path.replace(target)
        return target
    except SpeechToTextError:
        if temp_path.exists():
            temp_path.unlink()
        raise
    except Exception as exc:
        if temp_path.exists():
            temp_path.unlink()
        raise SpeechToTextError(f"Could not download Whisper model: {exc}") from exc


def is_available(explicit_path=""):
    try:
        resolve_model_path(explicit_path)
        import pywhispercpp  # noqa: F401

        return True, ""
    except Exception as exc:
        return False, str(exc)


def _load_model(model_path):
    from pywhispercpp.model import Model

    return Model(
        str(model_path),
        print_progress=False,
        print_realtime=False,
        print_timestamps=False,
        redirect_whispercpp_logs_to=None,
    )


def get_model(explicit_path=""):
    global _model, _model_path

    model_path = resolve_model_path(explicit_path)
    with _model_lock:
        if _model is None or _model_path != model_path:
            log.info("Loading Whisper STT model: %s", model_path)
            _model = _load_model(model_path)
            _model_path = model_path
    return _model


def _clean_transcript(text):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text


def _decode_pcm_wav(raw, sample_width):
    import numpy as np

    if sample_width == 1:
        audio = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        return (audio - 128.0) / 128.0
    if sample_width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sample_width == 3:
        bytes_in = np.frombuffer(raw, dtype=np.uint8)
        if len(bytes_in) % 3:
            bytes_in = bytes_in[: len(bytes_in) - (len(bytes_in) % 3)]
        triplets = bytes_in.reshape(-1, 3)
        sign = (triplets[:, 2] & 0x80) != 0
        padded = np.zeros((triplets.shape[0], 4), dtype=np.uint8)
        padded[:, :3] = triplets
        padded[sign, 3] = 0xFF
        return padded.view("<i4").reshape(-1).astype(np.float32) / 8388608.0
    if sample_width == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    raise SpeechToTextError(f"Unsupported WAV sample width: {sample_width * 8} bit")


def _decode_float_wav(raw, sample_width):
    import numpy as np

    if sample_width == 4:
        return np.frombuffer(raw, dtype="<f4").astype(np.float32)
    if sample_width == 8:
        return np.frombuffer(raw, dtype="<f8").astype(np.float32)
    raise SpeechToTextError(f"Unsupported float WAV sample width: {sample_width * 8} bit")


def _read_wav_chunks(wav_path):
    with open(wav_path, "rb") as handle:
        riff = handle.read(12)
        if len(riff) != 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise SpeechToTextError("Speech-to-text currently expects a little-endian RIFF/WAVE file.")

        fmt_data = None
        raw_data = None
        while True:
            header = handle.read(8)
            if not header:
                break
            if len(header) != 8:
                raise SpeechToTextError("Invalid WAV chunk header.")

            chunk_id, chunk_size = header[:4], struct.unpack("<I", header[4:8])[0]
            chunk_data = handle.read(chunk_size)
            if len(chunk_data) != chunk_size:
                raise SpeechToTextError("Unexpected end of WAV file.")
            if chunk_size % 2:
                handle.seek(1, os.SEEK_CUR)

            if chunk_id == b"fmt ":
                fmt_data = chunk_data
            elif chunk_id == b"data":
                raw_data = chunk_data
                if fmt_data is not None:
                    break

        if fmt_data is None:
            raise SpeechToTextError("WAV file has no fmt chunk.")
        if raw_data is None:
            raise SpeechToTextError("WAV file has no audio data chunk.")
        return fmt_data, raw_data


def _parse_wav_format(fmt_data):
    if len(fmt_data) < 16:
        raise SpeechToTextError("WAV fmt chunk is too short.")

    audio_format, channels, sample_rate, _byte_rate, block_align, bits_per_sample = struct.unpack_from(
        "<HHIIHH",
        fmt_data,
        0,
    )
    if channels <= 0:
        raise SpeechToTextError("WAV file has no audio channels.")
    if block_align <= 0 or block_align % channels:
        raise SpeechToTextError("WAV file has an invalid block alignment.")

    if audio_format == WAVE_FORMAT_EXTENSIBLE:
        if len(fmt_data) < 40:
            raise SpeechToTextError("WAVE_FORMAT_EXTENSIBLE fmt chunk is too short.")
        subtype_guid = fmt_data[24:40]
        if subtype_guid[4:] != WAVE_SUBTYPE_GUID_TAIL:
            raise SpeechToTextError(f"Unsupported WAVE_FORMAT_EXTENSIBLE subtype: {subtype_guid.hex()}")
        audio_format = struct.unpack("<I", subtype_guid[:4])[0]

    if audio_format not in {WAVE_FORMAT_PCM, WAVE_FORMAT_IEEE_FLOAT}:
        raise SpeechToTextError(f"Unsupported WAV format: {audio_format}")

    sample_width = block_align // channels
    expected_width = max(1, (bits_per_sample + 7) // 8)
    if sample_width < expected_width:
        raise SpeechToTextError("WAV sample width does not match its format metadata.")

    return audio_format, channels, sample_rate, block_align, sample_width


def _load_wav_data(wav_path):
    fmt_data, raw = _read_wav_chunks(wav_path)
    audio_format, channels, sample_rate, block_align, sample_width = _parse_wav_format(fmt_data)
    trim = len(raw) - (len(raw) % block_align)
    if trim <= 0:
        raise SpeechToTextError("WAV file contains no complete audio frames.")
    raw = raw[:trim]

    if audio_format == WAVE_FORMAT_PCM:
        audio = _decode_pcm_wav(raw, sample_width)
    else:
        audio = _decode_float_wav(raw, sample_width)
    return audio, channels, sample_rate


def _resample_linear(audio, source_rate, target_rate):
    import numpy as np

    if int(source_rate) == int(target_rate):
        return audio.astype(np.float32, copy=False)
    if len(audio) == 0:
        return audio.astype(np.float32, copy=False)

    duration = len(audio) / float(source_rate)
    target_len = max(1, int(round(duration * target_rate)))
    old_positions = np.linspace(0.0, duration, num=len(audio), endpoint=False, dtype=np.float64)
    new_positions = np.linspace(0.0, duration, num=target_len, endpoint=False, dtype=np.float64)
    return np.interp(new_positions, old_positions, audio).astype(np.float32)


def load_wav_for_whisper(wav_path):
    import numpy as np

    audio, channels, sample_rate = _load_wav_data(wav_path)
    if channels > 1:
        trim = len(audio) - (len(audio) % channels)
        audio = audio[:trim].reshape(-1, channels).mean(axis=1)

    audio = _resample_linear(audio, sample_rate, WHISPER_SAMPLE_RATE)
    return np.clip(audio, -1.0, 1.0).astype(np.float32, copy=False)


def transcribe_wav(wav_path, model_path="", n_threads=0):
    wav_path = Path(wav_path)
    if not wav_path.is_file():
        raise SpeechToTextError(f"WAV file does not exist: {wav_path}")
    if wav_path.suffix.lower() != ".wav":
        raise SpeechToTextError("Speech-to-text currently expects a .wav file.")

    model = get_model(model_path)
    kwargs = {
        "print_progress": False,
        "print_realtime": False,
        "print_timestamps": False,
    }
    if n_threads:
        kwargs["n_threads"] = int(n_threads)

    try:
        audio = load_wav_for_whisper(wav_path)
        segments = model.transcribe(audio, **kwargs)
    except Exception as exc:
        raise SpeechToTextError(f"Speech-to-text failed: {exc}") from exc

    text = _clean_transcript(" ".join(str(getattr(segment, "text", "") or "") for segment in segments))
    if not text:
        raise SpeechToTextError("Speech-to-text produced no text.")
    return text
