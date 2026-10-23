"""Validated sample typography, applied once and protected through later reviews.

Only program-extracted layout parameters belong here. Sample text, headings,
headers, logos and semantic model suggestions never enter a DOCX through this API.
"""

import json
import re
from copy import deepcopy
from typing import Literal

from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from pydantic import BaseModel, ConfigDict, Field, model_validator

from folio.document.reader import xpath
from folio.document.typography import BODY, NOTICE, TABLE_BODY, TABLE_HEADER, prop
from folio.runtime.language import localize_text, output_language

STYLE_LAYOUT_POLICY = 1
STYLE_VARIABLE = "FolioStyleLayout"


class _LayoutModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ParagraphStyle(_LayoutModel):
    font_name: str | None = Field(default=None, min_length=1, max_length=100)
    east_asia_font: str | None = Field(default=None, min_length=1, max_length=100)
    font_size_pt: float | None = Field(default=None, ge=5, le=72)
    bold: bool | None = None
    italic: bool | None = None
    color: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{6}$")
    alignment: Literal["left", "center", "right", "justify"] | None = None
    line_spacing: float | None = Field(default=None, gt=0, le=144)
    line_spacing_rule: Literal["multiple", "exactly", "at_least"] | None = None
    space_before_pt: float | None = Field(default=None, ge=0, le=144)
    space_after_pt: float | None = Field(default=None, ge=0, le=144)
    first_line_indent_pt: float | None = Field(default=None, ge=-144, le=144)
    left_indent_pt: float | None = Field(default=None, ge=-144, le=288)
    right_indent_pt: float | None = Field(default=None, ge=-144, le=288)

    @model_validator(mode="after")
    def coherent_spacing(self):
        if self.line_spacing_rule is not None and self.line_spacing is None:
            raise ValueError("A line-spacing rule requires a measured spacing value")
        if (self.line_spacing_rule or "multiple") == "multiple" and (
            self.line_spacing is not None and self.line_spacing > 5
        ):
            raise ValueError("Multiple line spacing cannot exceed five lines")
        return self


class PageStyle(_LayoutModel):
    width_mm: float | None = Field(default=None, ge=70, le=600)
    height_mm: float | None = Field(default=None, ge=70, le=600)
    margin_top_mm: float | None = Field(default=None, ge=0, le=100)
    margin_bottom_mm: float | None = Field(default=None, ge=0, le=100)
    margin_left_mm: float | None = Field(default=None, ge=0, le=100)
    margin_right_mm: float | None = Field(default=None, ge=0, le=100)
    header_distance_mm: float | None = Field(default=None, ge=0, le=100)
    footer_distance_mm: float | None = Field(default=None, ge=0, le=100)


class TableStyle(_LayoutModel):
    body: ParagraphStyle = Field(default_factory=ParagraphStyle)
    header: ParagraphStyle = Field(default_factory=ParagraphStyle)
    header_fill: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{6}$")
    border_color: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{6}$")
    border_size_pt: float | None = Field(default=None, ge=0.25, le=6)


class StyleLayout(_LayoutModel):
    body: ParagraphStyle = Field(default_factory=ParagraphStyle)
    headings: dict[str, ParagraphStyle] = Field(default_factory=dict)
    page: PageStyle = Field(default_factory=PageStyle)
    table: TableStyle = Field(default_factory=TableStyle)

    @model_validator(mode="after")
    def supported_headings(self):
        if not self.headings.keys() <= {str(level) for level in range(1, 7)}:
            raise ValueError("Sample headings must use supported levels 1 through 6")
        return self


def _compact(value):
    if isinstance(value, dict):
        return {key: result for key, item in value.items() if (result := _compact(item)) != {}}
    return value


def layout_values(layout):
    return _compact(layout.model_dump(exclude_none=True))


def profile_layout(profile):
    return StyleLayout.model_validate((profile or {}).get("layout") or {})


def _paths(value, prefix=""):
    return [
        path
        for key, item in value.items()
        for path in (_paths(item, prefix + key + ".") if isinstance(item, dict) else [prefix + key])
    ]


def style_application(profile, *, template=False):
    """Describe decisions even when image-based review is disabled."""
    values = layout_values(profile_layout(profile))
    return {
        "policy": STYLE_LAYOUT_POLICY,
        "status": "template_priority" if template and values else "applied" if values else "none",
        "applied": [] if template else _paths(values),
        "skipped": [{"rule": path, "reason": "template_priority"} for path in _paths(values)]
        if template
        else [],
        "limitations": [
            "封面设计、徽标、页眉页脚文字不从示例复制。",
            "复杂分节布局、图文浮动位置、局部特殊样式和字体安装/替换未自动复刻。",
            "未提取或未充分确认的参数保留模板或默认设置。",
        ]
        if profile
        else [],
        "user_format_policy": (profile or {}).get("user_format_policy", {}),
    }


