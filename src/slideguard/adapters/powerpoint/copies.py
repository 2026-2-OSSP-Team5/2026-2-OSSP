"""Prepare and validate a private saved PPTX revision without changing its source."""

import os
import shutil
import stat
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import (
    PowerPointCleanupError,
    PowerPointOpenError,
    PowerPointSessionError,
    PowerPointSourceChangedError,
)

_REQUIRED_PARTS = {"[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml"}


@dataclass(frozen=True)
class SavedPresentationCopy:
    source_path: Path
    copy_path: Path
    source_sha256: str


def _signature(info: os.stat_result) -> tuple[int, ...]:
    # Windows path/handle queries can report different ctime values for unchanged
    # bytes. Identity, size, mtime and the separate byte digests determine a revision.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _source_path(path: Path) -> Path:
    path = Path(path)
    if path.suffix.lower() != ".pptx" or path.name.startswith(("~", ".")):
        raise PowerPointOpenError("validate", "Choose a saved .pptx file, excluding lock/temporary files")
    source = path.resolve(strict=True)
    if source.suffix.lower() != ".pptx" or source.name.startswith(("~", ".")):
        raise PowerPointOpenError("validate", "The resolved source must be a saved .pptx file")
    if not source.is_file():
        raise PowerPointOpenError("validate", "The source must be a regular .pptx file")
    return source


def _unchanged(source: Path, before: os.stat_result) -> None:
    try:
        same = _signature(before) == _signature(source.stat())
    except FileNotFoundError:
        same = False
    if not same:
        raise PowerPointSourceChangedError("copy", "Source changed while preparing the saved copy; retry")


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _validate_package(path: Path) -> None:
    failure = None
    try:
        with zipfile.ZipFile(path) as package:
            names = package.namelist()
            if not _REQUIRED_PARTS.issubset(names) or len(names) != len(set(names)):
                raise ValueError("Missing PPTX parts or duplicate ZIP members")
            if package.testzip() is not None:
                raise ValueError("PPTX ZIP member failed its CRC check")
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error) as error:
        failure = PowerPointOpenError("validate", f"Invalid or incomplete PPTX copy: {error}")
    if failure is not None:
        raise failure from None


def _prepare_copy(source: Path, destination: Path) -> str:
    before = source.stat()
    if not stat.S_ISREG(before.st_mode):
        raise PowerPointOpenError("copy", "The source must be a regular file")
    try:
        with source.open("rb") as original, destination.open("xb") as copied:
            if _signature(before) != _signature(os.fstat(original.fileno())):
                raise PowerPointSourceChangedError("copy", "Source was replaced before copying; retry")
            shutil.copyfileobj(original, copied, length=1024 * 1024)
            copied.flush()
            original.seek(0)
            source_digest = file_digest(original, "sha256").hexdigest()
            if _signature(before) != _signature(os.fstat(original.fileno())):
                raise PowerPointSourceChangedError("copy", "Source changed during copying; retry")
        _unchanged(source, before)
        copy_digest = _sha256(destination)
        if source_digest != copy_digest:
            raise PowerPointSourceChangedError("copy", "Copied bytes differ from the source revision; retry")
        _validate_package(destination)
        _unchanged(source, before)
        return copy_digest
    except OSError:
        _unchanged(source, before)
        raise


@contextmanager
def saved_pptx_copy(path: Path) -> Iterator[SavedPresentationCopy]:
    source = _source_path(path)
    temporary = TemporaryDirectory(prefix="slideguard-read-")
    primary_error = None
    try:
        copy_path = Path(temporary.name) / "deck.pptx"
        digest = _prepare_copy(source, copy_path)
        yield SavedPresentationCopy(source, copy_path, digest)
        # Check only the copy here: a new source save during COM reading is a newer
        # revision, not a reason to relabel the snapshot with that newer hash.
        if _sha256(copy_path) != digest:
            raise PowerPointOpenError("copy", "The temporary copy changed during extraction")
    except BaseException as error:
        primary_error = error
        raise
    finally:
        failure = None
        try:
            temporary.cleanup()
        except OSError as error:
            failure = PowerPointCleanupError("remove-copy", str(error))
        if failure is not None:
            if primary_error is None:
                raise failure from None
            if isinstance(primary_error, PowerPointSessionError):
                primary_error.cleanup_errors += (failure,)
            primary_error.add_note(str(failure))
