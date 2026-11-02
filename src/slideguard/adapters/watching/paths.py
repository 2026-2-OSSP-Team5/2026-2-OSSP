import os
from collections.abc import Iterator
from pathlib import Path


def absolute_path(path: Path) -> Path:
    """Keep a deleted path addressable without resolving its former target."""
    return Path(os.path.abspath(path))


def accepts_path(root: Path, path: Path, *, recursive: bool) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return (
        path.suffix.lower() == ".pptx"
        and not path.name.startswith(("~$", ".", "~"))
        and (recursive or len(relative.parts) == 1)
    )


def presentation_paths(root: Path, *, recursive: bool) -> Iterator[Path]:
    candidates = root.rglob("*") if recursive else root.iterdir()
    return (p for p in candidates if accepts_path(root, p, recursive=recursive) and p.is_file())
