"""Read-only source views and task-local, durable research workspaces."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from uuid import uuid4

from filelock import FileLock

from folio.models import Evidence, digest
from folio.research.formats import (
    READER_VERSION,
    SourceBlock,
    SourceDocument,
    source_hash,
)
from folio.research.navigation import (
    annotate_nodes,
    navigation_entries,
    navigation_header,
    render_navigation,
    resolve_node,
)
from folio.research.source_index import SourceIndexError, get_prepared_index
from folio.research.structure import build_structure


class WorkspaceError(ValueError):
    pass


def _pending_ocr_warning(warning):
    """Recognize obsolete OCR warnings in both current and persisted records."""
    return any(marker in warning.lower() for marker in (
        "图片尚未进行 ocr", "页未提取到文字", "image has not undergone ocr",
        "pages have no extracted text",
    ))


def _terms(value):
    result = re.findall(r"[a-z0-9_]+", value.casefold())
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        result.extend(run[index : index + 2] for index in range(max(1, len(run) - 1)))
    return result


def evidence_for_block(source_id, document, block, start=0, end=None):
    """One canonical identity for full source blocks and exact character slices."""
    end = len(block.text) if end is None else min(end, len(block.text))
    if start < 0 or start >= end:
        raise WorkspaceError('Invalid evidence character range')
    sliced = start != 0 or end != len(block.text)
    locator = f"{block.locator}/characters {start}:{end}" if sliced else block.locator
    text = block.text[start:end]
    metadata = {
        key: value for key, value in block.metadata.items() if key not in {"cells", "formulas"}
    } | {
        "provenance": "source",
        "source_kind": document.metadata["format"],
        "source_locator": block.locator,
        "characters_start": start,
        "characters_end": end,
    }
    if block.metadata.get("formulas"):
        metadata.update(contains_formulas=True, formula_cache_verified=False)
    extraction = block.metadata.get("extraction")
    if extraction:
        metadata["extraction"] = extraction
        metadata["requires_extraction_review"] = True
        metadata["origin_format"] = metadata["source_kind"]
        metadata["source_kind"] = "ocr"
    return Evidence(
        evidence_id=("extracted:" if extraction else "ev_")
        + digest(
            json.dumps([source_id, document.source_version, locator, text], ensure_ascii=False)
        ),
        source_id=source_id,
        source_version=document.source_version,
        locator=locator,
        text=text,
        metadata=metadata,
    )


def _chunks(source_id, document, blocks=None):
    """Retain each source block as one item; only actual output budgets slice it."""
    for block in document.blocks if blocks is None else blocks:
        if block.text.strip():
            yield evidence_for_block(source_id, document, block)


def _slice_evidence(item, start, end):
    """Slice an existing source range while preserving canonical source identity."""
    if start == 0 and end == len(item.text):
        return item
    source_start = item.metadata["characters_start"] + start
    source_end = item.metadata["characters_start"] + end
    locator = f"{item.metadata['source_locator']}/characters {source_start}:{source_end}"
    text = item.text[start:end]
    return item.model_copy(update={
        "evidence_id": ("extracted:" if item.evidence_id.startswith("extracted:") else "ev_")
        + digest(json.dumps([item.source_id, item.source_version, locator, text], ensure_ascii=False)),
        "locator": locator,
        "text": text,
        "metadata": item.metadata | {"characters_start": source_start, "characters_end": source_end},
    })


def _paging(offset, limit, max_chars=None):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise WorkspaceError('offset must be a nonnegative integer')
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise WorkspaceError('limit must be a positive integer')
    if max_chars is not None and (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars < 1
    ):
        raise WorkspaceError('max_chars must be a positive integer')


def _inspect_preview(value, text_limit, list_limit):
    """Shorten optional metadata without changing the persisted structure."""
    if isinstance(value, str):
        return value[:text_limit]
    if isinstance(value, list):
        return [_inspect_preview(item, text_limit, list_limit) for item in value[:list_limit]]
    if isinstance(value, dict):
        result = {key: _inspect_preview(item, text_limit, list_limit) for key, item in value.items()}
        for key, item in value.items():
            if isinstance(item, (str, list)) and result[key] != item:
                result[key + "_truncated"] = True
        return result
    return value


def _inspect_node(node, compact=0):
    if node is None:
        return None
    visible = {key: value for key, value in node.items() if key != "block_indices"}
    if compact:
        title_limit, list_limit = (120, 8) if compact == 1 else (40, 0)
        visible["title"] = node["title"][:title_limit]
        if compact == 1:
            visible["metadata"] = _inspect_preview(node["metadata"], title_limit, list_limit)
        else:
            # Keep readability, position, and numeric scope metadata even when
            # a sheet's merge/header previews cannot fit in the smallest budget.
            visible["metadata"] = {
                key: value for key, value in node["metadata"].items()
                if isinstance(value, (bool, int, float)) or key in {"range_precision", "header_basis"}
            }
        visible["preview_truncated"] = True
    return visible


def _inspect_page(base, compact):
    result = base | {"node": _inspect_node(base["node"], compact), "structure": []}
    if compact:
        text_limit, list_limit = (120, 8) if compact == 1 else (40, 0)
        result["metadata"] = _inspect_preview(base["metadata"], text_limit, list_limit)
        result["warnings"] = _inspect_preview(base["warnings"], text_limit, list_limit)
        if result["warnings"] != base["warnings"]:
            result["warnings_truncated"] = True
            result["warnings_total"] = len(base["warnings"])
        if len(base["path"]) > text_limit:
            result.pop("path")
            result["path_preview"] = base["path"][:text_limit]
            result["path_truncated"] = True
        result["preview_truncated"] = True
        result["preview_note"] = (
            'Only the outline preview was compacted; the full index is saved. Increase max_chars or inspect a node_id individually, then read body text with read(node_id).'
        )
    return result


class WorkspaceManager:
    def __init__(
        self, source_root: Path, task_root: Path, source_snapshot_id=None, *, cache_root=None
    ):
        self.source_root = Path(source_root).resolve()
        self.task_root = Path(task_root).resolve()
        if not self.source_root.is_dir():
            raise WorkspaceError('The source snapshot directory does not exist')
        if self.task_root == self.source_root or self.task_root.is_relative_to(self.source_root):
            raise WorkspaceError('The task workspace cannot be inside the read-only source directory')
        self.task_root.mkdir(parents=True, exist_ok=True)
        for name in ("context", "scripts", "scratch", "artifacts", ".workspace"):
            path = self.task_root / name
            if path.is_symlink():
                raise WorkspaceError('The workspace cannot be a symbolic link')
            path.mkdir(exist_ok=True)
        self._documents = {}
        self._search_indexes = {}
        try:
            self.source_index = get_prepared_index(self.source_root, cache_root=cache_root)
        except SourceIndexError as exc:
            raise WorkspaceError(str(exc)) from exc
        self._manifest = self.source_index.files
        self.source_snapshot_id = source_snapshot_id or self.source_index.source_snapshot_id
        self._cache = self.task_root / ".workspace"
        # This binds to the shared snapshot; only OCR overlays remain task-local.
        self._atomic_json(
            self._cache / "manifest.json",
            {"snapshot_id": self.source_snapshot_id,
             "index_snapshot_id": self.source_index.source_snapshot_id},
        )

    @staticmethod
    def _atomic_json(path, value):
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_text(path, text):
        if path.is_symlink():
            raise WorkspaceError('The structure navigation file cannot be a symbolic link')
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _safe_path(self, relative="", *, file=False):
        if not isinstance(relative, str) or "\x00" in relative or "\\" in relative:
            raise WorkspaceError('Source paths must be relative and use / separators')
        requested = PurePosixPath(relative)
        if requested.is_absolute() or ".." in requested.parts:
            raise WorkspaceError('Source paths cannot traverse outside the snapshot root')
        path = self.source_root.joinpath(*requested.parts)
        current = self.source_root
        for part in requested.parts:
            current /= part
            if current.is_symlink():
                raise WorkspaceError('Source paths cannot pass through symbolic links')
        if not path.resolve().is_relative_to(self.source_root):
            raise WorkspaceError('The source path is outside the snapshot root')
        if file and (relative not in self._manifest or not path.is_file()):
            raise WorkspaceError('The source is absent from the current input snapshot')
        return path

    def _overlay_paths(self, relative):
        """Pre-generation paths, retained only for migration of saved tasks."""
        key = digest(relative + self._manifest[relative]["version"] + READER_VERSION)
        return self._cache / (key + ".json"), self._cache / (key + ".structure.json")

    def _overlay_pointer(self, relative):
        return self._overlay_paths(relative)[0].with_suffix(".overlay.json")

    @staticmethod
    def _overlay_json(path, expected_hash=None):
        if path.is_symlink() or not path.is_file():
            raise WorkspaceError('The OCR commit file is missing or uses a symbolic link')
        if expected_hash is not None and source_hash(path) != expected_hash:
            raise WorkspaceError('The OCR commit file checksum does not match')
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise WorkspaceError('The OCR commit file is corrupted') from exc
        if not isinstance(result, dict):
            raise WorkspaceError('Invalid OCR commit file format')
        return result

    def _overlay_record(self, relative):
        path = self._overlay_pointer(relative)
        if not path.exists() and not path.is_symlink():
            return None
        record = self._overlay_json(path)
        generation = record.get("generation", "")
        key = self._overlay_paths(relative)[0].stem
        if (
            record.get("version") != 1
            or record.get("source_version") != self._manifest[relative]["version"]
            or record.get("reader_version") != READER_VERSION
            or not isinstance(generation, str)
            or not re.fullmatch(r"[a-f0-9]{32}", generation)
            or record.get("content_ref") != f"{key}.{generation}.content.json"
            or record.get("structure_ref") != f"{key}.{generation}.structure.json"
            or any(
                not isinstance(record.get(field), str)
                or not re.fullmatch(r"[a-f0-9]{64}", record[field])
                for field in ("content_hash", "structure_hash")
            )
        ):
            raise WorkspaceError('Invalid OCR commit version or identity')
        return record

    def _commit_overlay(self, relative, document):
        """Publish the two immutable files with one last, atomic pointer update."""
        generation = uuid4().hex
        key = self._overlay_paths(relative)[0].stem
        content = self._cache / f"{key}.{generation}.content.json"
        structure = self._cache / f"{key}.{generation}.structure.json"
        self._atomic_json(content, document.model_dump())
        self._atomic_json(structure, build_structure(document))
        self._check_version(relative)
        self._atomic_json(self._overlay_pointer(relative), {
            "version": 1, "generation": generation,
            "source_version": document.source_version, "reader_version": READER_VERSION,
            "content_ref": content.name, "structure_ref": structure.name,
            "content_hash": source_hash(content), "structure_hash": source_hash(structure),
        })
        # Failed writes must not publish uncommitted data to this process either.
        self._documents[relative] = document
        self._search_indexes.clear()

    def _extraction_blocks(self, relative, document):
        """Check backend-generated identities before restoring an OCR overlay."""
        version = self._manifest[relative]["version"]
        format_name = self._manifest[relative]["format"]
        if document.source_version != version or document.metadata.get("format") != format_name:
            raise WorkspaceError('The OCR extraction cache does not match the original version')
        office = format_name in {"docx", "xlsx", "pptx"}
        count = document.metadata.get("page_count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 2000:
            raise WorkspaceError('Invalid page count in the OCR extraction cache')
        blocks, locators = [], set()
        for block in document.blocks:
            extraction = block.metadata.get("extraction")
            if not extraction:
                continue
            if not isinstance(extraction, dict):
                raise WorkspaceError('Invalid OCR extraction identity')
            page = block.metadata.get("page")
            execution_id, image_digest = extraction.get("execution_id"), extraction.get("image_digest")
            if (
                not isinstance(execution_id, str) or not execution_id
                or not isinstance(image_digest, str) or not image_digest
                or extraction.get("source_version") != version
                or extraction.get("method") != "ocr"
                or extraction.get("operation") != ("office_ocr" if office else "ocr")
                or isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= count
                or block.kind != "paragraph" or not isinstance(block.text, str) or not block.text.strip()
                or not isinstance(block.locator, str)
                or not re.fullmatch(
                    re.escape(f"page {page}/ocr {digest(execution_id + image_digest)}/block ") + r"[1-9]\d*",
                    block.locator,
                )
                or block.locator in locators
            ):
                raise WorkspaceError('Invalid OCR extraction location, execution identity, or original version')
            locators.add(block.locator)
            blocks.append(block)
        if not blocks or len(blocks) > 100_000 or sum(len(block.text) for block in blocks) > 8_000_000:
            raise WorkspaceError('The OCR extraction cache is empty or exceeds capacity')
        return blocks

    def _migrate_overlay(self, relative):
        """Rebase old task-local OCR onto current native parsing, preserving IDs."""
        versions = dict.fromkeys((READER_VERSION, "source-reader-v2.2", "source-reader-v2.1"))
        for reader in versions:
            key = digest(relative + self._manifest[relative]["version"] + reader)
            path = self._cache / (key + ".json")
            if not path.exists() and not path.is_symlink():
                continue
            raw = self._overlay_json(path)
            try:
                old = SourceDocument.from_dict(raw)
                has_extraction = any(block.metadata.get("extraction") for block in old.blocks)
            except (KeyError, TypeError, AttributeError) as exc:
                raise WorkspaceError('Invalid legacy OCR cache format') from exc
            if not has_extraction:
                continue  # Old native-text caches are superseded by SourceIndex.
            if old.metadata.get("reader_version") != reader:
                raise WorkspaceError('The legacy OCR cache parser version does not match its identity')
            blocks = self._extraction_blocks(relative, old)
            current = self.source_index.load(relative)
            restored = SourceDocument.from_dict(current.model_dump())
            restored.blocks.extend(blocks)
            office = current.metadata["format"] in {"docx", "xlsx", "pptx"}
            page_count = old.metadata["page_count"] if office else current.metadata.get("page_count", 1)
            if any(block.metadata["page"] > page_count for block in blocks):
                raise WorkspaceError('A legacy OCR page number does not belong to the current original')
            unread = set(range(1, page_count + 1) if office else current.metadata.get("unread_pages", range(1, page_count + 1)))
            unread -= {block.metadata["page"] for block in blocks}
            restored.metadata.update(
                page_count=page_count, needs_ocr=bool(unread), unread_pages=sorted(unread),
                complete=not unread, block_count=len(restored.blocks),
                characters=sum(len(block.text) for block in restored.blocks),
            )
            if office:
                restored.metadata.update(unread_visuals=bool(unread), needs_conversion=bool(unread))
            if not unread:
                restored.warnings = [warning for warning in restored.warnings
                                     if not _pending_ocr_warning(warning)]
            restored.warnings.append('OCR text is machine-recognized. Check key fields and values against original pages or through a review.')
            if len(restored.blocks) > 100_000 or restored.metadata["characters"] > 8_000_000:
                raise WorkspaceError('Migrated OCR sources exceed reading capacity')
            self._commit_overlay(relative, restored)
            return restored
        return None

    def _check_version(self, relative):
        self._safe_path(relative, file=True)
        try:
            self.source_index.validate(relative)
        except SourceIndexError as exc:
            raise WorkspaceError(str(exc)) from exc

    def _load(self, relative):
        self._check_version(relative)
        if relative in self._documents:
            return self._documents[relative]
        record = self._overlay_record(relative)
        if record is not None:
            try:
                parsed = SourceDocument.from_dict(self._overlay_json(
                    self._cache / record["content_ref"], record["content_hash"]
                ))
                if parsed.metadata.get("reader_version") != READER_VERSION:
                    raise WorkspaceError('OCR body text does not match the committed version')
                self._extraction_blocks(relative, parsed)
            except (KeyError, TypeError, AttributeError) as exc:
                raise WorkspaceError('Invalid OCR body commit format') from exc
            self._documents[relative] = parsed
            return parsed
        try:
            parsed = self._migrate_overlay(relative) or self.source_index.load(relative)
        except SourceIndexError as exc:
            raise WorkspaceError(str(exc)) from exc
        self._documents[relative] = parsed
        return parsed

    def _structure(self, relative):
        """Inspect structure without materializing the source body."""
        self._check_version(relative)
        record = self._overlay_record(relative)
        if record is None:
            # One-time legacy migration is preprocessing. Normal inspections
            # thereafter read only the pointer and its lightweight structure.
            try:
                self._migrate_overlay(relative)
            except SourceIndexError as exc:
                raise WorkspaceError(str(exc)) from exc
            record = self._overlay_record(relative)
        if record is not None:
            structure = self._overlay_json(self._cache / record["structure_ref"], record["structure_hash"])
            if (
                structure.get("source_version") != record["source_version"]
                or structure.get("reader_version") != READER_VERSION
                or not isinstance(structure.get("nodes"), list)
            ):
                raise WorkspaceError('OCR structure does not match the committed version')
            return self._navigation_structure(relative, structure)
        try:
            return self._navigation_structure(relative, self.source_index.structure(relative))
        except SourceIndexError as exc:
            raise WorkspaceError(str(exc)) from exc

    def _navigation_structure(self, relative, structure):
        """Keep aliases stable when task-local extraction inserts earlier pages."""
        path = self._cache / "navigation-references.json"
        lock_path = self._cache / "navigation-references.lock"
        if path.is_symlink() or lock_path.is_symlink():
            raise WorkspaceError('The short structure-ID cache cannot be a symbolic link')
        with FileLock(lock_path, timeout=30):
            if path.is_symlink():
                raise WorkspaceError('The short structure-ID cache cannot be a symbolic link')
            identity = {
                "version": 1,
                "snapshot_id": self.source_snapshot_id,
                "index_snapshot_id": self.source_index.source_snapshot_id,
            }
            saved = {}
            if path.is_file():
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeError) as exc:
                    raise WorkspaceError('The short structure-ID cache is corrupted; existing references cannot be renumbered') from exc
                if not isinstance(saved, dict):
                    raise WorkspaceError('Invalid short structure-ID cache format')
            if any(saved.get(key) != value for key, value in identity.items()):
                saved = identity | {"files": {}}
            if not isinstance(saved.get("files"), dict):
                raise WorkspaceError('Invalid short structure-ID file mapping')
            before = json.dumps(saved, ensure_ascii=False, sort_keys=True)
            try:
                original = self.source_index.structure(relative)
            except SourceIndexError as exc:
                raise WorkspaceError(str(exc)) from exc
            native = {node["node_id"]: node["short_id"] for node in annotate_nodes(original)}
            entry = saved["files"].get(relative)
            if entry is None:
                entry = {"source_version": structure["source_version"], "nodes": dict(native)}
                saved["files"][relative] = entry
            if (
                not isinstance(entry, dict)
                or entry.get("source_version") != structure["source_version"]
                or not isinstance(entry.get("nodes"), dict)
            ):
                raise WorkspaceError('Short structure IDs do not match the source version')
            aliases = entry["nodes"]
            if (
                any(not isinstance(key, str) or not isinstance(value, str)
                    or not re.fullmatch(r"N\d{3,}", value) for key, value in aliases.items())
                or len(set(aliases.values())) != len(aliases)
                or any(aliases.get(key) != value for key, value in native.items())
            ):
                raise WorkspaceError('Invalid short structure-ID mapping; existing references cannot be renumbered')
            number = max((int(value[1:]) for value in aliases.values()), default=0)
            for node in structure["nodes"]:
                if node["node_id"] not in aliases:
                    number += 1
                    aliases[node["node_id"]] = f"N{number:03d}"
            if not path.is_file() or before != json.dumps(saved, ensure_ascii=False, sort_keys=True):
                self._atomic_json(path, saved)
            return structure | {"nodes": annotate_nodes(structure, aliases)}

    @staticmethod
    def _node(structure, node_id):
        try:
            return resolve_node(structure, node_id)
        except ValueError as exc:
            raise WorkspaceError(str(exc)) from exc

    def resolve_navigation_node(self, path, node_id):
        """Map a model-facing alias to the unchanged canonical source node ID."""
        return self._node(self._structure(path), node_id)["node_id"]

    def navigation_node_id(self, path, node_id):
        """Return the persistent short alias for a current canonical source node."""
        return self._node(self._structure(path), node_id)["short_id"]

    @staticmethod
    def _node_blocks(document, node):
        # OCR pages may be appended after native blocks, so a node can be
        # discontiguous while retaining all original evidence locations.
        if "block_indices" in node:
            return [document.blocks[index] for index in node["block_indices"]]
        bounds = node["range"]
        return document.blocks[bounds["start_block"] : bounds["end_block"]]

    def list(self, path="", offset=0, limit=50):
        _paging(offset, limit)
        requested = self._safe_path(path)
        if not requested.exists():
            raise WorkspaceError('The source path does not exist')
        prefix = requested.relative_to(self.source_root).as_posix()
        prefix = "" if prefix == "." else prefix
        files = [
            {
                "path": name,
                "size": item["size"],
                "source_version": item["version"],
                "format": item["format"],
                "file_id": item["file_id"],
                "status": item["status"],
            }
            for name, item in self._manifest.items()
            if not prefix or name == prefix or name.startswith(prefix + "/")
        ]
        end = min(offset + limit, len(files))
        return {
            "snapshot_id": self.source_snapshot_id,
            "files": files[offset:end],
            "offset": offset,
            "total": len(files),
            "next_offset": end if end < len(files) else None,
            "complete": end >= len(files),
        }

    def inspect(self, path, offset=0, limit=50, node_id=None, max_chars=600000):
        _paging(offset, limit, max_chars)
        index = self._structure(path)
        document_node = next((node for node in index["nodes"] if node["kind"] == "document"), None)
        selected = self._node(index, node_id) if node_id is not None else document_node
        nodes = [node for node in index["nodes"] if node["kind"] != "document"]
        if node_id is not None:
            nodes = [node for node in nodes if node["parent_id"] == selected["node_id"]]
        # Preserve the legacy JSON inspection contract. Short references are
        # returned by inspect_navigation; they do not alter canonical metadata
        # or consume this older endpoint's JSON preview budget.
        if selected is not None:
            selected = {key: value for key, value in selected.items() if key != "short_id"}
        nodes = [{key: value for key, value in node.items() if key != "short_id"} for node in nodes]
        end = min(offset + limit, len(nodes))
        base = {
            "path": path,
            "source_version": index["source_version"],
            "snapshot_id": self.source_snapshot_id,
            "metadata": index["metadata"] | {"format": index["format"], "reader_version": index["reader_version"]},
            "structure_status": index["structure_status"],
            "node": selected,
            "structure_total": len(nodes),
            "offset": offset,
            "next_offset": None,
            "structure_complete": offset >= len(nodes),
            "block_count": index["metadata"].get("block_count", 0),
            "complete": index["complete"],
            "warnings": index["warnings"],
            "evidence": [],
            "preview_truncated": False,
        }

        def append_node(result, node, position, compact):
            return result | {
                "structure": [*result["structure"], _inspect_node(node, compact)],
                "next_offset": position + 1 if position + 1 < len(nodes) else None,
                "structure_complete": position + 1 >= len(nodes),
            }

        def fits(result):
            return len(json.dumps(result, ensure_ascii=False)) <= max_chars

        # Reserve a complete first node and the pagination envelope before
        # emitting anything. A metadata-heavy node must never trap offset N in
        # an empty page whose next_offset is still N.
        for compact in range(3):
            result = _inspect_page(base, compact)
            if offset < end:
                result = append_node(result, nodes[offset], offset, compact)
            if fits(result):
                break
        else:
            raise WorkspaceError('Core structure-node identifiers exceed max_chars. Increase the outline-reading budget')
        for position in range(offset + 1, end):
            candidate = append_node(result, nodes[position], position, compact)
            if not fits(candidate):
                break
            result = candidate
        return result

    def inspect_navigation(self, path, offset=0, node_id=None, max_chars=600000, limit=None):
        """Return Markdown structure with paging over complete navigation nodes.

        The internal fields remain available for canonical registration; only
        ``text`` is intended for the model. Its complete envelope is budgeted.
        """
        _paging(offset, 1 if limit is None else limit, max_chars)
        index = self._structure(path)
        annotated = annotate_nodes(index)
        root = next((node for node in annotated if node["kind"] == "document"), None)
        selected = self._node(index, node_id) if node_id is not None else root
        nodes = [node for node in annotated if node["kind"] != "document"]
        if node_id is not None:
            nodes = [node for node in nodes if node["parent_id"] == selected["node_id"]]
        total = len(nodes)
        stop = total if limit is None else min(offset + limit, total)
        header = navigation_header(index, path, selected)

        def footer(end):
            return f'\nStructure continues; next position: offset={end}.\n' if end < total else ""

        if offset == 0 and node_id is None and stop == total:
            record = self._overlay_record(path)
            if record is None:
                try:
                    full = self.source_index.navigation(path)
                except SourceIndexError as exc:
                    raise WorkspaceError(str(exc)) from exc
            else:
                # OCR overlays have their own immutable structure generation.
                # Derive missing navigation from it without reopening the body.
                target = (self._cache / record["structure_ref"]).with_suffix(".md")
                full = render_navigation(index, path)
                if target.is_symlink():
                    raise WorkspaceError('The structure navigation file cannot be a symbolic link')
                if not target.is_file() or target.read_text(encoding="utf-8") != full:
                    self._atomic_text(target, full)
                full = target.read_text(encoding="utf-8")
        else:
            full = header + "".join(text for _, text in navigation_entries(index, nodes[offset:stop])) + footer(stop)

        if len(full) <= max_chars:
            visible, text, end = nodes[offset:stop], full, stop
        else:
            visible, parts, end = [], [], offset
            for group, text in navigation_entries(index, nodes[offset:stop]):
                candidate_end = end + len(group)
                candidate = header + "".join(parts) + text + footer(candidate_end)
                if len(candidate) > max_chars:
                    break
                visible.extend(group)
                parts.append(text)
                end = candidate_end
            if (offset < total and not visible) or len(header + footer(end)) > max_chars:
                raise WorkspaceError('A structure node and its navigation information exceed max_chars. Increase the structure-reading budget')
            text = header + "".join(parts) + footer(end)
        return {
            "path": path,
            "source_version": index["source_version"],
            "snapshot_id": self.source_snapshot_id,
            "metadata": index["metadata"] | {"format": index["format"], "reader_version": index["reader_version"]},
            "structure_status": index["structure_status"],
            "node": _inspect_node(selected),
            "structure": [_inspect_node(node) for node in visible],
            "structure_total": total,
            "offset": offset,
            "next_offset": end if end < total else None,
            "structure_complete": end >= total,
            "block_count": index["metadata"].get("block_count", 0),
            "complete": index["complete"],
            "warnings": index["warnings"],
            "evidence": [],
            "preview_truncated": False,
            "text": text,
        }

    def read(
        self,
        path,
        locator=None,
        offset=0,
        limit=None,
        max_chars=None,
        sheet=None,
        cell_range=None,
        page=None,
        slide=None,
        node_id=None,
        character_offset=0,
    ):
        _paging(offset, limit, max_chars)
        document = self._load(path)
        blocks = document.blocks
        character_range = None
        if node_id is not None:
            if any(value is not None for value in (locator, sheet, cell_range, page, slide)):
                raise WorkspaceError('node_id cannot be combined with locator, sheet, page numbers, or other range selectors')
            node = self._node(self._structure(path), node_id)
            blocks = self._node_blocks(document, node)
        if locator is not None:
            if not isinstance(locator, str) or len(locator) > 3500:
                raise WorkspaceError('Invalid locator')
            match = re.fullmatch(r"(.+)/characters (\d+):(\d+)", locator)
            if match:
                locator = match[1]
                character_range = (int(match[2]), int(match[3]))
            blocks = [
                block
                for block in blocks
                if block.locator == locator or block.locator.startswith(locator + "/")
            ]
            if not blocks:
                raise WorkspaceError('The location does not exist. Inspect or page through read results to obtain a valid location first')
        if sheet is not None:
            if document.metadata["format"] != "xlsx" or sheet not in {
                item["name"] for item in document.metadata.get("sheets", [])
            }:
                raise WorkspaceError('The Excel sheet does not exist')
            blocks = [block for block in blocks if block.metadata.get("sheet") == sheet]
        if cell_range is not None:
            if character_range:
                raise WorkspaceError('Cell ranges and character ranges cannot be specified together')
            if sheet is None:
                raise WorkspaceError('Reading an Excel cell range requires sheet')
            blocks = self._cell_blocks(blocks, cell_range)
        if page is not None:
            if (
                isinstance(page, bool)
                or not isinstance(page, int)
                or not 1 <= page <= document.metadata.get("page_count", 0)
            ):
                raise WorkspaceError('Page number out of range')
            blocks = [block for block in blocks if block.metadata.get("page") == page]
        if slide is not None:
            if (
                isinstance(slide, bool)
                or not isinstance(slide, int)
                or not 1 <= slide <= document.metadata.get("slide_count", 0)
            ):
                raise WorkspaceError('Slide number out of range')
            blocks = [block for block in blocks if block.metadata.get("slide") == slide]
        if character_range:
            if len(blocks) != 1 or not 0 <= character_range[0] < character_range[1] <= len(
                blocks[0].text
            ):
                raise WorkspaceError('Invalid evidence character range')
            evidence = [evidence_for_block(path, document, blocks[0], *character_range)]
        else:
            evidence = list(
                self._range_evidence(path, document, blocks, cell_range)
                if cell_range
                else _chunks(path, document, blocks)
            )
        response = self._results(evidence, offset, limit, max_chars, character_offset)
        response.update(
            path=path,
            source_version=document.source_version,
            snapshot_id=self.source_snapshot_id,
            source_complete=document.metadata.get("complete", True),
            warnings=document.warnings,
        )
        return response

    @staticmethod
    def _cell_blocks(blocks, cell_range):
        from openpyxl.utils.cell import range_boundaries

        if not isinstance(cell_range, str) or not re.fullmatch(
            r"[A-Za-z]{1,3}[1-9]\d*(?::[A-Za-z]{1,3}[1-9]\d*)?", cell_range
        ):
            raise WorkspaceError('cell_range must use A1 or A1:C20 notation')
        min_col, min_row, max_col, max_row = range_boundaries(cell_range)
        if (
            max_col < min_col
            or max_row < min_row
            or (max_col - min_col + 1) * (max_row - min_row + 1) > 500_000
        ):
            raise WorkspaceError('The cell range is invalid or too large')
        return [block for block in blocks if min_row <= block.metadata["row"] <= max_row]

    @staticmethod
    def _range_evidence(path, document, blocks, cell_range):
        from openpyxl.utils.cell import range_boundaries

        first, _, last, _ = range_boundaries(cell_range)
        for block in blocks:
            values = [cell["value"] for cell in block.metadata["cells"]]
            selected = values[first - 1 : last]
            if not selected or not any(selected):
                continue
            start = sum(len(value) + 3 for value in values[: first - 1])
            end = start + len(" | ".join(selected))
            yield evidence_for_block(path, document, block, start, end)

    @staticmethod
    def _results(evidence, offset, limit, max_chars, character_offset=0):
        """Page stable source ranges, using a character cursor only when needed.

        ``offset`` and ``total`` count source ranges independently of the budget.
        ``character_offset`` is relative to the range at ``offset`` (including
        an explicitly selected character or Excel cell range).
        """
        if (
            isinstance(character_offset, bool)
            or not isinstance(character_offset, int)
            or character_offset < 0
            or character_offset and (
                offset >= len(evidence) or character_offset >= len(evidence[offset].text)
            )
        ):
            raise WorkspaceError('character_offset must be within the current source range')
        selected, segments, used = [], [], 0
        end, next_character = offset, character_offset
        stop = len(evidence) if limit is None else min(offset + limit, len(evidence))
        while end < stop:
            item = evidence[end]
            available = len(item.text) - next_character
            if max_chars is not None:
                available = min(available, max_chars - used)
            if available <= 0:
                break
            selected.append(_slice_evidence(item, next_character, next_character + available).model_dump())
            segments.append({
                "offset": end,
                "character_offset": next_character,
                "character_end": next_character + available,
                "total_characters": len(item.text),
            })
            used += available
            next_character += available
            if next_character < len(item.text):
                break
            end += 1
            next_character = 0
        complete = end >= len(evidence)
        return {
            "evidence": selected,
            "read_segments": segments,
            "offset": offset,
            "character_offset": character_offset,
            "total": len(evidence),
            "next_offset": None if complete else end,
            "next_character_offset": None if complete else next_character,
            "complete": complete,
            "truncated": not complete,
            "characters": used,
        }

    def search(self, query, path="", offset=0, limit=8, max_chars=600000, character_offset=0):
        _paging(offset, limit, max_chars)
        if isinstance(query, list):
            query = " ".join(query)
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise WorkspaceError('Search queries must contain 1 to 4000 characters')
        requested = self._safe_path(path)
        if not requested.exists():
            raise WorkspaceError('The search path does not exist')
        prefix = requested.relative_to(self.source_root).as_posix()
        prefix = "" if prefix == "." else prefix
        documents, warnings = [], []
        for name in self._manifest:
            if prefix and name != prefix and not name.startswith(prefix + "/"):
                continue
            document = self._load(name)
            warnings.extend(f"{name}: {warning}" for warning in document.warnings)
            documents.append((name, document))
        if prefix not in self._search_indexes:
            candidates = [item for name, document in documents for item in _chunks(name, document)]
            tokens = [Counter(_terms(item.text)) for item in candidates]
            frequencies = Counter(term for counts in tokens for term in counts)
            self._search_indexes[prefix] = (candidates, tokens, frequencies)
        candidates, tokens, frequencies = self._search_indexes[prefix]
        query_terms = set(_terms(query))
        scores = []
        for item, counts in zip(candidates, tokens, strict=True):
            score = sum(
                (1 + math.log(counts[term]))
                * math.log(1 + len(candidates) / (1 + frequencies[term]))
                for term in query_terms
                if counts[term]
            )
            if score > 0:
                scores.append((score, item))
        ranked = [
            item for _, item in sorted(scores, key=lambda entry: (-entry[0], entry[1].evidence_id))
        ]
        response = self._results(ranked, offset, limit, max_chars, character_offset)
        response.update(
            snapshot_id=self.source_snapshot_id,
            warnings=warnings[:40],
            warnings_truncated=len(warnings) > 40,
            coverage_note='Search results indicate relevance only; they do not establish complete source coverage.',
        )
        return response

    def register_extraction(self, path, extraction, *, execution_id, image_digest):
        """Register output of a trusted isolated converter, never model claims."""
        document = self._load(path)
        if not isinstance(extraction, dict) or not isinstance(extraction.get("blocks"), list):
            raise WorkspaceError('The OCR result lacks structured blocks')
        blocks = extraction["blocks"]
        if any(not isinstance(block, dict) for block in blocks):
            raise WorkspaceError('OCR blocks must be structured objects')
        if (
            not isinstance(execution_id, str)
            or not execution_id
            or not isinstance(image_digest, str)
            or not image_digest
        ):
            raise WorkspaceError('OCR results must be bound to an execution ID and environment summary')
        if (
            len(blocks) > 100_000
            or sum(len(str(block.get("text", ""))) for block in blocks) > 8_000_000
        ):
            raise WorkspaceError('The OCR result exceeds reading capacity')
        parsed = SourceDocument.from_dict(document.model_dump())
        parsed.blocks = [
            block
            for block in parsed.blocks
            if (block.metadata.get("extraction") or {}).get("execution_id") != execution_id
        ]
        page_count = document.metadata.get("page_count", 1)
        office = document.metadata["format"] in {"docx", "xlsx", "pptx"}
        if office:
            page_count = extraction.get("page_count")
            if (
                extraction.get("operation") != "office_ocr"
                or isinstance(page_count, bool)
                or not isinstance(page_count, int)
                or not 1 <= page_count <= 2000
            ):
                raise WorkspaceError('Office OCR must provide the actual page count produced by isolated conversion')
            parsed.metadata["page_count"] = page_count
        received_pages, added = set(), 0
        for index, item in enumerate(blocks, 1):
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            raw_page = item.get("page") or (item.get("metadata") or {}).get("page")
            if raw_page is None:
                match = re.match(r"page[\s:]+(\d+)", str(item.get("locator", "")))
                raw_page = int(match[1]) if match else 1
            if (
                isinstance(raw_page, bool)
                or not isinstance(raw_page, int)
                or not 1 <= raw_page <= page_count
            ):
                raise WorkspaceError('The OCR page number does not belong to the original source')
            received_pages.add(raw_page)
            parsed.blocks.append(
                SourceBlock(
                    f"page {raw_page}/ocr {digest(execution_id + image_digest)}/block {index}",
                    text,
                    "paragraph",
                    {
                        "page": raw_page,
                        "extraction": {
                            "method": "ocr",
                            "operation": "office_ocr" if office else "ocr",
                            "execution_id": execution_id,
                            "image_digest": image_digest,
                            "source_version": document.source_version,
                        },
                    },
                )
            )
            added += 1
        if not added:
            raise WorkspaceError('OCR returned no readable text. The source remains pending confirmation')
        if (
            len(parsed.blocks) > 100_000
            or sum(len(block.text) for block in parsed.blocks) > 8_000_000
        ):
            raise WorkspaceError('Cumulative OCR results exceed reading capacity')
        # Recompute from the immutable native read and ALL retained extraction
        # runs. A partial Office run must not forget prior pages; replacing an
        # execution must not leave removed pages marked as successfully read.
        native = self.source_index.load(path)
        unread = set(range(1, page_count + 1) if office else
                     native.metadata.get("unread_pages", range(1, page_count + 1)))
        extracted_pages = {block.metadata["page"] for block in parsed.blocks
                           if block.metadata.get("extraction")}
        unread -= extracted_pages
        parsed.metadata.update(
            needs_ocr=bool(unread),
            unread_pages=sorted(unread),
            complete=not unread,
            block_count=len(parsed.blocks),
            characters=sum(len(block.text) for block in parsed.blocks),
        )
        if office:
            parsed.metadata.update(unread_visuals=bool(unread), needs_conversion=bool(unread))
        if not unread:
            parsed.warnings = [
                warning
                for warning in parsed.warnings
                if not _pending_ocr_warning(warning)
            ]
        warning = 'OCR text is machine-recognized. Check key fields and values against original pages or through a review.'
        if warning not in parsed.warnings:
            parsed.warnings.append(warning)
        self._commit_overlay(path, parsed)
        return {
            "path": path,
            "source_version": document.source_version,
            "added_blocks": added,
            "complete": not unread,
            "warnings": parsed.warnings,
        }

    def resolve_evidence(self, evidence):
        """Return the canonical source record or reject a forged/stale quote."""
        evidence = Evidence.model_validate(evidence)
        if (evidence.evidence_id.startswith("visual:") or evidence.metadata.get("source_kind") == "visual"
                or evidence.metadata.get("visual_extraction")):
            from folio.research.extraction_review import ExtractionReviewManager

            self._check_version(evidence.source_id)
            return ExtractionReviewManager(self.task_root, self).resolve_visual(evidence)
        document = self._load(evidence.source_id)
        if evidence.source_version != document.source_version:
            raise WorkspaceError('The evidence cites a different source version')
        locator = evidence.metadata.get("source_locator", evidence.locator)
        block = next((item for item in document.blocks if item.locator == locator), None)
        if block is None:
            raise WorkspaceError('The evidence location does not belong to this source')
        start = evidence.metadata.get("characters_start", 0)
        end = evidence.metadata.get("characters_end", len(block.text))
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or end > len(block.text)
        ):
            raise WorkspaceError('Invalid evidence character range')
        canonical = evidence_for_block(evidence.source_id, document, block, start, end)
        # A previously read exact slice may acquire a signed review later. Only
        # add that verified receipt; never forgive changed text or other metadata.
        if evidence.metadata.get("extraction_review") != canonical.metadata.get("extraction_review") and "extraction_review" in canonical.metadata:
            previous = canonical.model_copy(update={"metadata": {
                key: value for key, value in canonical.metadata.items() if key != "extraction_review"
            }})
            incoming = evidence.model_copy(update={"metadata": {
                key: value for key, value in evidence.metadata.items() if key != "extraction_review"
            }})
            if incoming == previous:
                from folio.research.extraction_review import reviewed_evidence_error

                if reviewed_evidence_error(canonical) is None and (
                    "extraction_review" not in evidence.metadata or reviewed_evidence_error(evidence) is None
                ):
                    return canonical
        if (evidence.evidence_id, evidence.locator, evidence.text, evidence.metadata) != (
            canonical.evidence_id,
            canonical.locator,
            canonical.text,
            canonical.metadata,
        ):
            raise WorkspaceError('The evidence identity, content, or source metadata does not match the original')
        return canonical
