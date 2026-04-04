from io import BytesIO
import mmap
import os
from pathlib import Path


class SoundCacheItem:
    """Single .wem or .bnk entry stored inside a sound cache."""

    def __init__(self, parent=None):
        self.Bundle = parent
        self.Name = ""
        self.RawName = ""
        self.ParentFile = ""
        self.NameOffset = 0
        self.PageOffset = 0
        self.Size = 0
        self.ZSize = 0
        self.Language = ""
        self.ShortName = ""

    @property
    def name(self) -> str:
        return self.Name

    @property
    def CompressionType(self) -> str:
        return "None"

    @property
    def Extension(self) -> str:
        return Path(self.Name or self.RawName).suffix.lower()

    def Extract(self, output_stream) -> None:
        if not self.ParentFile:
            raise ValueError("Sound cache item has no parent archive path.")

        with open(self.ParentFile, "rb") as handle:
            mmapped_file = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                output_stream.write(mmapped_file[self.PageOffset:self.PageOffset + self.Size])
            finally:
                mmapped_file.close()

    def extract_to_file(self, filepath: str) -> str:
        path = Path(filepath)
        ext = self.Extension
        if ext:
            path = path.with_suffix(ext)

        from ...common_blender import win_safe_path

        safe_path = win_safe_path(str(path))
        safe_tmp = safe_path + ".tmp"
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)

        try:
            with open(safe_tmp, "wb") as output:
                self.Extract(output)
            if os.path.exists(safe_path):
                os.unlink(safe_path)
            os.replace(safe_tmp, safe_path)
        except Exception:
            if os.path.exists(safe_tmp):
                os.unlink(safe_tmp)
            raise

        return str(path)

    def extract_to_memory(self) -> BytesIO:
        output = BytesIO()
        self.Extract(output)
        output.seek(0)
        return output

    def __repr__(self) -> str:
        return f"SoundCacheItem(Name={self.Name!r}, Size={self.Size}, PageOffset={self.PageOffset})"

