"""Allowlisted cosmetic edits to rendered business content, never to document text."""

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

from docx import Document
from docx.oxml.ns import qn
from lxml import etree
from pydantic import Field, model_validator

from folio.document.presentation import prose_paragraphs
from folio.document.reader import heading_level, table_rows, visible_text, xpath
from folio.document.style_layout import document_style_layout, protected_style_properties
from folio.document.typography import prop, resolved_format
from folio.models import Model, digest

FORMAT_POLICY = 2


class LayoutPatch(Model):
    target_id: str = Field(min_length=1, max_length=80)
    alignment: Literal["left", "center", "right"] | None = None
    font_delta_pt: Literal[-1, 0, 1] | None = None
    space_before_pt: Literal[0, 3, 6, 9, 12] | None = None
    space_after_pt: Literal[0, 3, 6, 9, 12] | None = None
    keep_with_next: bool | None = None
    keep_together: bool | None = None
    repeat_table_header: Literal[True] | None = None
    prevent_row_split: Literal[True] | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not any(
            value is not None for key, value in self.model_dump().items() if key != "target_id"
        ):
            raise ValueError("A layout patch needs at least one permitted change")
        return self


class FormattingResult(Model):
    done: bool
    patches: list[LayoutPatch] = Field(default_factory=list, max_length=32)
    summary: str = Field(default="", max_length=240)


PARAGRAPH_KEYS = {
    "alignment",
    "font_delta_pt",
    "space_before_pt",
    "space_after_pt",
    "keep_with_next",
    "keep_together",
}
TABLE_KEYS = {"repeat_table_header", "prevent_row_split"}
LIST_TEXT = re.compile(r"^\s*(?:\d+[.、)]|[（(]\d+[）)]|[一二三四五六七八九十]+[、.]|[•●▪])")


@dataclass
class LayoutTarget:
    target_id: str
    section_id: str
    kind: str
    element: object
    path: str
    baseline: dict
    sizes: list
    header: bool = False
    permitted: set[str] | None = None


def _layout(document, element, kind):
    def enabled(nodes):
        return bool(nodes and nodes[0].get(qn("w:val")) not in {"0", "false", "off"})

    if kind == "table":
        rows = xpath(element, "./w:tr")
        return {
            "rows": len(rows),
            "columns": len(xpath(element, "./w:tblGrid/w:gridCol")),
            "repeat_table_header": bool(rows) and enabled(xpath(rows[0], "./w:trPr/w:tblHeader")),
            "prevent_row_split": bool(rows)
            and all(enabled(xpath(row, "./w:trPr/w:cantSplit")) for row in rows),
        }
    paragraph, run = resolved_format(document, element)
    sizes = xpath(run, "./w:sz/@w:val")
    result = {"font_size_pt": int(sizes[0]) / 2 if sizes else None}
    alignment = xpath(paragraph, "./w:jc/@w:val")
    result["alignment"] = alignment[0] if alignment else "left"
    for name in ("before", "after"):
        spacing = xpath(paragraph, f"./w:spacing/@w:{name}")
        result[f"space_{name}_pt"] = int(spacing[0]) / 20 if spacing else 0
    for key, tag in (("keep_with_next", "keepNext"), ("keep_together", "keepLines")):
        nodes = xpath(paragraph, f"./w:{tag}")
        result[key] = enabled(nodes)
    return result


def _eligible_paragraph(element, document):
    return bool(visible_text(element).strip()) and not (
        heading_level(element, document)
        or any(value != "0" for value in xpath(element, "./w:pPr/w:numPr/w:numId/@w:val"))
        or xpath(
            element, ".//w:drawing | .//w:pict | .//w:fldChar | .//w:instrText | .//w:txbxContent"
        )
        or LIST_TEXT.match(visible_text(element))
    )


