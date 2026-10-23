"""Read native PDF typography without guessing Word properties from coordinates."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from io import BytesIO

from pypdf import PdfReader

from folio.document.style_layout import PageStyle, ParagraphStyle
from folio.research.formats import MAX_PAGES, SourceFormatError

FONT_FAMILIES = {
    "TimesNewRomanPSMT": "Times New Roman", "Times New Roman": "Times New Roman",
    "ArialMT": "Arial", "Arial": "Arial", "Helvetica": "Helvetica", "Courier": "Courier",
    "SimSun": "宋体", "NSimSun": "新宋体", "KaiTi": "楷体", "SimHei": "黑体",
    "FangSong": "仿宋", "DengXian-Regular": "等线", "MicrosoftYaHeiUI": "Microsoft YaHei UI",
    "MicrosoftYaHei": "微软雅黑", "Calibri": "Calibri", "Cambria": "Cambria",
}


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _font_name(font):
    if not font:
        return None
    name = str(font.get("/BaseFont", "")).lstrip("/")
    # Embedded subset prefixes identify a subset, not a different typeface.
    name = re.sub(r"^[A-Z]{6}\+", "", name)
    return name if name and len(name) <= 100 else None


def _font_traits(font, name):
    descriptor = font.get("/FontDescriptor") if font else None
    if descriptor is None and font and font.get("/DescendantFonts"):
        descriptor = font["/DescendantFonts"][0].get_object().get("/FontDescriptor")
    descriptor = descriptor.get_object() if descriptor is not None else {}
    weight, angle = _number(descriptor.get("/FontWeight")), _number(descriptor.get("/ItalicAngle"))
    flags = int(descriptor.get("/Flags", 0))
    bold_name = re.search(r"(?:bold(?:italic|oblique)?(?:mt)?|-(?:semibold|demi|heavy|black)(?:italic|oblique)?)$", name, re.I)
    italic_name = re.search(r"(?:italic|oblique)(?:mt)?$", name, re.I)
    bold = True if bold_name or (weight is not None and weight >= 600) or flags & (1 << 18) else False if name in FONT_FAMILIES or weight is not None else None
    italic = True if italic_name or (angle is not None and angle != 0) or flags & 64 else False if name in FONT_FAMILIES or angle == 0 else None
    return bold, italic


def _rgb(components, space, resources, transforms):
    if not components or not all(value is not None and 0 <= value <= 1 for value in components):
        return None
    spaces = resources.get("/ColorSpace", {})
    spaces = spaces.get_object() if hasattr(spaces, "get_object") else spaces
    selected = spaces.get(space, space)
    selected = selected.get_object() if hasattr(selected, "get_object") else selected
    if selected == "/DeviceGray" and len(components) == 1:
        return "".join(f"{round(value * 255):02X}" for value in components * 3)
    if selected == "/DeviceRGB" and len(components) == 3:
        return "".join(f"{round(value * 255):02X}" for value in components)
    if not isinstance(selected, list) or not selected or selected[0] != "/ICCBased":
        return None
    # Convert the actual embedded profile, never assume an arbitrary ICC profile
    # is sRGB based on its channel count or descriptive name.
    from PIL import Image, ImageCms

    try:
        if space not in transforms:
            profile = selected[1].get_object()
            mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(profile.get("/N"))
            data = profile.get_data()
            transforms[space] = None if not mode or len(data) > 1024 * 1024 else (
                mode, ImageCms.buildTransformFromOpenProfiles(
                    ImageCms.ImageCmsProfile(BytesIO(data)), ImageCms.createProfile("sRGB"),
                    mode, "RGB", renderingIntent=1,
                ),
            )
        if transforms[space] is None:
            return None
        mode, transform = transforms[space]
        if len(components) != Image.getmodebands(mode):
            return None
        values = tuple(round(value * 255) for value in components)
        source = Image.new(mode, (1, 1), values[0] if mode == "L" else values)
        converted = ImageCms.applyTransform(source, transform).getpixel((0, 0))
        return "".join(f"{value:02X}" for value in converted)
    except (OSError, ValueError, TypeError, KeyError, IndexError, ImageCms.PyCMSError):
        transforms[space] = None
        return None


def _font_geometry(cm, tm, size, unit):
    """Native em axes remain measurable even when no single Word size fits."""
    values = [_number(value) for value in [*cm[:4], *tm[:4], size, unit]]
    if None in values or values[-1] <= 0 or values[-2] <= 0:
        return None
    a, b, c, d, e, f, g, h, size, unit = values
    x = (e * a + f * c, e * b + f * d)
    y = (g * a + h * c, g * b + h * d)
    sx, sy = math.hypot(*x), math.hypot(*y)
    if not sx or not sy:
        return None
    cosine = max(-1, min(1, (x[0] * y[0] + x[1] * y[1]) / (sx * sy)))
    return size * sx * unit, size * sy * unit, math.degrees(math.acos(cosine))


def _font_size(cm, tm, size, unit):
    """Both matrices can scale the text; rotation alone does not change its size."""
    geometry = _font_geometry(cm, tm, size, unit)
    if geometry is None:
        return None
    horizontal, vertical, angle = geometry
    # A stretched or skewed font has no single equivalent Word font size.
    if not math.isclose(horizontal, vertical, rel_tol=0.01) or abs(math.cos(math.radians(angle))) > 0.01:
        return None
    return round(vertical, 2)


def _page_geometry(page):
    unit = _number(page.get("/UserUnit", 1))
    rotation = _number(page.get("/Rotate", 0))
    if not unit or unit <= 0 or rotation is None or rotation % 90:
        return None, unit
    media, crop = page.mediabox, page.cropbox
    values = [_number(value) for value in [*media, *crop]]
    if None in values:
        return None, unit
    ml, mb, mr, mt, cl, cb, cr, ct = values
    width, height = (min(mr, cr) - max(ml, cl)), (min(mt, ct) - max(mb, cb))
    if width <= 0 or height <= 0:
        return None, unit
    if rotation % 180:
        width, height = height, width
    return (round(width * unit * 25.4 / 72, 2), round(height * unit * 25.4 / 72, 2)), unit


def _simple_text_colors(page):
    """Measure native opaque text colors, converting embedded ICC profiles safely."""
    contents = page.get_contents()
    resources = page.get("/Resources", {})
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    xobjects = resources.get("/XObject", {})
    xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
    stack, colors = [], set()
    state = {"fill": "000000", "fill_space": "/DeviceGray", "stroke": "000000", "stroke_space": "/DeviceGray", "mode": 0, "opaque": True}
    unknown, forms, transforms, strokes = False, False, {}, Counter()
    for operands, operator in contents.operations if contents is not None else []:
        if operator == b"q":
            stack.append(state.copy())
        elif operator == b"Q":
            if stack:
                state = stack.pop()
            else:
                unknown = True
        elif operator.lower() in {b"g", b"rg", b"k", b"sc", b"scn"}:
            kind = "fill" if operator.islower() else "stroke"
            lower = operator.lower()
            space = {b"g": "/DeviceGray", b"rg": "/DeviceRGB", b"k": "/DeviceCMYK"}.get(lower, state[kind + "_space"])
            if lower in {b"g", b"rg", b"k"}:
                state[kind + "_space"] = space
            components = [_number(value) for value in operands]
            state[kind] = _rgb(components, space, resources, transforms)
        elif operator in {b"cs", b"CS"}:
            kind = "fill" if operator == b"cs" else "stroke"
            state[kind + "_space"] = str(operands[0]) if operands else None
            state[kind] = None
        elif operator == b"Tr":
            state["mode"] = int(operands[0]) if operands else None
        elif operator == b"gs":
            # A graphics state can change opacity, blend mode, soft masks and fonts.
            state["opaque"] = False
        elif operator == b"Do" and operands:
            target = xobjects.get(operands[0])
            if target is not None and target.get_object().get("/Subtype") == "/Form":
                forms = unknown = True
        elif operator in {b"Tj", b"TJ", b"'", b'"'}:
            if state["mode"] in {3, 7}:  # Invisible/clip-only text has no visible color.
                continue
            if not state["opaque"] or state["mode"] not in {0, 1, 2, 4, 5, 6}:
                unknown = True
                continue
            if state["mode"] in {0, 2, 4, 6}:
                if state["fill"] is None:
                    unknown = True
                else:
                    colors.add(state["fill"])
            if state["mode"] in {1, 2, 5, 6}:
                if state["stroke"] is None:
                    unknown = True
                else:
                    strokes[state["stroke"]] += 1
    return colors, unknown, forms, strokes


def _dominant(counter, total):
    value, weight = counter.most_common(1)[0] if counter else (None, 0)
    return value if total and weight / total >= 0.6 else None


def measure_pdf_layout(path):
    """Return executable native measurements and auditable coverage observations.

    Text coordinates are not document margins, and untagged PDF runs are not
    paragraphs or heading levels. Those semantics remain with full-text analysis.
    """
    reader = PdfReader(path)
    if len(reader.pages) > MAX_PAGES:
        raise SourceFormatError(f"PDF 超过 {MAX_PAGES} 页，请拆分文件")
    page_formats, fonts, latin_fonts, east_asia_fonts, sizes = (Counter() for _ in range(5))
    characters = latin_characters = east_asia_characters = runs = 0
    colors, color_unknown, form_pages, unknown_size_chars = set(), False, 0, 0
    font_styles, font_style_pages, text_strokes = Counter(), defaultdict(set), Counter()
    transforms, transform_pages = Counter(), defaultdict(set)
    pages_without_text, failed_pages, geometry_unknown = [], [], []
    for page_number, page in enumerate(reader.pages, 1):
        geometry, unit = _page_geometry(page)
        if geometry:
            page_formats[geometry] += 1
        else:
            geometry_unknown.append(page_number)
        page_colors, unknown, has_forms, strokes = _simple_text_colors(page)
        text_strokes.update(strokes)
        colors.update(page_colors)
        color_unknown |= unknown
        form_pages += has_forms
        page_characters = 0

        def visitor(text, cm, tm, font, font_size, *, has_forms=has_forms, unit=unit, page_number=page_number):
            nonlocal characters, latin_characters, east_asia_characters, runs, page_characters, unknown_size_chars
            text = "".join(text.split())
            count = len(text)
            if not count:
                return
            runs += 1
            characters += count
            page_characters += count
            cjk = len(re.findall(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U000323af]", text))
            latin = len(re.findall(r"[A-Za-z0-9]", text))
            east_asia_characters += cjk
            latin_characters += latin
            name = _font_name(font)
            if name:
                fonts[name] += count
                east_asia_fonts[name] += cjk
                latin_fonts[name] += latin
                traits = (name, *_font_traits(font, name))
                font_styles[traits] += count
                font_style_pages[traits].add(page_number)
            # pypdf's nested-form visitor matrices do not include the caller's
            # complete transform; never promote those sizes to Word parameters.
            size = None if has_forms else _font_size(cm, tm, font_size, unit)
            if size:
                sizes[size] += count
            else:
                geometry = None if has_forms else _font_geometry(cm, tm, font_size, unit)
                if geometry is None:
                    unknown_size_chars += count
                else:
                    transform = (name, *(round(value, 3) for value in geometry))
                    transforms[transform] += count
                    transform_pages[transform].add(page_number)

        try:
            page.extract_text(visitor_text=visitor)
        except (ValueError, TypeError, KeyError, IndexError):
            failed_pages.append(page_number)
        if not page_characters:
            pages_without_text.append(page_number)

    body, layout, observations = {}, {}, []
    for key, counter, total in (
        ("font_name", latin_fonts if latin_characters else fonts, latin_characters or characters),
        ("east_asia_font", east_asia_fonts, east_asia_characters),
        ("font_size_pt", sizes, characters),
    ):
        value = _dominant(counter, total)
        if key in {"font_name", "east_asia_font"}:
            # Unknown PostScript/internal names remain observations, never Word
            # family settings. Known regular faces map to their family names.
            value = FONT_FAMILIES.get(value)
        if value is not None:
            try:
                ParagraphStyle.model_validate({key: value})
            except ValueError:
                continue
            body[key] = value
    if len(colors) == 1 and not color_unknown and characters:
        body["color"] = next(iter(colors))
    if body:
        layout["body"] = body
    geometry = _dominant(page_formats, len(reader.pages))
    if geometry:
        page_layout = dict(zip(("width_mm", "height_mm"), geometry, strict=True))
        try:
            PageStyle.model_validate(page_layout)
        except ValueError:
            pass
        else:
            layout["page"] = page_layout
    if fonts:
        observations.append({
            "dimension": "typography",
            "text": "原生文字字体（按非空白字符计数，已去除嵌入子集前缀）：" + "；".join(
                f"{name} {count} 字符" for name, count in fonts.most_common(12)
            ) + "。这是 PDF 字体名称，不代表本机已安装。",
        })
    emphasis = [(traits, count) for traits, count in font_styles.most_common() if traits[1] or traits[2]]
    if emphasis:
        observations.append({
            "dimension": "typography",
            "text": "原生字体名称/描述符确认的强调字形：" + "；".join(
                f"{name}（{'粗体' if bold else ''}{'斜体' if italic else ''}，{count} 字符，第 "
                + "、".join(map(str, sorted(font_style_pages[(name, bold, italic)]))) + " 页）"
                for (name, bold, italic), count in emphasis
            ) + "。这是实际字形使用分布，包含标题、目录编号等内容，不能直接设为全篇正文粗体或斜体。",
        })
    if sizes:
        observations.append({
            "dimension": "typography",
            "text": "组合文本矩阵、页面矩阵与 UserUnit 后的字号：" + "；".join(
                f"{size:g} pt（{count} 字符）" for size, count in sizes.most_common(12)
            ) + "。全局参数仅采用至少占可提取文字 60% 的值；标题层级不由字号自动认定。",
        })
    if transforms:
        observations.append({
            "dimension": "typography",
            "text": "已识别的非等比/倾斜字形（原生 em 坐标轴长度，不是字符包围框）：" + "；".join(
                f"{name or '未知字体'} 横轴 {horizontal:g} pt、纵轴 {vertical:g} pt、夹角 {angle:g}°，{count} 字符，第 "
                + "、".join(map(str, sorted(transform_pages[(name, horizontal, vertical, angle)]))) + " 页"
                for (name, horizontal, vertical, angle), count in transforms.most_common()
            ) + "。这些变换可由程序直接读取，无需多模态；未强制转换为单一 Word 字号。",
        })
    if page_formats:
        observations.append({
            "dimension": "page",
            "text": "可见页面尺寸（CropBox 与 MediaBox 交集，已计入旋转与 UserUnit）：" + "；".join(
                f"{width:g} × {height:g} mm，共 {count} 页" for (width, height), count in page_formats.most_common()
            ) + "。该尺寸不是页边距。",
        })
    if colors:
        observations.append({
            "dimension": "visual",
            "text": "可明确读取的原生文字填充色（嵌入 ICC 配置经色彩转换后以 sRGB 表示）：" + "、".join("#" + color for color in sorted(colors))
            + ("；另有未确认的复杂文字渲染，不能据此认定全篇主色。" if color_unknown else "；仅描述文字，不代表图形或页面背景色。"),
        })
    if text_strokes:
        observations.append({
            "dimension": "typography",
            "text": "原生文字描边：" + "；".join(f"#{color}，{count} 次文字绘制" for color, count in text_strokes.most_common())
            + "。描边会影响文字粗细，属于 PDF 绘制方式，不直接等同于 Word 全文加粗。",
        })
    tagged = reader.trailer["/Root"].get("/StructTreeRoot") is not None
    if not tagged:
        observations.extend([
            {"dimension": "typography", "text": "源格式语义：该 PDF 没有段落结构标记，未保存可直接恢复的 Word 行距规则、段前段后、首行缩进与段落对齐属性；文字位置只能说明实际显示效果，不能冒充原始参数。这属于源文件缺少编辑语义，并非必须使用多模态才能提取。"},
            {"dimension": "page", "text": f"分页观测：原生页对象共 {len(reader.pages)} 页，实际分页已确认；PDF 未保存 Word 手动分页符、分节和页边距设置，不能由文字包围框精确还原，也不能通过页面图像恢复原始设置。"},
        ])
    limitations = [
        "PDF 没有可直接复用的 Word 段落/页边距参数；文字坐标不等于段落缩进、行距或页边距。",
        "未标注 PDF 的段落、标题层级与表格数量不能从文本块数量直接确认；应结合全文内容描述组织方式。",
        "图片内部字体、图形美术设计和视觉布局的精确复刻需要页面图像分析。",
    ]
    if color_unknown:
        limitations.append("部分文字颜色的配置不可转换，或涉及透明度/嵌套图形，未强行换算为全局 RGB 文字颜色。")
    if form_pages:
        limitations.append(f"{form_pages} 页含嵌套 Form 图形；其文字字号暂不采用，避免遗漏外层变换导致错误。")
    if transforms:
        limitations.append(f"{sum(transforms.values())} 个字符已识别为非等比/倾斜字形，横纵 em 轴长度及夹角已记录，未转为单一 Word 字号。")
    if unknown_size_chars:
        limitations.append(f"{unknown_size_chars} 个字符缺少有效完整矩阵或涉及未组合的嵌套变换，字号仍无法可靠换算。")
    if pages_without_text:
        limitations.append("以下页面未读取到原生文字，可能需要图像识别：" + "、".join(map(str, pages_without_text)) + "。")
    if failed_pages:
        limitations.append("以下页面原生文字测量失败：" + "、".join(map(str, failed_pages)) + "。")
    if geometry_unknown:
        limitations.append("以下页面的裁切/旋转/单位不合法，未采用页面尺寸：" + "、".join(map(str, geometry_unknown)) + "。")
    return layout, {
        "complete": not failed_pages and not geometry_unknown,
        "pages_total": len(reader.pages), "pages_measured": len(reader.pages) - len(failed_pages),
        "geometry_pages_measured": len(reader.pages) - len(geometry_unknown),
        "geometry_unmeasured_pages": geometry_unknown,
        "text_runs_measured": runs, "characters_measured": characters,
        "fonts": [{"name": name, "characters": count} for name, count in fonts.most_common()],
        "font_styles": [{"name": name, "bold": bold, "italic": italic, "characters": count, "pages": sorted(font_style_pages[(name, bold, italic)])} for (name, bold, italic), count in font_styles.most_common()],
        "text_strokes": [{"color": color, "draw_operations": count} for color, count in text_strokes.most_common()],
        "has_structural_tags": tagged,
        "font_sizes": [{"size_pt": size, "characters": count} for size, count in sizes.most_common()],
        "font_transforms": [{"name": name, "horizontal_em_pt": horizontal, "vertical_em_pt": vertical, "axis_angle_degrees": angle, "characters": count, "pages": sorted(transform_pages[(name, horizontal, vertical, angle)])} for (name, horizontal, vertical, angle), count in transforms.most_common()],
        "unknown_font_size_characters": unknown_size_chars,
        "page_formats": [{"width_mm": width, "height_mm": height, "pages": count} for (width, height), count in page_formats.most_common()],
        "pages_without_text": pages_without_text, "failed_pages": failed_pages,
        "observations": observations, "limitations": limitations,
    }
