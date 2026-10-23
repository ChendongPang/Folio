"""Enumerated source coverage, distinct from relevance-oriented search."""

from __future__ import annotations

import hmac
import json
import re
from pathlib import Path

from filelock import FileLock

from folio.research.artifacts import (
    MAX_RECORD_BYTES,
    ArtifactError,
    _canonical,
    _digest,
    _file_hash,
    _safe_path,
    _signing_key,
    _write_record,
)


def _prefix_matches(locator, prefix):
    if not prefix:
        return True
    # A page prefix must not accidentally select page 10, 11, etc.
    if re.fullmatch(r"(?:page|slide) [1-9]\d*", prefix):
        return locator == prefix or locator.startswith(prefix + "/")
    return locator.startswith(prefix)


def _scope_pages(document, node, prefix):
    metadata = document.metadata
    count = metadata.get("page_count", metadata.get("slide_count", 0))
    if node and node["kind"] != "document":
        info = node["metadata"]
        page = info.get("page", info.get("frame", info.get("slide")))
        if page is not None:
            return set(range(page, info.get("end_page", page) + 1))
        return None
    if prefix:
        match = re.match(r"(?:page|slide) ([1-9]\d*)(?:/|$)", prefix)
        return {int(match[1])} if match else None
    return set(range(1, count + 1)) if count else None


def _require_complete_scope(document, node, prefix, pages, verified):
    metadata = document.metadata
    if metadata.get("truncated"):
        raise ArtifactError('Sources still contain unread ranges; a complete coverage inventory cannot be created')
    format_name = metadata["format"]
    full = not prefix and (node is None or node["kind"] == "document")
    unread = set(metadata.get("unread_pages", [])) | set(metadata.get("unread_visual_pages", []))
    if format_name in {"pdf", "png", "jpg", "jpeg", "tif", "tiff", "webp"}:
        if metadata.get("needs_ocr") and not unread:
            unread = set(range(1, metadata.get("page_count", 1) + 1))
        remaining = unread - verified
        if (pages is None and remaining) or (pages is not None and pages & remaining):
            raise ArtifactError('The selected source still has unread or unverified content; a complete coverage inventory cannot be created')
        return
    if full and metadata.get("page_count") and set(range(1, metadata["page_count"] + 1)).issubset(verified):
        return
    if format_name == "xlsx" and node and node["kind"] == "sheet":
        incomplete = node["metadata"]["name"] in metadata.get("unread_sheets", [])
    elif format_name == "xlsx" and prefix.startswith("sheet '"):
        incomplete = any(prefix.startswith("sheet '" + name.replace("'", "''") + "'!") for name in metadata.get("unread_sheets", []))
    elif format_name == "pptx" and pages is not None:
        incomplete = bool(pages & set(metadata.get("unread_slides", [])))
    else:
        incomplete = bool(unread or metadata.get("unread_visuals") or metadata.get("needs_ocr") or metadata.get("complete") is False)
    if incomplete:
        raise ArtifactError('Sources still contain unread ranges; a complete coverage inventory cannot be created. Original Office sections must be explicitly mapped to converted pages')