def _paragraph(style, values):
    values = values.model_dump(exclude_none=True)
    font = style.font
    for key, attribute in (("font_name", "name"), ("bold", "bold"), ("italic", "italic")):
        if key in values:
            setattr(font, attribute, values[key])
    rpr = style.element.get_or_add_rPr()
    if "font_name" in values or "east_asia_font" in values:
        names = {}
        if "font_name" in values:
            names.update(ascii=values["font_name"], hAnsi=values["font_name"])
        if "east_asia_font" in values:
            names["eastAsia"] = values["east_asia_font"]
        node = prop(rpr, "rFonts", **names)
        for name in names:
            node.attrib.pop(qn("w:" + name + "Theme"), None)
    if "font_size_pt" in values:
        font.size = Pt(values["font_size_pt"])
        prop(rpr, "szCs", val=round(values["font_size_pt"] * 2))
    if "color" in values:
        font.color.rgb = RGBColor.from_string(values["color"].upper())
        color = rpr.find(qn("w:color"))
        for key in ("themeColor", "themeTint", "themeShade"):
            color.attrib.pop(qn("w:" + key), None)
    paragraph = style.paragraph_format
    if "alignment" in values:
        paragraph.alignment = {
            "left": WD_ALIGN_PARAGRAPH.LEFT,
            "center": WD_ALIGN_PARAGRAPH.CENTER,
            "right": WD_ALIGN_PARAGRAPH.RIGHT,
            "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
        }[values["alignment"]]
    for key, attribute in (
        ("space_before_pt", "space_before"),
        ("space_after_pt", "space_after"),
        ("first_line_indent_pt", "first_line_indent"),
        ("left_indent_pt", "left_indent"),
        ("right_indent_pt", "right_indent"),
    ):
        if key in values:
            setattr(paragraph, attribute, Pt(values[key]))
    if "first_line_indent_pt" in values:
        indent = style.element.get_or_add_pPr().find(qn("w:ind"))
        for key in ("firstLineChars", "hangingChars"):
            indent.attrib.pop(qn("w:" + key), None)
    if "line_spacing" in values:
        rule = values.get("line_spacing_rule", "multiple")
        paragraph.line_spacing = (
            values["line_spacing"] if rule == "multiple" else Pt(values["line_spacing"])
        )
        paragraph.line_spacing_rule = {
            "multiple": WD_LINE_SPACING.MULTIPLE,
            "exactly": WD_LINE_SPACING.EXACTLY,
            "at_least": WD_LINE_SPACING.AT_LEAST,
        }[rule]


def _remember(document, layout):
    settings = document.settings.element
    variables = settings.find(qn("w:docVars"))
    if variables is None:
        variables = OxmlElement("w:docVars")
        settings.append(variables)
    for node in list(variables):
        if (node.get(qn("w:name")) or "").endswith("StyleLayout"):
            variables.remove(node)
    variable = OxmlElement("w:docVar")
    variable.set(qn("w:name"), STYLE_VARIABLE)
    variable.set(qn("w:val"), json.dumps(layout_values(layout), ensure_ascii=False))
    variables.append(variable)


def document_style_layout(document):
    variables = xpath(document.settings.element, "./w:docVars/w:docVar")
    values = [
        node.get(qn("w:val"))
        for node in variables
        if node.get(qn("w:name")) == STYLE_VARIABLE
    ]
    # Earlier exports used a different product prefix. Read their layout contract
    # without rewriting the uploaded document or invalidating its content hash.
    if not values:
        values = [
            node.get(qn("w:val"))
            for node in variables
            if (node.get(qn("w:name")) or "").endswith("StyleLayout")
        ]
    if not values:
        return StyleLayout()
    if len(values) != 1:
        raise ValueError("Duplicate sample style layout declarations")
    return StyleLayout.model_validate_json(values[0])