def collect_targets(document, plan, states):
    """Only rendered text matching stored business blocks inside their section bookmarks."""
    sample_layout = document_style_layout(document)
    definitions = {section.section_id: section for section in plan.sections}
    originals = {}
    for state in states:
        if state.section_id not in definitions:
            continue
        prose, tables = Counter(), []
        headers = definitions[state.section_id].expected_table_headers
        for block in state.content.blocks:
            if block.kind == "paragraph":
                prose.update(prose_paragraphs(block.text))
            elif block.kind == "table":
                rows = [["\n".join(prose_paragraphs(cell)) for cell in row] for row in block.rows]
                forms = {tuple(tuple(row) for row in rows)}
                if headers and rows and rows[0] != headers and len(rows[0]) == len(headers):
                    forms.add(tuple(tuple(row) for row in [list(headers), *rows]))
                tables.append(forms)
        originals[state.section_id] = (prose, tables)
    targets = []
    current = None

    def add(element, kind, header=False):
        path = document.element.getroottree().getpath(element)
        baseline = _layout(document, element, kind)
        runs = xpath(element, "./w:r[w:t]") if kind != "table" else []
        sizes = []
        for run in runs:
            values = {}
            for name in ("sz", "szCs"):
                items = xpath(run, f"./w:rPr/w:{name}/@w:val")
                values[name] = items[0] if items else None
            sizes.append(values)
        permitted = None
        if plan.template:
            # The uploaded template owns fonts, alignment and spacing. Only add
            # missing pagination hints; never override an explicit true/false.
            if kind == "table":
                permitted = set()
                rows = xpath(element, "./w:tr")
                if header and rows and not xpath(rows[0], "./w:trPr/w:tblHeader"):
                    permitted.add("repeat_table_header")
                if rows and not any(xpath(row, "./w:trPr/w:cantSplit") for row in rows):
                    permitted.add("prevent_row_split")
            elif kind == "cell":
                permitted = set()
            else:
                properties, _ = resolved_format(document, element)
                permitted = {
                    key
                    for key, tag in (("keep_with_next", "keepNext"), ("keep_together", "keepLines"))
                    if properties.find(qn("w:" + tag)) is None
                }
        target = LayoutTarget(
            "fmt_" + digest(current + path),
            current,
            kind,
            element,
            path,
            baseline,
            sizes,
            header,
            permitted,
        )
        protected = protected_style_properties(document, element, kind, layout=sample_layout)
        if protected:
            target.permitted = set(allowed_properties(target)) - protected
        targets.append(target)

    for element in document.element.body:
        anchors = xpath(element, "./w:bookmarkStart/@w:name")
        section = next((name for name in anchors if name in definitions), None)
        if section:
            current = section
            continue  # Confirmed headings and their numbering are never targets.
        if current not in originals:
            continue
        prose, tables = originals[current]
        if element.tag == qn("w:p"):
            text = visible_text(element)
            if prose[text] > 0:
                prose[text] -= 1
                if _eligible_paragraph(element, document):
                    add(element, "paragraph")
        elif element.tag == qn("w:tbl"):
            rows = table_rows(element)
            form = tuple(tuple(row) for row in rows)
            match = next((index for index, options in enumerate(tables) if form in options), None)
            if match is None:
                continue
            tables.pop(
                match
            )  # One stored block owns one rendered table, including header variants.
            headers = definitions[current].expected_table_headers
            add(element, "table", bool(headers and rows[0] == headers))
            for paragraph in xpath(element, ".//w:tc/w:p"):
                if _eligible_paragraph(paragraph, document):
                    add(paragraph, "cell")
    return targets


def allowed_properties(target):
    if target.permitted is not None:
        return sorted(target.permitted)
    if target.kind == "table":
        return ["prevent_row_split"] + (["repeat_table_header"] if target.header else [])
    keys = sorted(PARAGRAPH_KEYS)
    size = target.baseline["font_size_pt"]
    explicit = {
        int(value) / 2 for item in target.sizes for value in item.values() if value is not None
    }
    if (
        size is None
        or not 9 <= size <= 17
        or len(explicit) > 1
        or any(not 9 <= value <= 17 for value in explicit)
        or xpath(target.element, ".//w:rStyle | .//w:vertAlign")
    ):
        keys.remove("font_delta_pt")
    return keys


def target_view(document, target):
    result = {
        "target_id": target.target_id,
        "section_id": target.section_id,
        "kind": target.kind,
        "allowed_properties": allowed_properties(target),
        "baseline_layout": target.baseline,
        "current_layout": _layout(document, target.element, target.kind),
    }
    if target.kind != "table":
        result["text"] = visible_text(target.element)
    return result