class CoverageManager:
    """A complete parsed scope plus explicit, auditable per-item dispositions."""

    def __init__(self, task_root: Path, workspace):
        self.task_root = Path(task_root).resolve()
        self.source_root = Path(workspace.source_root).resolve()
        self.workspace = workspace
        self.root = self.task_root / ".research" / "coverage_records"
        self._source_documents = {}

    def _path(self, coverage_id: str) -> Path:
        import re

        if not re.fullmatch(r"[a-f0-9]{64}", coverage_id):
            raise ArtifactError('Invalid coverage inventory ID')
        return self.root / (coverage_id + ".json")

    def _load(self, coverage_id: str) -> dict:
        path = self._path(coverage_id)
        if any(parent.is_symlink() for parent in [path, *path.parents]):
            raise ArtifactError('Coverage inventories cannot use symbolic links')
        if not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
            raise ArtifactError('The coverage inventory is missing or exceeds the size limit')
        try:
            record = json.loads(path.read_bytes())
            signature = record.pop("signature", "")
            expected = hmac.new(_signing_key(), _canonical(record), "sha256").hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ArtifactError('Invalid coverage inventory signature')
            if record["coverage_id"] != coverage_id or _digest(record["scope"]) != coverage_id:
                raise ArtifactError('Invalid coverage inventory identity')
            source = _safe_path(self.source_root, record["scope"]["source_id"])
            if _file_hash(source) != record["scope"]["source_version"]:
                raise ArtifactError('The source version associated with the coverage inventory has changed')
            if record["scope"].get("extraction_reviews"):
                from folio.research.extraction_review import _read_review

                for review_path in record["scope"]["extraction_reviews"]:
                    _read_review(review_path)
            return record
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ArtifactError):
                raise
            raise ArtifactError('Invalid coverage inventory format') from exc

    def create(self, source_id: str, locator_prefix: str = "", *, node_id=None) -> dict:
        source = _safe_path(self.source_root, source_id)
        from folio.research.extraction_review import ExtractionReviewManager
        from folio.research.structure import build_structure

        # Raw task OCR never establishes completeness. Only original text plus
        # independently reviewed, signed page snapshots may enter enumeration.
        document, verified_pages, reviews = ExtractionReviewManager(
            self.task_root, self.workspace
        ).verified_document(source_id)
        node = None
        if node_id is not None:
            if locator_prefix:
                raise ArtifactError('node_id and locator_prefix cannot be specified together')
            node = self.workspace._node(build_structure(document), node_id)
            selected = self.workspace._node_blocks(document, node)
        else:
            selected = [block for block in document.blocks if _prefix_matches(block.locator, locator_prefix)]
        pages = _scope_pages(document, node, locator_prefix)
        _require_complete_scope(document, node, locator_prefix, pages, verified_pages)
        if reviews and pages is not None:
            from folio.research.extraction_review import _read_review

            reviews = [path for path in reviews if pages & {
                page["page"] for page in _read_review(path)["registration"]["pages"]
            }]
        empty_page_scope = (
            not locator_prefix
            or bool(re.fullmatch(r"page [1-9]\d*", locator_prefix))
        )
        if not selected and not (empty_page_scope and pages and pages.issubset(verified_pages)):
            raise ArtifactError('The selected range has no enumerable source entries')
        if len(selected) > 10_000:
            raise ArtifactError('The coverage inventory exceeds 10,000 entries. Split it into explicit section or sheet ranges')
        scope = {
            "source_id": source_id,
            "source_version": document.source_version,
            "locator_prefix": locator_prefix,
            "unit": "source_block",
            "items": [
                {
                    "item_id": _digest([block.locator, block.text]),
                    "locator": block.locator,
                    "text": block.text,
                }
                for block in selected
            ],
        }
        if reviews:
            scope["extraction_reviews"] = reviews
        if node_id is not None:
            scope["node_id"] = node_id
        if _file_hash(source) != document.source_version:
            raise ArtifactError('The source version changed during enumeration')
        coverage_id = _digest(scope)
        path = self._path(coverage_id)
        if path.exists():
            return self._load(coverage_id)
        record = {
            "version": 1,
            "coverage_id": coverage_id,
            "scope": scope,
            "dispositions": {},
        }
        _write_record(path, record)
        return record

    def record(
        self,
        coverage_id: str,
        item_id: str,
        status: str,
        *,
        references: list[dict] | None = None,
        reason: str = "",
    ) -> dict:
        if status not in {"covered", "pending", "not_applicable"}:
            raise ArtifactError('Invalid coverage entry status')
        references = references or []
        if status == "covered" and (
            not references
            or any(not ref.get("section_id") or not ref.get("block_id") for ref in references)
        ):
            raise ArtifactError('Covered entries must point to specific section body blocks')
        if status != "covered" and not reason.strip():
            raise ArtifactError('Pending or inapplicable entries must include a reason')
        path = self._path(coverage_id)
        with FileLock(str(path) + ".lock"):
            record = self._load(coverage_id)
            if item_id not in {item["item_id"] for item in record["scope"]["items"]}:
                raise ArtifactError('The coverage entry does not belong to this inventory')
            record["dispositions"][item_id] = {
                "status": status,
                "references": references,
                "reason": reason,
                "review_key": "",
            }
            _write_record(path, record)
        return record["dispositions"][item_id]

    def review_exclusion(
        self, coverage_id: str, item_id: str, *, review_key: str, reason: str
    ) -> None:
        """Only the independent reviewer can approve a declared exclusion."""
        if not review_key.strip() or not reason.strip():
            raise ArtifactError('An inapplicable entry requires an independent review record and reason')
        path = self._path(coverage_id)
        with FileLock(str(path) + ".lock"):
            record = self._load(coverage_id)
            item = record["dispositions"].get(item_id)
            if not item or item["status"] != "not_applicable":
                raise ArtifactError('Only entries already declared inapplicable can be reviewed for exclusion')
            item["review_key"] = review_key
            item["review_reason"] = reason
            _write_record(path, record)

    def covering_references(self, scope, item, states, *, references=None) -> list[dict]:
        """Return real body anchors only when their authentic spans cover an item.

        Source readers split long blocks into exact character slices. Coverage
        must join those intervals, including fragments cited by different body
        blocks, without treating one matching excerpt as the whole source item.
        """
        from folio.research.extraction_review import evidence_for_reviewed_block

        identity = (scope["source_id"], scope["source_version"])
        document_key = (*identity, tuple(scope.get("extraction_reviews", [])))
        if document_key not in self._source_documents:
            from folio.research.extraction_review import ExtractionReviewManager

            document, _, _ = ExtractionReviewManager(self.task_root, self.workspace).verified_document(
                scope["source_id"], review_paths=scope.get("extraction_reviews", [])
            )
            if document.source_version != scope["source_version"]:
                raise ArtifactError('The source version associated with the coverage inventory has changed')
            self._source_documents[document_key] = (
                document,
                {block.locator: block for block in document.blocks},
            )
        document, blocks = self._source_documents[document_key]
        original = blocks.get(item["locator"])
        if original is None or original.text != item["text"]:
            raise ArtifactError('The coverage entry does not match the original source location or content')
        allowed = (
            None
            if references is None
            else {(reference["section_id"], reference["block_id"]) for reference in references}
        )
        intervals = []
        for state in states:
            valid = {}
            for evidence in state.evidence:
                if (evidence.source_id, evidence.source_version) != identity:
                    continue
                if evidence.metadata.get("source_locator", evidence.locator) != original.locator:
                    continue
                start = evidence.metadata.get("characters_start", 0)
                end = evidence.metadata.get("characters_end", len(original.text))
                if (
                    isinstance(start, bool)
                    or isinstance(end, bool)
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or not 0 <= start < end <= len(original.text)
                ):
                    continue
                canonical = evidence_for_reviewed_block(scope["source_id"], document, original, start, end)
                actual_metadata, expected_metadata = evidence.metadata, canonical.metadata
                if original.metadata.get("extraction") or original.metadata.get("visual_extraction"):
                    from folio.research.artifacts import derived_evidence_error

                    if derived_evidence_error(evidence):
                        continue
                    # Re-review may issue another valid immutable receipt for
                    # exactly the same text. Both must validate independently.
                    actual_metadata = {key: value for key, value in actual_metadata.items() if key != "extraction_review"}
                    expected_metadata = {key: value for key, value in expected_metadata.items() if key != "extraction_review"}
                if (
                    evidence.evidence_id != canonical.evidence_id
                    or evidence.locator != canonical.locator
                    or evidence.text != canonical.text
                    or actual_metadata != expected_metadata
                ):
                    continue
                valid[evidence.evidence_id] = (start, end)
            for block in state.content.blocks:
                anchor = (state.section_id, block.block_id)
                if (
                    block.origin == "model_proposal"
                    or allowed is not None
                    and anchor not in allowed
                ):
                    continue
                intervals.extend(
                    (*valid[eid], anchor) for eid in block.evidence_ids if eid in valid
                )
        covered, anchors = 0, []
        for start, end, anchor in sorted(intervals):
            if start > covered:
                return []
            if end > covered:
                covered = end
                if anchor not in anchors:
                    anchors.append(anchor)
        if covered != len(original.text):
            return []
        return [
            {"section_id": section_id, "block_id": block_id} for section_id, block_id in anchors
        ]

    def report(self, coverage_id: str, states=()) -> dict:
        record = self._load(coverage_id)
        scope, missing = record["scope"], []
        states = tuple(states)
        for item in scope["items"]:
            disposition = record["dispositions"].get(item["item_id"])
            failure = 'No handling location has been registered'
            if disposition:
                if disposition["status"] == "not_applicable":
                    if disposition.get("review_key"):
                        continue
                    failure = 'The inapplicability declaration has not been independently reviewed'
                elif disposition["status"] == "pending":
                    failure = disposition["reason"]
                else:
                    if self.covering_references(
                        scope, item, states, references=disposition["references"]
                    ):
                        continue
                    failure = 'The body location is missing, has changed, or does not fully cite this original evidence'
            missing.append(
                {"item_id": item["item_id"], "locator": item["locator"], "reason": failure}
            )
        return {
            "coverage_id": coverage_id,
            "source_id": scope["source_id"],
            "source_version": scope["source_version"],
            "locator_prefix": scope["locator_prefix"],
            "node_id": scope.get("node_id"),
            "total": len(scope["items"]),
            "covered": len(scope["items"]) - len(missing),
            "complete": not missing,
            "missing": missing,
        }