def apply_style_layout(document, profile, *, template=False):
    layout = profile_layout(profile)
    report = style_application(profile, template=template)
    if template or not layout_values(layout):
        return report
    # Page constraints are checked against the concrete document, including any
    # dimensions omitted in the sample and therefore supplied by defaults.
    page_map = {
        "width_mm": "page_width",
        "height_mm": "page_height",
        "margin_top_mm": "top_margin",
        "margin_bottom_mm": "bottom_margin",
        "margin_left_mm": "left_margin",
        "margin_right_mm": "right_margin",
        "header_distance_mm": "header_distance",
        "footer_distance_mm": "footer_distance",
    }
    values = layout.page.model_dump(exclude_none=True)
    for section in document.sections:
        measured = {key: Mm(value) for key, value in values.items()}
        actual = {
            key: measured.get(key, getattr(section, attribute))
            for key, attribute in page_map.items()
        }
        if actual["width_mm"] - actual["margin_left_mm"] - actual["margin_right_mm"] < Mm(
            20
        ) or actual["height_mm"] - actual["margin_top_mm"] - actual["margin_bottom_mm"] < Mm(20):
            raise ValueError("Sample margins leave less than 20 mm of writable page area")
        for key, value in measured.items():
            setattr(section, page_map[key], value)
    for name in ("Normal", BODY, NOTICE):
        _paragraph(document.styles[name], layout.body)
    for level in range(1, 7):
        if str(level) in layout.headings:
            for name in (f"Heading {level}", f"Folio Heading {level}"):
                _paragraph(document.styles[name], layout.headings[str(level)])
        # A paper-size change requires rebuilding the existing right-hand TOC tab.
        toc = document.styles[f"Folio TOC {level}"]
        width = (
            document.sections[0].page_width
            - document.sections[0].left_margin
            - document.sections[0].right_margin
        )
        toc.paragraph_format.tab_stops.clear_all()
        toc.paragraph_format.tab_stops.add_tab_stop(
            width, WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS
        )
    _paragraph(document.styles[TABLE_BODY], layout.table.body)
    _paragraph(document.styles[TABLE_HEADER], layout.table.header)
    _remember(document, layout)
    return report