def _normalize(element, kind, allowed):
    if kind == "table":
        for key, tag in (("prevent_row_split", "cantSplit"), ("repeat_table_header", "tblHeader")):
            if key in allowed:
                for node in xpath(element, "./w:tr/w:trPr/w:" + tag):
                    node.getparent().remove(node)
        for node in xpath(element, "./w:tr/w:trPr"):
            if not len(node) and not node.attrib:
                node.getparent().remove(node)
        return
    for key, tag in (
        ("alignment", "jc"),
        ("keep_with_next", "keepNext"),
        ("keep_together", "keepLines"),
    ):
        if key in allowed:
            for node in xpath(element, "./w:pPr/w:" + tag):
                node.getparent().remove(node)
    for spacing in xpath(element, "./w:pPr/w:spacing"):
        for name in ("before", "after"):
            if "space_" + name + "_pt" in allowed:
                spacing.attrib.pop(qn("w:" + name), None)
        if not len(spacing) and not spacing.attrib:
            spacing.getparent().remove(spacing)
    if "font_delta_pt" in allowed:
        for node in xpath(element, "./w:r[w:t]/w:rPr/w:sz | ./w:r[w:t]/w:rPr/w:szCs"):
            node.getparent().remove(node)
    for node in xpath(element, "./w:pPr | ./w:r/w:rPr"):
        if not len(node) and not node.attrib:
            node.getparent().remove(node)


def protected_signature(document, targets):
    """Hash all text, typography, cover, headings, colors, numbering and other XML."""
    root = deepcopy(document.element)
    for target in targets:
        copied = xpath(root, target.path)
        if len(copied) != 1:
            raise ValueError("Layout target no longer resolves")
        _normalize(copied[0], target.kind, allowed_properties(target))
    data = etree.tostring(root, method="c14n") + etree.tostring(
        document.styles.element, method="c14n"
    )
    return hashlib.sha256(data).hexdigest()


def apply_patches(document, targets, patches):
    by_id = {target.target_id: target for target in targets}
    # Validate the entire response before making any edit, including duplicate targets.
    seen = set()
    for patch in patches:
        if patch.target_id not in by_id or patch.target_id in seen:
            raise ValueError("Formatting patch references an unknown or duplicate target")
        seen.add(patch.target_id)
        used = set(patch.model_dump(exclude_none=True)) - {"target_id"}
        if not used <= set(allowed_properties(by_id[patch.target_id])):
            raise ValueError("Formatting property is not allowed for this target")
    before = {
        target.target_id: _layout(document, target.element, target.kind) for target in targets
    }
    signature = protected_signature(document, targets)
    for patch in patches:
        target = by_id[patch.target_id]
        values = patch.model_dump(exclude_none=True, exclude={"target_id"})
        if target.kind == "table":
            rows = xpath(target.element, "./w:tr")
            if values.get("repeat_table_header"):
                properties = prop(rows[0], "trPr")
                rows[0].remove(properties)
                rows[0].insert(0, properties)
                prop(properties, "tblHeader", val="true")
            if values.get("prevent_row_split"):
                for row in rows:
                    properties = prop(row, "trPr")
                    row.remove(properties)
                    row.insert(0, properties)
                    prop(properties, "cantSplit", val="true")
            continue
        ppr = prop(target.element, "pPr")
        # OOXML property containers precede content; no runs or text are rebuilt.
        target.element.remove(ppr)
        target.element.insert(0, ppr)
        if "alignment" in values:
            prop(ppr, "jc", val=values["alignment"])
        for name in ("before", "after"):
            if f"space_{name}_pt" in values:
                prop(ppr, "spacing", **{name: values[f"space_{name}_pt"] * 20})
        for name, tag in (("keep_with_next", "keepNext"), ("keep_together", "keepLines")):
            if name in values:
                prop(ppr, tag, val="true" if values[name] else "false")
        if "font_delta_pt" in values:
            delta = values["font_delta_pt"]
            runs = xpath(target.element, "./w:r[w:t]")
            for run, original in zip(runs, target.sizes, strict=True):
                rpr = prop(run, "rPr")
                run.remove(rpr)
                run.insert(0, rpr)
                for name in ("sz", "szCs"):
                    if delta == 0 and original[name] is None:
                        for node in xpath(rpr, "./w:" + name):
                            rpr.remove(node)
                    else:
                        base = (
                            int(original[name])
                            if original[name] is not None
                            else int(target.baseline["font_size_pt"] * 2)
                        )
                        prop(rpr, name, val=base + delta * 2)
    if protected_signature(document, targets) != signature:
        raise ValueError("Formatting changed protected document content or typography")
    return any(
        _layout(document, target.element, target.kind) != before[target.target_id]
        for target in targets
    )


