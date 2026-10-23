"""Explicit constraints from an optional uploaded Word template, never factual evidence."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class TemplateTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str
    body_index: int
    headers: list[str] = Field(min_length=1)
    header_rows: int = Field(default=1, ge=1)
    min_data_rows: int = Field(default=1, ge=1)


class TemplateSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str
    title: str
    level: int = Field(ge=1, le=6)
    body_index: int
    instruction: str = ""
    allow_children: bool = False
    tables: list[TemplateTable] = Field(default_factory=list)


class TemplateContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    name: str
    source_hash: str
    sections: list[TemplateSection] = Field(min_length=1)

    def planning_context(self) -> dict:
        # No local filesystem path or example business prose goes to a model.
        return {
            "name": self.name,
            "version": self.source_hash,
            "sections": [section.model_dump(exclude={"body_index"}) for section in self.sections],
            "rules": [
                "Preserve existing template heading text, levels, parent-child relationships and order; do not delete, rename or merge fixed sections.",
                "Add subsections only within sections where allow_children=true; do not expand other locations.",
                "Sections with required tables must use generate. Use the specified headers and at least min_data_rows data rows for each table.",
                "The template prescribes structure, writing requirements and formatting, not project facts. Explicitly mark unsupported fields as awaiting confirmation.",
                "instruction contains template completion guidance, not text to copy into the generated body; preserve layout from the original Word template.",
            ],
        }


_INSTRUCTION = re.compile(
    r"^(?:[【\[（(]\s*)?(?:编写说明|填写说明|填写要求|编写要求|写作要求|写作说明|说明[:：]|"
    r"请(?:在|根据|填写|描述|说明|列出|补充|保留|编写|删除|按)|"
    r"本(?:节|章)(?:应|需|须|用于|可|允许|禁止|不得|不能|不允许)|(?:可|允许|不得|禁止|不允许)(?:按|根据|增加|新增|扩展|添加)|"
    r"(?:writing|completion|formatting)\s+(?:instructions?|requirements?)\b|instructions?\s*:|"
    r"please\s+(?:enter|fill|describe|list|provide|keep|write|remove|follow)\b|"
    r"this\s+(?:section|chapter)\s+(?:must|should|may|shall|can|cannot)\b|"
    r"(?:you\s+)?(?:may|can|must\s+not|cannot|do\s+not)\s+(?:add|extend|insert)\b)", re.I
)
_ALLOW_CHILDREN = re.compile(
    r"(?:允许|可以|可)(?!以?不).{0,24}(?:增加|新增|扩展|添加).{0,12}(?:子章节|子节|子标题|下级章节|下级标题|[二三四五六]级标题)|"
    r"\b(?:may|can|allowed\s+to)\s+(?:add|extend|insert)\s+(?:new\s+)?(?:subsections?|subheadings?|child\s+sections?)\b", re.I
)
_DENY_CHILDREN = re.compile(
    r"(?:禁止|不得|不允许|不能|不可).{0,24}(?:增加|新增|扩展|添加).{0,12}(?:章节|标题|目录)|"
    r"\b(?:must\s+not|may\s+not|cannot|do\s+not|not\s+allowed\s+to)\s+(?:add|extend|insert)\s+(?:new\s+)?(?:subsections?|subheadings?|sections?|headings?|chapters?)\b", re.I
)


def read_template(path: Path, *, name: str | None = None) -> TemplateContract:
    """Read genuine Word structure; unsupported structures fail without dropping content."""
    from folio.document.reader import read_docx, xpath

    path = Path(path)
    if path.suffix.lower() != ".docx":
        raise ValueError('Use a DOCX template to preserve Word structure and formatting.')
    source = read_docx(path)
    body = source.document.element.body
    sections: list[TemplateSection] = []
    current = None
    previous_level = 0
    ancestors = []
    siblings = set()
    for block in source.blocks:
        if block.kind == "heading":
            if block.element.getparent() is not body:
                raise ValueError('Template headings must be in the document body. Headings nested in content controls or revisions are not supported.')
            if block.level > 6 or block.level > previous_level + 1:
                raise ValueError('Template headings must start at level 1 without skipping levels. Up to six heading levels are supported.')
            if len(block.text) > 200:
                raise ValueError('A template heading exceeds 200 characters. Shorten it and upload the template again.')
            while ancestors and ancestors[-1].level >= block.level:
                ancestors.pop()
            sibling = (ancestors[-1].section_id if ancestors else None, block.text.casefold())
            if sibling in siblings:
                raise ValueError(f'The template has duplicate headings "{block.text}" under the same parent. Give them distinct names.')
            siblings.add(sibling)
            current = TemplateSection(
                section_id=f"template_{len(sections) + 1:04d}",
                title=block.text,
                level=block.level,
                body_index=block.body_index,
            )
            sections.append(current)
            ancestors.append(current)
            previous_level = block.level
        elif current is not None and block.kind == "paragraph" and (
            _INSTRUCTION.search(block.text)
            or _ALLOW_CHILDREN.search(block.text)
            or _DENY_CHILDREN.search(block.text)
        ):
            current.instruction = "\n".join(filter(None, [current.instruction, block.text]))
            if len(current.instruction) > 4000:
                raise ValueError(f'Completion instructions for template section "{current.title}" exceed 4,000 characters. Shorten them.')
        elif current is not None and block.kind == "table":
            if block.element.getparent() is not body or xpath(block.element, ".//w:tbl"):
                raise ValueError('Nested tables in the template body are not supported. Place required tables directly in the section body.')
            header_rows = 0
            for row in xpath(block.element, "./w:tr"):
                if not xpath(row, "./w:trPr/w:tblHeader[not(@w:val='0' or @w:val='false' or @w:val='off')]"):
                    break
                header_rows += 1
            header_rows = max(1, header_rows)
            for row in xpath(block.element, "./w:tr")[header_rows:]:
                if xpath(row, "./w:tc/w:tcPr/w:vMerge | ./w:tc/w:tcPr/w:gridSpan[number(@w:val) > 1]"):
                    raise ValueError(
                        f'Data rows in template section "{current.title}" contain merged cells and cannot be filled automatically. Keep merged headers but use one data cell per column.'
                    )
            width = len(block.rows[0])
            if any(len(row) != width for row in block.rows[:header_rows]):
                raise ValueError(f'Header column counts in template section "{current.title}" are inconsistent. Check merged cells.')
            headers = []
            for column in range(width):
                labels = []
                for row in block.rows[:header_rows]:
                    label = row[column].strip()
                    if label and (not labels or labels[-1] != label):
                        labels.append(label)
                headers.append(" / ".join(labels))
            if not headers or not all(headers):
                raise ValueError(f'Required tables in template section "{current.title}" need explicit column headings.')
            current.tables.append(TemplateTable(
                table_id=f"{current.section_id}_table_{len(current.tables) + 1}",
                body_index=block.body_index,
                headers=headers,
                header_rows=header_rows,
            ))
    if not sections:
        raise ValueError('No template headings were recognized. Apply Word Heading 1, Heading 2 and related styles.')
    complex_tags = {"drawing", "pict", "txbxContent", "sdt", "ins", "del", "moveTo", "moveFrom"}
    for element in list(body)[sections[0].body_index:]:
        if any(
            node.tag.rsplit("}", 1)[-1] in complex_tags or node.tag.endswith("PrChange")
            for node in element.iter()
        ):
            raise ValueError(
                'The template body contains images, text boxes, content controls or unresolved revisions whose retention cannot be determined. Use headings, completion instructions and tables in the body. Cover images and headers/footers can be preserved.'
            )
    for section in sections:
        section.allow_children = bool(_ALLOW_CHILDREN.search(section.instruction)) and not bool(
            _DENY_CHILDREN.search(section.instruction)
        )
    return TemplateContract(
        path=str(path.resolve()), name=name or path.name,
        source_hash=hashlib.sha256(path.read_bytes()).hexdigest(), sections=sections,
    )


def verify_template_source(template: TemplateContract) -> None:
    path = Path(template.path)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != template.source_hash:
        raise ValueError('The original template is missing or changed. Regenerate and confirm the outline.')


def _template_mapping(sections, template: TemplateContract):
    """Match real fixed headings and permit extra descendants only at explicit openings."""
    actual, mapping = [], {}
    fixed = template.sections
    cursor = 0
    expected_stack = []
    expected_parents = {}
    for node in fixed:
        while expected_stack and expected_stack[-1].level >= node.level:
            expected_stack.pop()
        expected_parents[node.section_id] = expected_stack[-1].section_id if expected_stack else None
        expected_stack.append(node)
    errors = []
    for index, node in enumerate(sections):
        while actual and actual[-1][0].level >= node.level:
            actual.pop()
        next_fixed = fixed[cursor] if cursor < len(fixed) else None
        if next_fixed and node.title.strip() == next_fixed.title and node.level == next_fixed.level:
            parent = actual[-1][1] if actual else None
            if parent != expected_parents[next_fixed.section_id]:
                errors.append(f'The parent of template section "{next_fixed.title}" has changed.')
            if next_fixed.tables and node.content_mode != "generate":
                errors.append(f'Template section "{next_fixed.title}" has required tables and must use generate.')
            mapping[index] = next_fixed
            actual.append((node, next_fixed.section_id, next_fixed.allow_children))
            cursor += 1
        else:
            permitted = bool(actual and actual[-1][2])
            if not permitted:
                errors.append(f'The template does not permit adding or renaming a section at this position: {node.title!r}.')
            # An existing fixed heading cannot be relocated into an expansion slot.
            if any(node.title.strip() == item.title and node.level == item.level for item in fixed):
                errors.append(f'A fixed template section is duplicated or out of order: {node.title!r}.')
            actual.append((node, None, permitted))
    if cursor < len(fixed):
        errors.append("缺少模板固定章节或顺序不符：" + "、".join(node.title for node in fixed[cursor:]))
    if errors:
        raise ValueError("\n".join(errors))
    return mapping


def bind_template(plan, template: TemplateContract):
    """Attach trusted constraints after model planning, never accept model-authored bindings."""
    mapping = _template_mapping(plan.sections, template)
    plan.template = template.model_copy(deep=True)
    for index, section in enumerate(plan.sections):
        node = mapping.get(index)
        if node is None:
            continue
        section.template_section_id = node.section_id
        section.required_tables = [table.model_copy(deep=True) for table in node.tables]
        section.allow_subheadings = node.allow_children
        if node.instruction:
            section.instruction += "\nTemplate completion requirements: " + node.instruction
    return plan


def validate_template_plan(plan) -> None:
    if plan.template is None:
        return
    verify_template_source(plan.template)
    mapping = _template_mapping(plan.sections, plan.template)
    for index, section in enumerate(plan.sections):
        node = mapping.get(index)
        if node is None:
            if section.template_section_id or section.required_tables:
                raise ValueError('New sections must not reuse fixed template section or table constraints.')
        elif (
            section.template_section_id != node.section_id
            or section.required_tables != node.tables
            or section.allow_subheadings != node.allow_children
        ):
            raise ValueError(f'Confirmed constraints for template section "{node.title}" are inconsistent.')
