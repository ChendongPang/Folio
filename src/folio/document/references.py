"""Project requirement sources matched by full heading path, before general RAG."""

from __future__ import annotations

import re
from pathlib import Path

from folio.document.reader import read_docx
from folio.models import Block, Evidence, Section, SectionContent, digest


def normalized(path: list[str]) -> tuple[str, ...]:
    return tuple(title.strip() for title in path)


class ReferenceDocuments:
    def __init__(self, paths: list[str | Path], view: str = "final"):
        self.sources = [read_docx(Path(path), view) for path in paths]
        self.view = view
        self.versions = {str(s.path): digest(s.path.read_bytes().hex()) for s in self.sources}

    def matching(self, section: Section):
        targets = [normalized(p) for p in section.source_heading_paths if p]
        if not targets:
            targets = [normalized(section.heading_path or [section.title])]
        for source in self.sources:
            for block in source.blocks:
                path = normalized(block.path)
                if not any(path[: len(target)] == target for target in targets):
                    continue
                if block.kind == "heading" and path in targets:
                    continue
                yield source, block

    def evidence(self, section: Section, max_chars: int | None) -> list[Evidence]:
        result, size = [], 0
        for source, block in self.matching(section):
            if block.kind == "heading":
                continue
            text = (
                block.text
                if block.kind != "table"
                else "\n".join(" | ".join(r) for r in block.rows)
            )
            if not text:
                continue
            size += len(text)
            if max_chars is not None and size > max_chars:
                raise ValueError(
                    f"Project source for {' / '.join(section.heading_path)} exceeds evidence budget; split the section"
                )
            version = self.versions[str(source.path)]
            result.append(
                Evidence(
                    evidence_id="ev_" + digest(f"{source.path.name}:{version}:{block.locator}"),
                    source_id=source.path.name,
                    source_version=version,
                    locator=block.locator,
                    text=text,
                    metadata={
                        "role": "project_requirement",
                        "heading_path": block.path,
                        "revision_view": self.view,
                        "contains_revisions": any(source.revisions.values()),
                        "inserted": block.inserted,
                        "deleted": block.deleted,
                        "block_kind": block.kind,
                        "rows": block.rows,
                        "comment_ids": block.comment_ids,
                    },
                )
            )
        return result

    def comments(self, section: Section | None = None) -> list[dict]:
        result = []
        for source in self.sources:
            locators = (
                {b.locator for s, b in self.matching(section) if s is source} if section else None
            )
            for comment in source.comments:
                if locators is None or locators.intersection(comment["locators"]):
                    anchor = next(
                        (b for b in source.blocks if b.locator in comment["locators"]), None
                    )
                    result.append(
                        comment
                        | {
                            "source": source.path.name,
                            "source_version": self.versions[str(source.path)],
                            "heading_path": anchor.path if anchor else [],
                        }
                    )
        return result

    def project_content(
        self, section: Section, max_chars=100000, *, language="zh-CN"
    ) -> tuple[SectionContent, list[Evidence]]:
        """Deterministic layout preview: copy source content, never invent requirements."""
        evidence = self.evidence(section, max_chars)
        by_location = {(e.source_id, e.source_version, e.locator): e for e in evidence}
        blocks = []
        for source, block in self.matching(section):
            reference = by_location.get(
                (source.path.name, self.versions[str(source.path)], block.locator)
            )
            kwargs = {
                "block_id": "block_"
                + digest(f"{source.path.name}:{self.versions[str(source.path)]}:{block.locator}"),
                "kind": block.kind,
                "evidence_ids": [reference.evidence_id] if reference else [],
            }
            if block.kind == "table":
                kwargs["rows"] = block.rows
            else:
                kwargs["text"] = block.text
                if block.level:
                    kwargs["heading_level"] = block.level
            blocks.append(Block(**kwargs))
        if not blocks and section.content_mode == "generate":
            return SectionContent(
                blocks=[Block(block_id="missing", text=(
                    "Content pending: no material for this section was found in the supplied sources."
                    if language == "en" else "待补充：提供的材料中未找到该节内容。"
                ))],
                pending_items=["Project material for this section is missing" if language == "en" else "缺少对应章节的项目材料"],
            ), evidence
        return SectionContent(blocks=blocks), evidence

    def cover_values(self) -> dict[str, str]:
        if not self.sources:
            return {}
        preamble = [b.text for b in self.sources[0].blocks if not b.path and b.kind == "paragraph"]
        result = {}
        for text in preamble:
            if (text.endswith("项目") or re.search(r"\bproject$", text, re.I)) and "project_name" not in result:
                result["project_name"] = text
            elif (text.endswith("系统") or re.search(r"\bsystem$", text, re.I)) and "system_name" not in result:
                result["system_name"] = text
            elif ("需求说明书" in text or re.search(r"\brequirements?\s+(?:document|specification)\b", text, re.I)) and "document_title" not in result:
                result["document_title"] = text
            elif text.startswith(("-", "－")) and "requirement_name" not in result:
                result["requirement_name"] = text
            elif "年" in text and "月" in text and text.endswith("日"):
                result["document_date"] = text
        return result

    def audit(self) -> list[dict]:
        return [
            {
                "source": s.path.name,
                "version": self.versions[str(s.path)],
                "revision_view": self.view,
                "revisions": s.revisions,
                "warnings": s.warnings,
                "comments": self.comments_for_source(s.path.name),
                "headings": [
                    {"path": b.path, "locator": b.locator} for b in s.blocks if b.kind == "heading"
                ],
            }
            for s in self.sources
        ]

    def comments_for_source(self, name: str) -> list[dict]:
        return [c for c in self.comments() if c["source"] == name]

    def unmatched(self, sections: list[Section]) -> list[dict]:
        matched = {
            (s.path.name, b.locator)
            for section in sections
            if section.content_mode != "container"
            for s, b in self.matching(section)
        }
        return [
            {"source": s.path.name, "locator": b.locator, "path": b.path, "kind": b.kind}
            for s in self.sources
            for b in s.blocks
            if b.path and b.kind != "heading" and (s.path.name, b.locator) not in matched
        ]
