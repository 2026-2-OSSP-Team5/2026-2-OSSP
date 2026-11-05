from pathlib import Path

from slideguard.adapters.watching.paths import absolute_path, accepts_path, presentation_paths
from slideguard.adapters.watching.revisions import saved_revision
from slideguard.application.ports import SavedRevision


class SavedFileCatalog:
    """Use the monitor's scope and complete-archive/hash checks for collection."""

    def __init__(self, path: Path, *, recursive: bool = False):
        self.path = path.resolve()
        self.recursive = recursive

    def list_paths(self) -> tuple[Path, ...]:
        if not self.path.is_dir():
            raise NotADirectoryError(f"Watch directory does not exist: {self.path}")
        return tuple(sorted(presentation_paths(self.path, recursive=self.recursive)))

    def current_revision(self, path: Path) -> SavedRevision | None:
        path = absolute_path(path)
        if not accepts_path(self.path, path, recursive=self.recursive):
            raise ValueError(f"Path is outside the collection scope: {path}")
        try:
            # The COM reader identifies its canonical source. Reject aliases rather
            # than silently delivering a different path or escaping the watched root.
            if path.resolve(strict=True) != path:
                raise ValueError(f"Aliased presentation paths are not supported: {path}")
            return saved_revision(path)[0]
        except FileNotFoundError:
            return None