def save_document(document, path):
    temporary = Path(str(path) + ".formatting.docx")
    try:
        document.save(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def document_signature(source):
    """Bind replay to the entire DOCX package, including style and image bytes.

    ZIP timestamps/compression are irrelevant. Unlike protected_signature, this
    signature also binds all allowed layout properties to their exact baseline.
    """
    digest = hashlib.sha256()
    with ZipFile(source) as archive:
        for name in sorted(archive.namelist()):
            content = archive.read(name)
            if name.endswith((".xml", ".rels")):
                content = etree.tostring(etree.fromstring(content), method="c14n")
            digest.update(name.encode("utf-8") + b"\0" + content + b"\0")
    return digest.hexdigest()


def replay_page_review(path, plan, states, manifest_path):
    """Replay only the exact reviewed patch sequence; this is not a new visual check."""
    result = {"status": "not_available", "changed": False, "visual_checked": False}
    if not Path(manifest_path).is_file():
        return result
    report = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if report.get("status") != "passed" or not report.get("changed"):
        return result | {"status": "skipped", "reason": "no_approved_changes"}
    if report.get("policy") != 1 or not report.get("source_document_signature"):
        return result | {"status": "different_source", "reason": "missing_replay_contract"}
    if document_signature(path) != report["source_document_signature"]:
        return result | {"status": "different_source"}
    document = Document(path)
    targets = collect_targets(document, plan, states)
    changed = False
    for round_result in report.get("rounds", []):
        if not round_result.get("applied"):
            continue
        patches = [LayoutPatch.model_validate(item) for item in round_result.get("patches", [])]
        if any(patch.font_delta_pt is not None for patch in patches):
            raise ValueError("Page review cannot replay changes to protected font sizes")
        changed = apply_patches(document, targets, patches) or changed
    serialized = BytesIO()
    document.save(serialized)
    serialized.seek(0)
    if not changed or document_signature(serialized) != report.get("target_document_signature"):
        return result | {"status": "different_source", "reason": "replay_result_mismatch"}
    save_document(document, path)
    return result | {"status": "replayed", "changed": True, "reason": "saved_patches_only"}


def baseline_snapshot(targets):
    return {
        target.target_id: {"layout": deepcopy(target.baseline), "sizes": deepcopy(target.sizes)}
        for target in targets
    }


def _restore_baselines(targets, saved):
    """Relative font operations always refer to the initial document layout, including replay."""
    if not isinstance(saved, dict) or set(saved) != {target.target_id for target in targets}:
        raise ValueError("Formatting manifest lacks matching immutable target baselines")
    for target in targets:
        original = saved[target.target_id]
        layout, sizes = original.get("layout"), original.get("sizes")
        if not isinstance(layout, dict) or set(layout) != set(target.baseline):
            raise ValueError("Invalid formatting baseline layout")
        if not isinstance(sizes, list) or len(sizes) != len(target.sizes):
            raise ValueError("Invalid formatting baseline run count")
        for item in sizes:
            if not isinstance(item, dict) or set(item) != {"sz", "szCs"}:
                raise ValueError("Invalid formatting baseline run sizes")
            if any(
                value is not None and not (isinstance(value, str) and value.isdigit())
                for value in item.values()
            ):
                raise ValueError("Invalid formatting baseline font size")
        size = layout.get("font_size_pt")
        if size is not None and (type(size) not in {int, float} or not 0 < size < 1000):
            raise ValueError("Invalid formatting baseline font size")
        target.baseline, target.sizes = deepcopy(layout), deepcopy(sizes)


def replay_formatting(path, plan, states, manifest_path):
    """Reapply saved safe operations to the same rendered input; never call a model."""
    if not Path(manifest_path).is_file():
        return {"status": "not_available", "changed": False}
    report = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if report.get("status") == "skipped":
        return {"status": "skipped", "changed": False}
    if report.get("status") in {"failed", "discarded"}:
        return {"status": "discarded", "changed": False}
    if report.get("policy") != FORMAT_POLICY:
        return {"status": "different_source", "changed": False}
    document = Document(path)
    targets = collect_targets(document, plan, states)
    saved = report.get("target_baselines")
    if isinstance(saved, dict) and set(saved) != {target.target_id for target in targets}:
        return {"status": "different_source", "changed": False}
    _restore_baselines(targets, saved)
    if report.get("source_signature") != protected_signature(document, targets):
        return {"status": "different_source", "changed": False}
    by_id = {target.target_id: target for target in targets}
    changed = False
    for group in report.get("groups", []):
        for iteration in group.get("iterations", []):
            patches = [LayoutPatch.model_validate(p) for p in iteration.get("applied_patches", [])]
            permitted = [by_id[key] for key in iteration.get("target_ids", []) if key in by_id]
            changed = apply_patches(document, permitted, patches) or changed
    if changed:
        save_document(document, path)
    return {"status": "replayed", "changed": changed}
