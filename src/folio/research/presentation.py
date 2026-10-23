"""Small model-facing references over the unchanged canonical source ledger."""

from __future__ import annotations

import json
import re
from copy import deepcopy

from folio.research.planning_progress import source_name
from folio.research.workspace import WorkspaceError, WorkspaceManager


class SourcePresentation:
    """Aliases are scoped to a workspace snapshot, never accepted as evidence alone."""

    def __init__(self, workspace, existing_evidence=()):
        self.workspace = workspace
        self.files = {path: f"F{i}" for i, path in enumerate(sorted(workspace.source_index.files), 1)}
        self.evidence = {}
        self.ranges = {}
        self.cursors = {}
        self.legacy_cursors = set()
        self.path = workspace.task_root / ".workspace" / "model-references.json"
        if self.path.is_file() and not self.path.is_symlink():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("snapshot_id") == workspace.source_snapshot_id:
                self.evidence = saved.get("evidence", {})
                self.ranges = saved.get("ranges", {})
                self.cursors = saved.get("cursors", {})
                self.legacy_cursors = set(saved.get(
                    "legacy_cursors", self.cursors if saved.get("pagination_version", 1) < 2 else ()))
        for item in existing_evidence:
            key = item if isinstance(item, str) else (
                item.get("evidence_id") if isinstance(item, dict) else item.evidence_id
            )
            self.evidence_id(key)

    def save(self):
        WorkspaceManager._atomic_json(self.path, {
            "snapshot_id": self.workspace.source_snapshot_id,
            "files": self.files, "evidence": self.evidence,
            "ranges": self.ranges, "cursors": self.cursors,
            "pagination_version": 2, "legacy_cursors": sorted(self.legacy_cursors),
        })

    def file_id(self, path):
        return self.files.get(path, path)

    def resolve_file(self, value):
        if value in self.files:
            return value
        inverse = {alias: path for path, alias in self.files.items()}
        if value in inverse:
            return inverse[value]
        if isinstance(value, str) and re.fullmatch(r"F\d+", value):
            raise WorkspaceError('The file ID does not exist. Check the file catalog')
        # The catalog shows the upload's original name, without its storage UUID.
        # Only resolve an exact, unique display name; never guess between uploads
        # or turn an invalid/traversing path into a valid source by taking basename.
        matches = [path for path in self.files if source_name(path) == value]
        if len(matches) == 1:
            return matches[0]
        if matches:
            aliases = "、".join(self.files[path] for path in matches)
            raise WorkspaceError(f'Filename “{value}” matches multiple sources. Use a file ID: {aliases}')
        return value

    def evidence_id(self, key):
        if key not in self.evidence:
            self.evidence[key] = f"E{len(self.evidence) + 1}"
        return self.evidence[key]

    def resolve_evidence_id(self, key):
        return next((original for original, short in self.evidence.items() if short == key), key)

    def range_id(self, key):
        if key not in self.ranges:
            self.ranges[key] = f"R{len(self.ranges) + 1}"
        return self.ranges[key]

    def resolve_range_id(self, key):
        return next((original for original, short in self.ranges.items() if short == key), key)

    def node_id(self, path, key):
        resolve = getattr(self.workspace, "navigation_node_id", None)
        if resolve is not None:
            return resolve(path, key)
        structure = self.workspace._structure(path)
        for i, node in enumerate(structure["nodes"]):
            if node["node_id"] == key:
                return f"N{i:03}"
        return key

    def _canonical_arguments(self, arguments):
        args = deepcopy(arguments)
        for key in ("file", "path"):
            if key in args:
                args[key] = self.resolve_file(args[key])
        path = args.get("path", args.get("file"))
        for key in ("node", "node_id"):
            value = args.get(key)
            if path in self.files and isinstance(value, str) and re.fullmatch(r"N\d+", value):
                args[key] = self.workspace.resolve_navigation_node(path, value)
        for short, canonical in (("file", "path"), ("node", "node_id")):
            if short in args:
                if canonical in args and args[short] != args[canonical]:
                    raise WorkspaceError(f'{short} and {canonical} cannot specify different values simultaneously')
                args[canonical] = args.pop(short)
        if "evidence_id" in args:
            args["evidence_id"] = self.resolve_evidence_id(args["evidence_id"])
        return args

    def resolve_arguments(self, arguments, *, tool=None):
        args = deepcopy(arguments)
        if "cursor" not in args:
            return self._canonical_arguments(args)
        cursor = args.pop("cursor")
        if cursor not in self.cursors:
            raise WorkspaceError("Unknown continuation ID. Use the C ID returned by the tool as cursor; R IDs identify read ranges only.")
        following = self._canonical_arguments(self.cursors[cursor])
        # A cursor already identifies the source and position. Repeating a matching
        # filename/node is harmless; conflicting selectors must never redirect it.
        if "path" in following and not {"file", "path"} & args.keys():
            args["path"] = following["path"]
        args = self._canonical_arguments(args)
        for key, value in args.items():
            if key in {"limit", "max_chars"}:
                continue
            expected = following.get(key, {"scope": "sources", "character_offset": 0}.get(key))
            if value != expected:
                raise WorkspaceError(f"cursor {cursor} and {key} identify different files or reading positions. "
                                     "Pass only cursor to continue its range, or omit cursor to select a different range.")
        resolved = following | args
        if cursor in self.legacy_cursors and tool is not None:
            if (tool == "fs.read" and resolved.get("scope", "sources") == "sources"
                    and not {"execution_id", "artifact_id"} & resolved.keys()):
                # Old persisted cursors counted fixed 2,000-character slices.
                # Translate them once; new readers count source blocks instead.
                selector = {key: value for key, value in following.items()
                            if key not in {"offset", "character_offset", "limit", "max_chars", "scope"}}
                full = self.workspace.read(**selector)
                old_offset = following.get("offset", 0)
                position = (full["total"], 0)
                seen = 0
                for index, item in enumerate(full["evidence"]):
                    for start in range(0, len(item["text"]), 2000):
                        if (not item["text"][start:start + 2000].strip()
                                and not selector.get("cell_range")
                                and "/characters " not in selector.get("locator", "")):
                            continue
                        if seen == old_offset:
                            position = (index, start)
                            break
                        seen += 1
                    else:
                        continue
                    break
                resolved["offset"] = following["offset"] = position[0]
                if position[1]:
                    resolved["character_offset"] = following["character_offset"] = position[1]
                else:
                    resolved.pop("character_offset", None)
                    following.pop("character_offset", None)
                self.cursors[cursor] = following
            elif tool == "fs.search":
                raise WorkspaceError("This search cursor predates source-block pagination. Repeat the search to obtain a current location.")
            self.legacy_cursors.discard(cursor)
        return resolved

    def continuation_command(self, tool, args, offset, character_offset=0):
        """An executable next read survives eviction of the original tool result."""
        following = {key: value for key, value in args.items()
                     if key not in {"limit", "max_chars", "cursor", "character_offset"}}
        following["offset"] = offset
        if character_offset:
            following["character_offset"] = character_offset
        matches = [key for key, value in self.cursors.items() if value == following]
        cursor = next((key for key in matches if key not in self.legacy_cursors),
                      matches[0] if matches else None)
        if cursor is not None:
            return f"{tool.replace('.', '_')}(cursor={cursor})"
        path = following.pop("path", None)
        if path is not None:
            following["file" if path in self.files and tool != "fs.list" else "path"] = self.file_id(path)
        node = following.pop("node_id", None)
        if node is not None:
            following["node"] = self.node_id(path, node) if path in self.files else node
        values = ", ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in following.items())
        return f"{tool.replace('.', '_')}({values})"

    def resolve_conclusion(self, value):
        result = deepcopy(value)
        for item in result.get("requirements", []):
            item["evidence_ids"] = [self.resolve_evidence_id(key) for key in item.get("evidence_ids", [])]
        for item in result.get("selected_ranges", []):
            item["range_id"] = self.resolve_range_id(item["range_id"])
        for field in ("excluded_files", "unread_assessments"):
            for item in result.get(field, []):
                if "file" in item and "path" not in item:
                    item["path"] = item.pop("file")
                if "path" in item:
                    item["path"] = self.resolve_file(item["path"])
        return result

    def catalog_text(self, result):
        files = result.get("files", []) if isinstance(result, dict) else result
        lines = ["Source catalog:"]
        for item in files:
            path = item.get("path", item.get("name", ""))
            lines.append(f"- {self.file_id(path)}：{source_name(path)}（{item.get('format', '')}）")
        if isinstance(result, dict) and result.get("next_offset") is not None:
            lines.append(f"Catalog continues; call fs_list(offset={result['next_offset']}).")
        return "\n".join(lines)

    def _continuation(self, tool, args, result):
        offset = result.get("next_offset")
        if offset is None:
            return "End of the selected range."
        following = {key: value for key, value in args.items()
                     if key not in {"limit", "max_chars", "cursor", "character_offset"}}
        following["offset"] = offset
        character_offset = result.get("next_character_offset") or 0
        if character_offset:
            following["character_offset"] = character_offset
        key = next((key for key, value in self.cursors.items()
                    if value == following and key not in self.legacy_cursors), None)
        if key is None:
            key = f"C{len(self.cursors) + 1}"
            self.cursors[key] = following
        remaining = ""
        if tool == "fs.read" and args.get("scope", "sources") == "sources":
            total = result.get("total")
            remaining = f" Remaining range: source block {offset + 1}"
            if isinstance(total, int):
                remaining += f" through {total}"
            if character_offset:
                remaining += f", starting at character {character_offset} of that block"
            remaining += "."
        reason = ("The selected content exceeds the available request capacity. "
                  if result.get("pagination_reason") == "context_capacity" else "")
        return reason + f"More content remains; continue with {tool.replace('.', '_')}(cursor={key})." + remaining

    def result_text(self, tool, args, result):
        if not isinstance(result, dict):
            return str(result)
        if "error" in result:
            return "Execution failed: " + str(result["error"])
        if tool == "fs.list":
            return self.catalog_text(result)
        path = args.get("path", result.get("path", ""))
        if tool == "fs.inspect" and "text" in result:
            return f"Source ID: {self.file_id(path)}\n" + result["text"]
        if tool == "fs.read" and args.get("scope", "sources") != "sources":
            text = result.get("text", "")
            return str(text) + "\n\n" + self._continuation(tool, args, result)
        if tool in {"fs.read", "fs.search"} and "evidence" in result:
            label = self.file_id(path) if path else "Selected sources"
            if path in self.files and args.get("node_id"):
                label += " / " + self.node_id(path, args["node_id"])
            elif args.get("sheet"):
                label += " / " + args["sheet"] + " " + args.get("cell_range", "")
            elif args.get("page") or args.get("slide"):
                label += " / page " + str(args.get("page", args.get("slide")))
            lines = [f"Source: {label}"]
            if tool == "fs.search":
                lines.append("These are search locations. Read the original text before citing it as research evidence.")
            spans = []
            for i, item in enumerate(result["evidence"], 1):
                if tool == "fs.read":
                    key = self.evidence_id(item["evidence_id"])
                    prefix = f"[{key}] {item.get('locator', '')}\n"
                    start = len("\n\n".join(lines)) + 2 + len(prefix)
                    spans.append({"evidence_id": item["evidence_id"], "start": start,
                                  "end": start + len(item["text"])})
                    lines.append(prefix + item["text"])
                else:
                    source = self.file_id(item.get("source_id", path))
                    lines.append(f"Candidate {i}: {source}, locator {item.get('locator', '')}\n{item['text']}")
            if result.get("source_complete") is False or result.get("warnings"):
                lines.append("Parsing status: " + "；".join(result.get("warnings") or ["Some content has not been extracted"]))
            lines.append(self._continuation(tool, args, result))
            if tool == "fs.read":
                result["model_evidence_spans"] = spans
            return "\n\n".join(lines)
        # Other business tools retain exact execution/artifact identifiers.
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    def progress_text(self, ranges):
        lines = []
        for item in ranges:
            selector = item.get("selector", {})
            path = item.get("path", selector.get("path", ""))
            label = self.file_id(path)
            if selector.get("node_id"):
                label += "/" + self.node_id(path, selector["node_id"])
            elif selector.get("page") or selector.get("slide"):
                label += " / page " + str(selector.get("page", selector.get("slide")))
            elif selector.get("sheet"):
                label += " / " + selector["sheet"] + " " + selector.get("cell_range", "")
            complete = item.get("delivered_complete", False)
            count = len(item.get("delivered_evidence_ids", []))
            lines.append(f"{label}: {count} passages supplied; "
                         + ("The complete range has been supplied" if complete else "The range has not been fully supplied"))
            if not item.get("complete", False) and isinstance(item.get("total"), int):
                offset = item.get("next_offset")
                character_offset = item.get("next_character_offset") or 0
                if offset is None:
                    offset = 0
                    for start, end in sorted(item.get("pages", [])):
                        if start > offset:
                            break
                        offset = max(offset, end)
                if offset < item["total"]:
                    lines[-1] += "; continue: " + self.continuation_command(
                        "fs.read", selector, offset, character_offset)
            elif not complete and item.get("complete", False):
                lines[-1] += "; Text was extracted but not fully sent. Retrieve unseen content from reading history."
        return "\n".join(lines) or "No body text has been supplied to the model yet."
