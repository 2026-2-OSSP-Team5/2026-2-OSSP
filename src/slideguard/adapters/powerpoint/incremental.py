"""Content-addressed slide dependencies, analogous to Git's changed-file comparison."""

import hashlib
import os
import posixpath
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
CACHE_VERSION = "slide-measurements-4"


def measurement_key(render, *, image_text=False):
    # Font installation changes invalidate saved rendering measurements too.
    digest = hashlib.sha256(f"{CACHE_VERSION}:{render}:{image_text}:{sys.platform}".encode())
    if sys.platform == "win32":
        import winreg

        digest.update(str(sys.getwindowsversion()).encode())
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Office\ClickToRun\Configuration"
            ) as office:
                digest.update(str(winreg.QueryValueEx(office, "ClientVersionToReport")[0]).encode())
        except OSError:
            pass
    roots = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
    ]
    for root in roots:
        if root.is_dir():
            for font in sorted(root.iterdir()):
                info = font.stat()
                digest.update(f"{font.name}:{info.st_size}:{info.st_mtime_ns}".encode())
    return digest.hexdigest()


def slide_fingerprints(path):
    """Hash each slide plus its transitive internal relationships and presentation settings.

    ZIP timestamps/compression and document properties are irrelevant. Shared theme,
    layout, master, media, chart and embedded-object changes invalidate their users.
    External resources cannot be content-verified and are intentionally never reused.
    """
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("ppt/presentation.xml"))
        slide_list = root.find("{*}sldIdLst")
        ids = list(slide_list) if slide_list is not None else []
        if slide_list is not None:
            root.remove(slide_list)
        global_settings = ET.tostring(root)
        relationships = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
        targets = {
            r.get("Id"): posixpath.normpath(posixpath.join("ppt", r.get("Target", ""))).lstrip("/")
            for r in relationships
        }
        dependencies = {}
        hashes = {}
        names = set(archive.namelist())

        def visit(part, seen):
            if part in seen:
                return False
            seen.add(part)
            if part not in hashes:
                hashes[part] = hashlib.sha256(archive.read(part)).digest()
                folder, name = posixpath.split(part)
                rel_path = posixpath.join(folder, "_rels", name + ".rels")
                refs = []
                if rel_path in names:
                    hashes[rel_path] = hashlib.sha256(archive.read(rel_path)).digest()
                    seen.add(rel_path)
                    for ref in ET.fromstring(archive.read(rel_path)):
                        target = ref.get("Target", "")
                        if ref.get("TargetMode") == "External" and ref.get("Type", "").endswith("/hyperlink"):
                            continue
                        refs.append(
                            None
                            if ref.get("TargetMode") == "External"
                            else posixpath.normpath(posixpath.join(folder, target)).lstrip("/")
                        )
                dependencies[part] = (rel_path if rel_path in names else None, refs)
            rel_path, refs = dependencies[part]
            if rel_path:
                seen.add(rel_path)
            external = False
            for ref in refs:
                if ref is None:
                    external = True
                else:
                    external = visit(ref, seen) or external
            return external

        # Presentation-level themes/settings are shared even without a slide relationship.
        shared = set()
        shared_external = False
        for ref in relationships:
            if not ref.get("Type", "").endswith(
                ("/slide", "/notesMaster", "/handoutMaster", "/viewProps", "/printerSettings")
            ):
                if ref.get("TargetMode") == "External":
                    shared_external = True
                else:
                    shared_external = visit(targets[ref.get("Id")], shared) or shared_external
        result = []
        for entry in ids:
            seen = set()
            external = visit(targets[entry.get(REL)], seen) or shared_external
            digest = hashlib.sha256(global_settings)
            for part in sorted(shared | seen):
                digest.update(part.encode())
                digest.update(hashes[part])
            # Empty fingerprints force remeasurement for unverified external data.
            result.append((entry.get("id"), "" if external else digest.hexdigest()))
        return tuple(result)