def style_generated_table(table, profile):
    layout = profile_layout(profile).table
    if layout.header_fill:
        for cell in table.rows[0].cells:
            prop(
                cell._tc.get_or_add_tcPr(),
                "shd",
                val="clear",
                color="auto",
                fill=layout.header_fill.upper(),
            )
    if layout.border_color is not None or layout.border_size_pt is not None:
        borders = prop(table._tbl.tblPr, "tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            attributes = {"val": "single"}
            if layout.border_color is not None:
                attributes["color"] = layout.border_color.upper()
            if layout.border_size_pt is not None:
                attributes["sz"] = round(layout.border_size_pt * 8)
            prop(borders, edge, **attributes)


def protected_style_properties(document, element, kind, *, layout=None):
    """Explicitly adopted style parameters cannot be reset by formatting agents."""
    if kind == "table":
        return set()
    layout = layout if layout is not None else document_style_layout(document)
    if not layout_values(layout):
        return set()
    if kind == "cell":
        ids = xpath(element, "./w:pPr/w:pStyle/@w:val")
        header = bool(
            ids
            and TABLE_HEADER in document.styles
            and ids[0] == document.styles[TABLE_HEADER].style_id
        )
        specification = layout.table.header if header else layout.table.body
    else:
        specification = layout.body
    values = specification.model_dump(exclude_none=True)
    mapping = {
        "font_size_pt": "font_delta_pt",
        "alignment": "alignment",
        "space_before_pt": "space_before_pt",
        "space_after_pt": "space_after_pt",
    }
    return {property_name for key, property_name in mapping.items() if key in values}


def layout_review_style(profile, *, template=False):
    if not profile:
        return None
    return {
        "style_markdown": (profile.get("style_markdown") or profile.get("markdown") or ""),
        "application": style_application(profile, template=template),
        "priority": "Explicit user requirements and the template take precedence. Do not revert an adopted style to defaults. Adjust only properties allowed by the target.",
    }


_SIZES = {
    "初号": 42,
    "小初": 36,
    "一号": 26,
    "小一": 24,
    "二号": 22,
    "小二": 18,
    "三号": 16,
    "小三": 15,
    "四号": 14,
    "小四": 12,
    "五号": 10.5,
    "小五": 9,
    "六号": 7.5,
    "小六": 6.5,
    "七号": 5.5,
    "八号": 5,
}
_FONTS = r"微软雅黑|Times New Roman|Arial|Calibri|Cambria|Aptos|仿宋(?:_GB2312)?|楷体(?:_GB2312)?|宋体|黑体|等线"
_SCOPE = re.compile(
    r"正文|([一二三四五六1-6])级标题|[上下左右](?:页)?边距|页边距|页面|纸张|"
    r"\b(?:heading\s*([1-6])|level\s*([1-6])\s+headings?|body(?:\s+text)?|"
    r"(?:top|bottom|left|right)\s+margin|margins?|page|paper)\b", re.I
)
_DOUBTFUL = re.compile(
    r"[?？]|(?:是否|能否|可否|可以吗|比如|例如|示例|举例|假设|如果|不要|不用|不使用|不采用|不需要|取消|撤销|别用|改回|还是|或者|或用)|"
    r"\b(?:if|whether|could|would|example|suppose|instead|or|not|never|cancel|revert|undo|don't|do\s+not)\b", re.I
)
_POLICY_HEADING = "\n## 本次明确的格式要求\n"
_EN_POLICY_HEADING = "\n## Explicit formatting requirements\n"


def resolve_style_policy(profile, user_request):
    """Apply only unambiguous, scoped numeric/font requests; never guess intent.

    This small deterministic parser handles common Chinese and English formatting commands.
    Unknown prose remains visible for review, and does not become a fabricated
    executable parameter. Quotes, examples, questions and negated instructions
    cannot accidentally override an uploaded sample.
    """
    result = deepcopy(profile or {})
    layout = layout_values(profile_layout(result))
    baseline = deepcopy(layout)
    overrides, unresolved = {}, []
    text = re.sub(r"```[\s\S]*?```", "", str(user_request or ""))
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    text = re.sub(r'“[^”]*”|「[^」]*」|"[^"\n]*"', "", text)
    for sentence in re.split(r"[。；;\n]", text):
        scopes = list(_SCOPE.finditer(sentence))
        if not scopes and re.search(r"(?:取消|撤销|忽略).*(?:要求|格式)|\b(?:cancel|revert|undo|ignore)\b.*\b(?:formatting|requirements?)\b", sentence, re.I):
            layout, overrides = deepcopy(baseline), {}
            unresolved.append(sentence.strip()[:160])
        for index, match in enumerate(scopes):
            end = scopes[index + 1].start() if index + 1 < len(scopes) else len(sentence)
            clause = sentence[match.start() : end].strip()
            if not re.search(
                r"字体|字号|行距|边距|纸张|加粗|缩进|[磅厘毫]|倍|号|\b(?:font|size|spacing|margin|margins|paper|bold|indent|pt|points|cm|mm|inches)\b|" + _FONTS, clause, re.I
            ):
                continue
            scope = match.group()
            updates = {}
            prefix = (
                "body"
                if scope == "正文" or scope.lower() in {"body", "body text"}
                else "headings." + str("一二三四五六".find(match.group(1)) + 1)
                if match.group(1) and not match.group(1).isdigit()
                else "headings." + match.group(1)
                if match.group(1)
                else "headings." + (match.group(2) or match.group(3))
                if match.group(2) or match.group(3)
                else "page"
            )
            # Check the whole sentence: a qualifier may precede the scope, and a
            # question about a font must not be treated as formatting authorization.
            if _DOUBTFUL.search(sentence):
                if re.search(r"不要|不用|不使用|不采用|取消|撤销|别用|改回|\b(?:not|never|cancel|revert|undo|don't)\b", sentence, re.I):
                    target, original = layout, baseline
                    for key in prefix.split(".")[:-1]:
                        target = target.setdefault(key, {})
                        original = original.get(key, {})
                    key = prefix.split(".")[-1]
                    if key in original:
                        target[key] = deepcopy(original[key])
                    else:
                        target.pop(key, None)
                    overrides = {
                        key: value
                        for key, value in overrides.items()
                        if not key.startswith(prefix + ".")
                    }
                unresolved.append(clause[:160])
                continue
            if prefix == "page":
                margins = list(
                    re.finditer(
                        r"(?:(上|下|左|右)(?:页)?边距|页边距)\s*(?:为|设为|设置为|采用|用|:|：)?\s*(\d+(?:\.\d+)?)\s*(厘米|毫米|cm|mm)",
                        clause,
                        re.I,
                    )
                )
                for item in margins:
                    value = float(item.group(2)) * (
                        10 if item.group(3).lower() in {"厘米", "cm"} else 1
                    )
                    sides = (
                        [{"上": "top", "下": "bottom", "左": "left", "右": "right"}[item.group(1)]]
                        if item.group(1)
                        else ["top", "bottom", "left", "right"]
                    )
                    updates.update({f"margin_{side}_mm": value for side in sides})
                for item in re.finditer(
                    r"\b(?:(top|bottom|left|right)\s+)?margins?\s*(?:to|is|are|of|at|=|:)?\s*"
                    r"(\d+(?:\.\d+)?)\s*(cm|mm|inches|inch|in)\b", clause, re.I
                ):
                    value = float(item.group(2)) * ({"cm": 10, "mm": 1}.get(item.group(3).lower(), 25.4))
                    sides = [item.group(1).lower()] if item.group(1) else ["top", "bottom", "left", "right"]
                    updates.update({f"margin_{side}_mm": value for side in sides})
                paper = re.search(
                    r"(?:纸张|\bpaper|\bpage)\s*(?:size\s*)?(?:为|设为|设置为|采用|用|to|is|=|:|：)?\s*(A[345])", clause, re.I
                )
                if paper:
                    updates.update(
                        zip(
                            ("width_mm", "height_mm"),
                            {"A3": (297, 420), "A4": (210, 297), "A5": (148, 210)}[
                                paper.group(1).upper()
                            ],
                            strict=True,
                        )
                    )
            else:
                font = re.search(_FONTS, clause, re.I)
                if font:
                    updates[
                        "east_asia_font"
                        if re.search(r"[\u4e00-\u9fff]", font.group())
                        else "font_name"
                    ] = font.group()
                size = re.search(
                    r"(?:字号|字体大小|\bfont\s+size|\bsize)\s*(?:为|设为|设置为|采用|用|to|is|=|:|：)?\s*(\d+(?:\.\d+)?)\s*(?:pt|磅|points?)",
                    clause,
                    re.I,
                )
                if size:
                    updates["font_size_pt"] = float(size.group(1))
                else:
                    size = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:pt|points?)\s+(?:font|" + _FONTS + r")\b", clause, re.I)
                    if size:
                        updates["font_size_pt"] = float(size.group(1))
                    else:
                        size = re.search("|".join(_SIZES), clause)
                        if size:
                            updates["font_size_pt"] = _SIZES[size.group()]
                multiple = re.search(r"(\d+(?:\.\d+)?)\s*倍(?:行距)?", clause) or re.search(
                    r"\b(?:line\s+spacing\s*(?:to|is|=|:)?\s*)(\d+(?:\.\d+)?)(?![\d.])(?!\s*(?:pt|points?))", clause, re.I
                )
                exact = re.search(
                    r"(?:行距|固定行距|\bline\s+spacing)\s*(?:为|设为|设置为|固定值?|to|is|exactly|=|:|：)?\s*(\d+(?:\.\d+)?)\s*(?:pt|磅|points?)",
                    clause,
                    re.I,
                )
                if multiple:
                    updates.update(
                        line_spacing=float(multiple.group(1)), line_spacing_rule="multiple"
                    )
                elif exact:
                    updates.update(line_spacing=float(exact.group(1)), line_spacing_rule="exactly")
                if "加粗" in clause or re.search(r"\bbold\b", clause, re.I):
                    updates["bold"] = True
            if not updates:
                unresolved.append(clause[:160])
                continue
            candidate = deepcopy(layout)
            target = candidate
            for key in prefix.split("."):
                target = target.setdefault(key, {})
            target.update(updates)
            try:
                StyleLayout.model_validate(candidate)
            except ValueError:
                unresolved.append(clause[:160])
                continue
            layout = candidate
            overrides.update({prefix + "." + key: value for key, value in updates.items()})
    result["layout"] = layout
    result["user_format_policy"] = {
        "overrides": overrides,
        "unresolved": list(dict.fromkeys(unresolved)),
    }
    markdown = (result.get("style_markdown") or result.get("markdown") or "")
    for heading in (_POLICY_HEADING, _EN_POLICY_HEADING):
        markdown = markdown.split(heading, 1)[0]
    if overrides or unresolved:
        lines = ["以下明确参数优先于示例风格；上传模板的固定格式仍保留。"]
        lines.extend(f"- {key}：{value}" for key, value in overrides.items())
        if unresolved:
            lines.append(
                "以下表述未自动转换为排版参数，不能视为已执行（可能是疑问、否定、示例或尚未支持的要求）："
            )
            lines.extend(f"- {value}" for value in dict.fromkeys(unresolved))
        language = output_language(user_request, default=result.get("language", "zh-CN"))
        markdown += (_EN_POLICY_HEADING if language == "en" else _POLICY_HEADING) + "\n".join(
            localize_text(line, language) for line in lines
        ) + "\n"
    if markdown:
        result["style_markdown"] = result["markdown"] = markdown
    return result
