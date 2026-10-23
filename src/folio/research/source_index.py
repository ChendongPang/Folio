"""Shared source manifests, light structure indexes and versioned text caches.

Only immutable source data is shared. OCR output and business-task state stay in
their own workspaces. No model decides cache paths or executes preprocessing.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path, PurePosixPath
from threading import Event, RLock
from time import monotonic
from uuid import uuid4

from filelock import FileLock, Timeout

from folio.models import digest
from folio.research.formats import (
    READER_VERSION,
    SUPPORTED_SUFFIXES,
    SourceDocument,
    SourceFormatError,
    read_source,
    source_hash,
)
from folio.research.navigation import NAVIGATION_VERSION, render_navigation
from folio.research.structure import build_structure

INDEX_VERSION = 1
MAX_FILES = 2000
_VERSION_CACHE_SIZE = 4096
_version_cache = OrderedDict()
_version_lock = RLock()
_prepared_cache = OrderedDict()
_prepared_lock = RLock()
_preparation_locks = [RLock() for _ in range(32)]
_PREPARED_CACHE_SIZE = 16


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise SourceIndexError('Source preprocessing was cancelled')


class SourceIndexError(ValueError):
    """An invalid index path or a source that no longer matches its snapshot."""


def _bytes(value):
    # Stable identity encoding; disk indentation must not change cache or file IDs.
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _no_symlinks(path):
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise SourceIndexError('Source and index paths cannot pass through symbolic links')
    return path


def _directory(path):
    path = _no_symlinks(path)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _regular(path):
    return _no_symlinks(path).is_file()


def _atomic_json(path, value):
    _no_symlinks(path)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return _hash(payload)


def _atomic_text(path, value):
    _no_symlinks(path)
    payload = value.encode("utf-8")
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return _hash(payload)


@contextmanager
def _lock(path, cancelled=None):
    _no_symlinks(path)
    lock = FileLock(path, timeout=120)
    if cancelled is None:
        lock.acquire()
    else:
        deadline = monotonic() + 120
        while True:
            _check_cancelled(cancelled)
            try:
                lock.acquire(timeout=min(0.1, max(0, deadline - monotonic())))
                break
            except Timeout:
                if monotonic() >= deadline:
                    raise
    try:
        _check_cancelled(cancelled)
        _no_symlinks(path)
        yield
    finally:
        lock.release()


def _json(path, expected_hash=None):
    if not _regular(path):
        return None
    payload = path.read_bytes()
    if expected_hash is not None and _hash(payload) != expected_hash:
        return None
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None


def _document(value, version, format_name):
    """Validate cached data before admitting it to the evidence reader."""
    try:
        document = SourceDocument.from_dict(value)
        if (
            document.source_version != version
            or document.metadata.get("reader_version") != READER_VERSION
            or document.metadata.get("format") != format_name
            or not isinstance(document.warnings, list)
            or not all(isinstance(warning, str) for warning in document.warnings)
            or any(
                not isinstance(block.locator, str)
                or not isinstance(block.text, str)
                or not isinstance(block.kind, str)
                or not isinstance(block.metadata, dict)
                for block in document.blocks
            )
        ):
            return None
        return document
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _fingerprint(path):
    stat = _no_symlinks(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def source_version(path: Path | str) -> str:
    """Hash changed files once; unchanged snapshots only need a metadata check.

    The cache is process-local and bounded. Inode/device identity catches file
    replacement; ctime catches same-size edits even when mtime is restored. No
    persisted metadata is accepted as proof of content identity after a restart.
    Metadata is checked again after hashing, before publishing the result.
    """
    path = _no_symlinks(path)
    before = _fingerprint(path)
    key = (str(path), before)
    with _version_lock:
        if version := _version_cache.get(key):
            _version_cache.move_to_end(key)
            return version
    version = source_hash(path)
    if _fingerprint(path) != before:
        raise SourceIndexError('The source changed while being read. Retry')
    with _version_lock:
        _version_cache[key] = version
        _version_cache.move_to_end(key)
        while len(_version_cache) > _VERSION_CACHE_SIZE:
            _version_cache.popitem(last=False)
    return version


def _read_version(path, expected_version):
    _no_symlinks(path)
    if not path.is_file() or source_version(path) != expected_version:
        raise SourceIndexError('The source snapshot changed. Create a new source-index version')


def cached_source(path: Path | str, cache_root: Path | str | None = None, *, cancelled: Event | None = None) -> SourceDocument:
    """Read a source once per reader, format and content version across projects.

    Object payloads contain no project paths or source filenames. Explicit callers
    choose the shared cache root; absent a root, nothing is stored on disk.
    """
    path = _no_symlinks(Path(path))
    _check_cancelled(cancelled)
    if not path.is_file():
        raise SourceFormatError('The source file does not exist')
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise SourceFormatError(f"Unsupported source format: {suffix or '(no extension)'}")
    version = source_version(path)
    if cache_root is None:
        document = read_source(path)
        _check_cancelled(cancelled)
        _read_version(path, version)
        if document.source_version != version:
            raise SourceIndexError('The source changed during parsing')
        return document
    root = _directory(cache_root)
    objects = _directory(root / "objects")
    key = _hash(_bytes([READER_VERSION, suffix, version]))
    target = objects / (key + ".json")
    with _lock(objects / (key + ".lock"), cancelled):
        stored = _json(target)
        document = None
        if isinstance(stored, dict):
            payload = stored.get("document")
            if stored.get("payload_hash") == _hash(_bytes(payload)):
                document = _document(payload, version, suffix[1:])
        if document is None:
            document = read_source(path)
            _check_cancelled(cancelled)
            _read_version(path, version)
            if document.source_version != version:
                raise SourceIndexError('The source changed during parsing')
            payload = document.model_dump()
            _atomic_json(target, {"payload_hash": _hash(_bytes(payload)), "document": payload})
        _read_version(path, version)
        return document


def prepare_uploaded_source(
    path: Path | str, cache_root: Path | str, *, display_name: str | None = None,
    cancelled: Event | None = None,
) -> SourceDocument:
    """Validate an upload and derive its navigation beside the original file.

    The shared parsed object is reused, so this adds no second Office/PDF parse.
    Later research snapshots retain their own version-bound indexes and aliases.
    """
    path = _no_symlinks(path)
    if path.name in {"structure.json", "structure.md"}:
        raise SourceIndexError('The uploaded-original path conflicts with the structure-cache path')
    document = cached_source(path, cache_root, cancelled=cancelled)
    _check_cancelled(cancelled)
    _read_version(path, document.source_version)
    name = display_name or path.name
    structure = build_structure(document) | {"path": path.name, "display_name": name}
    with _lock(path.parent / ".structure.lock", cancelled):
        _atomic_json(path.parent / "structure.json", structure)
        _atomic_text(path.parent / "structure.md", render_navigation(structure, name))
        _read_version(path, document.source_version)
    return document


class SourceIndex:
    """A project index; call prepare once before querying its frozen manifest."""

    def __init__(self, source_root, index_root=None, *, cache_root=None):
        self.source_root = _no_symlinks(source_root).resolve()
        if not self.source_root.is_dir():
            raise SourceIndexError('The source directory does not exist')
        target = index_root or self.source_root.with_name(self.source_root.name + ".index")
        target = _no_symlinks(target).resolve()
        if target.is_relative_to(self.source_root):
            raise SourceIndexError('The index directory cannot be inside the read-only source directory')
        self.index_root = _directory(target)
        self.cache_root = None
        if cache_root is not None:
            cache_root = _no_symlinks(cache_root).resolve()
            if cache_root.is_relative_to(self.source_root):
                raise SourceIndexError('The shared cache cannot be inside the read-only source directory')
            self.cache_root = _directory(cache_root)
        for name in ("structures", "content"):
            _directory(self.index_root / name)
        self._manifest = None
        self._shared = False

    @property
    def prepared(self):
        return self._manifest is not None

    @property
    def manifest(self):
        if self._manifest is None:
            raise SourceIndexError('Prepare the source index first')
        return deepcopy(self._manifest)

    @property
    def files(self):
        return self.manifest["files"]

    @property
    def source_snapshot_id(self):
        if self._manifest is None:
            raise SourceIndexError('Prepare the source index first')
        return self._manifest["snapshot_id"]

    @property
    def snapshot_id(self):
        return self.source_snapshot_id

    def _scan(self, cancelled=None):
        result = {}
        _no_symlinks(self.source_root)
        for path in sorted(self.source_root.rglob("*")):
            _check_cancelled(cancelled)
            _no_symlinks(path)
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                relative = path.relative_to(self.source_root).as_posix()
                result[relative] = {"version": source_version(path), "size": path.stat().st_size}
                if len(result) > MAX_FILES:
                    raise SourceIndexError(f'The project exceeds {MAX_FILES} source files. Split the project')
        return result

    @staticmethod
    def _identity(relative, item):
        file_id = _hash(_bytes([relative, item["version"], READER_VERSION]))[:32]
        return {
            "file_id": file_id,
            "name": PurePosixPath(relative).name,
            "format": PurePosixPath(relative).suffix.lower()[1:],
            "size": item["size"],
            "version": item["version"],
            "structure_ref": f"structures/{file_id}.json",
            "content_ref": f"content/{file_id}.json",
        }

    def _reuse(self, previous, expected):
        if not isinstance(previous, dict) or any(previous.get(k) != v for k, v in expected.items()):
            return None
        if previous.get("status") != "ready":
            return None
        structure = _json(self.index_root / expected["structure_ref"], previous.get("structure_hash"))
        if (
            not isinstance(structure, dict)
            or structure.get("source_version") != expected["version"]
            or structure.get("reader_version") != READER_VERSION
            or not previous.get("content_hash")
            or not previous.get("structure_hash")
            or not _regular(self.index_root / expected["content_ref"])
        ):
            return None
        return previous

    def _build(self, relative, expected, cancelled=None):
        path = self.source_root / relative
        _read_version(path, expected["version"])
        entry = dict(expected)
        try:
            document = cached_source(path, self.cache_root, cancelled=cancelled)
            if document.source_version != expected["version"]:
                raise SourceIndexError('The source changed during indexing')
            structure = build_structure(document) | {"path": relative}
            entry["content_hash"] = _atomic_json(
                self.index_root / entry["content_ref"], document.model_dump()
            )
            entry.update(status="ready", warnings=structure.get("warnings", document.warnings))
        except SourceFormatError as exc:
            structure = {
                "version": 1, "path": relative, "source_version": expected["version"],
                "reader_version": READER_VERSION, "format": expected["format"],
                "metadata": {}, "nodes": [], "warnings": [str(exc)],
                "complete": False, "structure_status": "unreadable",
            }
            entry.update(status="unreadable", warnings=[str(exc)], content_hash=None)
        _read_version(path, expected["version"])
        entry["structure_hash"] = _atomic_json(self.index_root / entry["structure_ref"], structure)
        return self._ensure_navigation(relative, entry, structure)

    def _ensure_navigation(self, relative, entry, structure=None):
        """Repair only the derived view; old valid indexes need no source reparse."""
        reference = str(PurePosixPath(entry["structure_ref"]).with_suffix(".md"))
        target = self.index_root / reference
        if (
            entry.get("navigation_ref") == reference
            and entry.get("navigation_version") == NAVIGATION_VERSION
            and entry.get("navigation_hash")
            and _regular(target)
            and _hash(target.read_bytes()) == entry["navigation_hash"]
        ):
            return entry
        if structure is None:
            structure = _json(self.index_root / entry["structure_ref"], entry["structure_hash"])
        if not isinstance(structure, dict):
            raise SourceIndexError('The structure index is corrupted or missing. Prepare the index again')
        return entry | {
            "navigation_ref": reference,
            "navigation_version": NAVIGATION_VERSION,
            "navigation_hash": _atomic_text(target, render_navigation(structure, relative)),
        }

    def prepare(self, *, cancelled: Event | None = None):
        """Preprocess every supported source without a model call.

        A new prepare explicitly adopts the current source versions. In contrast,
        load and structure never silently move an existing object to a new version.
        """
        if self._shared:
            raise SourceIndexError('A shared index snapshot cannot be prepared again. Obtain the current source index instead')
        with _lock(self.index_root / ".prepare.lock", cancelled):
            scanned = self._scan(cancelled)
            previous = _json(self.index_root / "manifest.json")
            previous_files = {}
            if (
                isinstance(previous, dict)
                and previous.get("index_version") == INDEX_VERSION
                and previous.get("reader_version") == READER_VERSION
                and isinstance(previous.get("files"), dict)
            ):
                previous_files = previous["files"]
            files = {}
            for relative, item in scanned.items():
                _check_cancelled(cancelled)
                expected = self._identity(relative, item)
                entry = self._reuse(previous_files.get(relative), expected) or self._build(
                    relative, expected, cancelled
                )
                files[relative] = self._ensure_navigation(relative, entry)
            if self._scan(cancelled) != scanned:
                raise SourceIndexError('The source changed during preprocessing. Retry')
            manifest = {
                "index_version": INDEX_VERSION,
                "snapshot_id": digest(json.dumps(scanned, sort_keys=True)),
                "reader_version": READER_VERSION,
                "files": files,
            }
            if previous != manifest:
                _atomic_json(self.index_root / "manifest.json", manifest)
            self._manifest = manifest
        return self.manifest

    def validate(self, path):
        """Check one source against this immutable manifest without loading text."""
        return self._entry(path)["version"]

    def _entry(self, relative):
        if (
            not isinstance(relative, str) or "\x00" in relative or "\\" in relative
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
        ):
            raise SourceIndexError('Source paths must be relative paths within the index')
        if self._manifest is None:
            raise SourceIndexError('Prepare the source index first')
        entry = self._manifest["files"].get(relative)
        if entry is None:
            raise SourceIndexError('The source is absent from the current index snapshot')
        _read_version(self.source_root / relative, entry["version"])
        return entry

    def structure(self, path):
        """Return only the lightweight structure, without opening content caches."""
        entry = self._entry(path)
        result = _json(self.index_root / entry["structure_ref"], entry["structure_hash"])
        if not isinstance(result, dict):
            raise SourceIndexError('The structure index is corrupted or missing. Prepare the index again')
        return result

    def navigation_path(self, path):
        """Return the Markdown cache location for one immutable source version."""
        entry = self._entry(path)
        return self.index_root / PurePosixPath(entry["structure_ref"]).with_suffix(".md")

    def navigation(self, path):
        """Read persisted navigation, repairing absent/corrupt Markdown from JSON."""
        entry = self._entry(path)
        self._ensure_navigation(path, entry)
        return self.navigation_path(path).read_text(encoding="utf-8")

    def load(self, path, *, cancelled: Event | None = None):
        """Load and check text; rebuild corrupt caches only for the same version."""
        entry = self._entry(path)
        _check_cancelled(cancelled)
        if entry["status"] == "unreadable":
            raise SourceFormatError("；".join(entry["warnings"]))
        payload = _json(self.index_root / entry["content_ref"], entry["content_hash"])
        document = _document(payload, entry["version"], entry["format"])
        if document is not None:
            return document

        with _lock(self.index_root / ".prepare.lock", cancelled):
            entry = self._entry(path)
            # Another reader may have repaired the same deterministic cache while
            # this caller waited for the project lock.
            payload = _json(self.index_root / entry["content_ref"], entry["content_hash"])
            document = _document(payload, entry["version"], entry["format"])
            if document is not None:
                return document
            rebuilt = self._build(path, self._identity(path, entry), cancelled)
            self._manifest["files"][path] = rebuilt
            current = _json(self.index_root / "manifest.json")
            if isinstance(current, dict) and current.get("snapshot_id") == self.source_snapshot_id:
                current["files"][path] = rebuilt
                _atomic_json(self.index_root / "manifest.json", current)
            if rebuilt["status"] != "ready":
                raise SourceFormatError("；".join(rebuilt["warnings"]))
            payload = _json(self.index_root / rebuilt["content_ref"], rebuilt["content_hash"])
            document = _document(payload, rebuilt["version"], rebuilt["format"])
            if document is None:
                raise SourceIndexError('The body cache remains incomplete after rebuilding')
            return document

def _source_signature(root, cancelled=None):
    """Check directory membership and metadata, never decode source contents."""
    if not _no_symlinks(root).is_dir():
        raise SourceIndexError('The source directory does not exist')
    signature = [(".", _fingerprint(root))]
    files = 0
    for path in sorted(root.rglob("*")):
        _check_cancelled(cancelled)
        signature.append((path.relative_to(root).as_posix(), _fingerprint(path)))
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            files += 1
            if files > MAX_FILES:
                raise SourceIndexError(f'The project exceeds {MAX_FILES} source files. Split the project')
    return tuple(signature)


def _index_signature(index):
    # Missing/corrupt structures must still trigger prepare, while normal task
    # startup need not open every structure header. Content hashes are verified
    # when content is loaded, just as with ordinary SourceIndex.prepare().
    paths = {index.index_root / "manifest.json"}
    for entry in index._manifest["files"].values():
        paths.add(index.index_root / entry["structure_ref"])
        if entry.get("navigation_ref"):
            paths.add(index.index_root / entry["navigation_ref"])
        if entry.get("content_hash"):
            paths.add(index.index_root / entry["content_ref"])
    try:
        return tuple((str(path), _fingerprint(path)) for path in sorted(paths))
    except FileNotFoundError:
        return None


def get_prepared_index(source_root, *, cache_root=None, cancelled: Event | None = None):
    """Reuse a frozen prepared view while source and index metadata are unchanged.

    A changed directory gets a new object; holders of an earlier object keep
    their earlier snapshot identity and reject changed files. The cache is
    process-local, bounded and validated on every acquisition. It never treats
    persisted stat metadata as proof of source contents across process restarts.
    """
    root = _no_symlinks(source_root).resolve()
    cache = _no_symlinks(cache_root).resolve() if cache_root is not None else None
    key = (str(root), str(cache) if cache is not None else None, READER_VERSION, INDEX_VERSION)
    # Serialize same-project preparation without blocking unrelated projects on
    # the global cache mutex. A fixed lock pool keeps idle lock memory bounded.
    guard = _preparation_locks[hash(key) % len(_preparation_locks)]
    while not guard.acquire(timeout=0.1):
        _check_cancelled(cancelled)
    try:
        _check_cancelled(cancelled)
        signature = _source_signature(root, cancelled)
        with _prepared_lock:
            previous = _prepared_cache.get(key)
        if previous is not None:
            index, source_signature, index_signature = previous
            if signature == source_signature and _index_signature(index) == index_signature:
                with _prepared_lock:
                    if key in _prepared_cache:
                        _prepared_cache.move_to_end(key)
                return index
        index = SourceIndex(root, cache_root=cache)
        index.prepare(cancelled=cancelled)
        if _source_signature(root, cancelled) != signature:
            raise SourceIndexError('The source changed during preprocessing. Retry')
        index_signature = _index_signature(index)
        if index_signature is None:
            raise SourceIndexError('The source index changed during preprocessing. Retry')
        index._shared = True
        with _prepared_lock:
            _prepared_cache[key] = (index, signature, index_signature)
            _prepared_cache.move_to_end(key)
            while len(_prepared_cache) > _PREPARED_CACHE_SIZE:
                _prepared_cache.popitem(last=False)
        return index
    finally:
        guard.release()
