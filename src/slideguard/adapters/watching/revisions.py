import lzma
import os
import zipfile
import zlib
from hashlib import file_digest
from pathlib import Path

from slideguard.adapters.watching.paths import absolute_path
from slideguard.application.ports import SavedRevision

type FileSignature = tuple[int, int, int]


def _stat_signature(info: os.stat_result) -> FileSignature:
    return info.st_size, info.st_mtime_ns, info.st_ino


def file_signature(path: Path) -> FileSignature:
    return _stat_signature(path.stat())


def saved_revision(path: Path) -> tuple[SavedRevision, FileSignature]:
    """A readable, complete PPTX revision; the monitor checks the quiet interval."""
    path = absolute_path(path)
    before = file_signature(path)
    with path.open("rb") as stream:
        if before != _stat_signature(os.fstat(stream.fileno())):
            raise OSError("File was replaced before checking the saved revision")
        try:
            with zipfile.ZipFile(stream) as package:
                if "ppt/presentation.xml" not in package.namelist() or package.testzip() is not None:
                    raise ValueError("Incomplete PPTX archive")
        except (zlib.error, lzma.LZMAError) as error:
            raise ValueError(f"Damaged PPTX compressed data: {error}") from None
        stream.seek(0)
        digest = file_digest(stream, "sha256").hexdigest()
        if before != _stat_signature(os.fstat(stream.fileno())):
            raise OSError("File changed while reading the saved revision")
    if before != file_signature(path):
        raise OSError("File changed while checking the saved revision")
    return SavedRevision(path, digest, before[0]), before
