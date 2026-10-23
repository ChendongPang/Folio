"""One bounded whole-document review of complete accumulated style observations."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections import defaultdict
from copy import deepcopy

from folio.document.style_batches import DIMENSION_DETAILS, TEXT_DIMENSIONS, dimension_coverage
from folio.document.style_contracts import DIMENSION_LABELS, StyleConsolidationStep
from folio.runtime.language import localize_text


def _structure_evidence(document, delivered):
    """Build whole-text navigation/count evidence, without guessing PDF semantics.

    The original slices are already delivered. Rejoining split blocks lets the
    final reviewer see complete heading/TOC lines and exact section counts without
    receiving a second copy of all business paragraphs.
    """
    grouped = defaultdict(list)
    for sample in delivered.values():
        if not sample.get("page_image"):
            grouped[sample.get("block_index", sample["locator"])].append(sample)
    blocks, total = [], 0
    for samples in grouped.values():
        samples.sort(key=lambda sample: sample.get("char_start", 0))
        text = "".join(sample["text"] for sample in samples)
        blocks.append({"text": text, "samples": samples, "start": total, "locator": samples[0]["locator"].split("; chars ")[0], "kind": samples[0]["kind"], "level": samples[0].get("heading_level")})
        total += len(text)

    def evidence(block, offset=0):
        matches = [sample["sample_id"] for sample in block["samples"] if sample.get("char_start", 0) <= offset < sample.get("char_end", len(block["text"]))]
        return ", ".join(matches or [block["samples"][0]["sample_id"]])

    def compact(text):
        return re.sub(r"\.{3,}|…{2,}", " … ", " ".join(text.split()))

    coverage = document["text_coverage"]
    lines = ["## Full-text structure and length evidence (all read text)", f"Coverage: {coverage['characters_read']}/{coverage['characters_total']} characters read; {total} characters reconstructed here, without sampling or skipped text."]
    complete = total == coverage["characters_total"] and coverage["complete"]
    if not complete:
        lines.append("Reconstruction or reading coverage is incomplete; do not infer absence across the entire document.")
    pages = defaultdict(lambda: {"characters": 0, "ids": []})
    headings, sections, contents = [], [], []
    patterns = {
        "Abstract": re.compile(r"摘要|abstract", re.I),
        "Appendix": re.compile(r"附录|appendix|appendices", re.I),
        "References": re.compile(r"参考文献|参考资料|references|bibliography", re.I),
        "Conclusion": re.compile(r"结论|conclusions?", re.I),
    }
    matches_by_part = defaultdict(list)
    for block in blocks:
        page = re.match(r"page (\d+)(?:/|$)", block["locator"])
        if page:
            item = pages[int(page[1])]
            item["characters"] += len(block["text"])
            item["ids"].extend(sample["sample_id"] for sample in block["samples"])
        is_contents = bool(re.search(r"(?:^|\n)\s*目\s*录\s*(?:\n|$)", block["text"])) and bool(re.search(r"\.{3,}|…{2,}", block["text"]))
        if is_contents:
            contents.append(f"- {block['locator']}; evidence {', '.join(sample['sample_id'] for sample in block['samples'])}: \n" + compact(block["text"]))
        offset = 0
        for raw_line in block["text"].splitlines(keepends=True):
            line = raw_line.strip()
            native_heading = block["kind"] == "heading"
            numbered = re.match(r"(?:[一二三四五六七八九十百]+[、．.]|\d+(?:\.\d+)+(?!\d))\s*\S", line)
            if line and (native_heading or numbered) and not is_contents:
                if native_heading or len(line) <= 180:
                    level = block["level"] if native_heading else 1 if re.match(r"[一二三四五六七八九十百]+[、．.]", line) else len(re.match(r"\d+(?:\.\d+)+", line)[0].split("."))
                    headings.append(f"- {evidence(block, offset)} / {block['locator']} / {'native' if native_heading else 'text candidate'} level {level}: {compact(line)}")
                    if level == 1 and not re.search(r"\.{3,}|…{2,}", line):
                        sections.append((block["start"] + offset, compact(line), evidence(block, offset)))
            for name, pattern in patterns.items():
                if pattern.search(line):
                    matches_by_part[name].append(f"{evidence(block, offset)} / {block['locator']}：{compact(line)[:180]}")
            offset += len(raw_line)
    if pages:
        lines += ["### Extractable characters per page (not visual page coverage)", *[f"- Page {page}: {item['characters']} characters; evidence {', '.join(item['ids'])}" for page, item in sorted(pages.items())]]
    lines += ["### Original table of contents (only whitespace and dot leaders collapsed; no entries omitted)", *contents] if contents else ["### Original table of contents", "- No separate TOC block with a recognized label and dot leaders was found; do not infer that the original has no TOC."]
    lines += ["### All identified headings in order", *headings] if headings else ["### Headings in order", "- No native headings or numbered heading candidates were identified. Infer structure from the body text already read; do not invent heading levels."]
    lines += ["### Relative lengths of top-level sections (program counts using heading positions)"]
    if sections:
        body_chars = total - sections[0][0]
        for index, (start, title, sample_id) in enumerate(sections):
            end = sections[index + 1][0] if index + 1 < len(sections) else total
            count = end - start
            lines.append(f"- {sample_id} / {title}: {count} characters, {count / body_chars:.1%} of identified top-level section text.")
        lines.append("Counts include headings and extracted running headers. Text candidates are not native PDF heading styles. Compare relative section detail without claiming exact semantic proportions of explanatory versus conclusive sentences.")
    else:
        lines.append("- No reliable top-level boundaries were identified, so exact section percentages are unavailable. Qualitatively describe detail and the balance of explanation and conclusions from read text; do not demand unsupported percentages.")
    lines += ["### Full-text scan for special section labels (not just summarized rules)"]
    for name in patterns:
        found = matches_by_part[name]
        absent = "These labels were not matched in all extractable text. State that no separate section of this kind was identified in extractable text; do not generalize to unseen images." if complete else "These labels were not matched in the partially reconstructed text; do not claim whole-document absence."
        lines.append(f"- {name}：" + ("Matches at these original locations; distinguish headings from mentions in body text: " if found else absent))
        lines.extend(f"  - {item}" for item in found)
    return "\n\n".join(lines)


def consolidation_text(document, delivered):
    rules = document["rules"]
    locations = {sample["sample_id"]: sample["locator"] for sample in document["sample_locations"]}
    lines = [
        "# Final whole-document style consolidation",
        "All extractable text has been read in batches. The following are all batch observations, not a truncated summary.",
        "Check every R ID and list each one in reviewed_rule_ids. Return the complete final rule set in rules, not only additions from this round.",
        "Resolve contradictory generalizations: if sections use different writing approaches, qualify scope and applicability instead of retaining conflicting document-wide rules.",
        "Merge duplicate observations while preserving important features from different locations. Do not omit earlier-batch features or invent observations from evidence locations alone.",
        "Mark incomplete dimensions as unresolved in dimension_checks. Absence requires evidence; do not label text-analysis gaps as requires_visual.",
        "Cite only the read evidence below, without inheriting example business content. Fonts and page parameters remain based on program measurements.",
        "Completion covers all extractable text and readable native properties; do not require information the source format never stored.",
        "For figures/tables, analyze extractable captions, references and explanations separately. If only image-internal column counts, headers or graphical content are unreadable, record an image limitation without marking completed textual analysis unresolved. Missing analysis of extractable table text must still be unresolved.",
        "For original Word paragraph styles, exact spacing, indents or margin settings not stored in a PDF, state that the source format does not provide them. Neither guess nor require page images to reconstruct original editing settings.",
        "Native measurements are retained separately; do not duplicate them as typography/page/visual rules. Check the provided measurements and limitations for these dimensions in dimension_checks.",
        "Describe the balance of explanation and conclusions qualitatively when supported. Give proportions only with direct statistical support; inability to quantify precisely does not make all detail analysis incomplete.",
        "## Per-dimension review criteria", *[f"- {dimension}（{DIMENSION_LABELS[dimension]}）：{detail}" for dimension, detail in DIMENSION_DETAILS.items()],
    ]
    lines += [
        "## Native layout values actually measured", json.dumps(document.get("layout", {}), ensure_ascii=False),
        *[f"- {item['dimension']}: {localize_text(item['text'], 'en')}" for item in document.get("layout_coverage", {}).get("observations", [])],
        "## Native measurement coverage and source limitations", *[f"- {localize_text(item, 'en')}" for item in document.get("layout_coverage", {}).get("limitations", [])],
        _structure_evidence(document, delivered), "## All batch observations",
    ]
    for index, rule in enumerate(rules, 1):
        evidence = "；".join(f"{sample_id}（{locations.get(sample_id, 'unknown location')}）" for sample_id in rule["sample_ids"])
        lines.append(f"- R{index} | {rule['dimension']} | {rule['scope']} | {rule['confidence']}: {rule['description']}; evidence {evidence}")
    lines += ["## Current dimension review status", *[f"- {dimension}: {item['status']}; {localize_text(item['reason'], 'en')}" for dimension, item in document["dimension_coverage"].items()]]
    if document.get("uncertainties"):
        lines += ["## Current unresolved items (omit resolved items)", *[f"- {localize_text(item, 'en')}" for item in document["uncertainties"]]]
    return "\n\n".join(lines)


async def consolidate_document(document, delivered, *, runtime, config, calls_left, chars_left, profile_coverage, output_dir, session_key, on_progress, copy_guard, user_request=""):
    # Delayed import keeps transport and audit helpers in one place without a
    # second implementation of how real runtime requests are rendered.
    from folio.document.style_profile import (
        _json,
        _overcopies,
        _progress,
        _readable_message,
        _write,
        style_messages,
    )

    document["batch_observations"] = deepcopy(document["rules"])
    document["consolidation"] = {"status": "partial", "observations_total": len(document["rules"])}
    actual_characters = sum(len(sample["text"]) for sample in delivered.values() if not sample.get("page_image"))
    if not document["text_coverage"]["complete"] or actual_characters != document["text_coverage"]["characters_total"]:
        document["uncertainties"].append("最终核对的原文证据未覆盖全部可提取文字，不进行全篇完成认定。")
        return 0, 0, None
    if not calls_left or chars_left <= 0:
        document["uncertainties"].append("全文已分批读取，但预算不足以综合核对全部观察；保留分批结果，尚未完成。")
        return 0, 0, None
    context = {"user_request": user_request, "style_analysis_text": consolidation_text(document, delivered), "style_consolidation": True}
    schema = StyleConsolidationStep.model_json_schema()
    measure = getattr(runtime, "prompt_chars", None)
    size = measure("style_analysis", context, schema) if measure else sum(len(message["content"]) for message in style_messages(context, schema))
    capacity = getattr(runtime, "measure", None)
    limit = chars_left if capacity else min(config.max_prompt_chars, chars_left)
    if size > limit:
        document["uncertainties"].append("全部分批观察超过单次综合核对容量；未截断冒充全篇总结，保留逐批观察并标为未完成。")
        return 0, 0, None
    if capacity:
        try:
            measured = capacity("style_analysis", context, schema, config.max_prompt_chars)
            if inspect.isawaitable(measured):
                measured = await measured
            if not measured.get("fits", False):
                document["uncertainties"].append("全部分批观察超过模型综合核对容量，保留原有观察，尚未完成。")
                return 0, 0, None
        except Exception as exc:
            document["uncertainties"].append(f"综合核对容量测量失败（{type(exc).__name__}），保留原有观察，尚未完成。")
            return 0, 0, None
    parts = getattr(runtime, "request_parts", None)
    messages, wire_schema, *_ = parts("style_analysis", context, schema) if parts else (style_messages(context, schema), schema)
    number = profile_coverage["model_calls"] + 1
    _write(output_dir / "audit" / f"request-{number:02d}.md", "\n\n".join(_readable_message(message) for message in messages) + "\n")
    _json(output_dir / "audit" / f"request-{number:02d}.json", {"messages": messages, "schema": wire_schema, "messages_source": "runtime" if parts else "reference_renderer", "prompt_characters": size})
    await _progress(on_progress, f"正在综合核对 {document['document_id']} 全文的 {len(document['rules'])} 条风格观察，合并重复并检查章节间差异。")
    profile_coverage["model_calls"] += 1
    profile_coverage["input_characters"] += size
    try:
        result = await asyncio.wait_for(runtime.complete("style_analysis", context, schema, session_key + "-consolidation"), timeout=config.timeout_seconds)
        _json(output_dir / "audit" / f"response-{number:02d}.json", {"content": result.content, "stop_reason": result.stop_reason, "usage": result.usage})
        if result.stop_reason not in {"completed", "stop", "end_turn"}:
            raise ValueError('The consolidation review response is incomplete.')
        step = StyleConsolidationStep.model_validate_json(result.content)
        expected = {f"R{index}" for index in range(1, len(document["rules"]) + 1)}
        if set(step.reviewed_rule_ids) != expected or len(step.reviewed_rule_ids) != len(expected):
            raise ValueError('Not every batch observation was individually reviewed.')
        accepted = []
        for rule in step.rules:
            if any(sample_id not in delivered for sample_id in rule.sample_ids) or _overcopies(rule.description, copy_guard):
                raise ValueError('A consolidated rule cites unread content or copies source text.')
            samples = [delivered[sample_id] for sample_id in rule.sample_ids]
            if rule.dimension in {"writing", "organization", "detail", "expression"} and not any(sample["kind"] in {"paragraph", "row", "table_row", "page_image"} for sample in samples):
                raise ValueError('A consolidated rule lacks body-text evidence.')
            if rule.dimension in {"typography", "page", "visual"} and not any(sample.get("page_image") for sample in samples):
                raise ValueError('Consolidated rules must not infer visual properties from plain text.')
            value = rule.model_dump()
            if len({sample.get("block_index", sample["locator"]) for sample in samples}) < 2 and rule.scope in {"general", "body"}:
                value["confidence"] = "tentative"
            accepted.append(value)
        checks = {}
        for check in step.dimension_checks:
            if _overcopies(check.reason, copy_guard) or any(sample_id not in delivered for sample_id in check.sample_ids):
                raise ValueError('A consolidated dimension explanation lacks valid evidence.')
            if check.status == "not_present" and not check.sample_ids:
                raise ValueError('An absence statement lacks evidence from material already read.')
            if check.dimension in TEXT_DIMENSIONS and check.status == "requires_visual":
                raise ValueError('Missing text analysis cannot be classified as an image limitation.')
            checks[check.dimension] = check.model_dump() | {"characters_read_at_check": document["text_coverage"]["characters_read"]}
        candidate = document | {"rules": accepted}
        coverage = dimension_coverage(candidate, checks)
        if any(item["status"] == "unresolved" for item in coverage.values()):
            raise ValueError('The consolidated dimensions remain incomplete.')
        document["rules"] = accepted
        document["dimension_coverage"] = coverage
        document["consolidation"]["status"] = "completed"
        document["consolidation"]["uncertainties"] = [item for item in step.uncertainties if not _overcopies(item, copy_guard)]
        return 1, size, checks
    except Exception as exc:
        document["uncertainties"].append(f"全文综合核对未完成（{type(exc).__name__}），保留全部分批观察，不替换为不完整的最终结论。")
        return 1, size, None
