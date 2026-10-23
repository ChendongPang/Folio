"""Readable Word detail pages reuse the original PNG, with overlapping viewports."""

import math

# Small headroom for integer EMU and Word's crop-percentage rounding.
MIN_FLOW_TEXT_PT = 10.1


def detail_views(item, max_width, max_height):
    """Pixel rectangles in reading order; the unchanged whole image remains first.

    Word dimensions are EMU. Each viewport's fit preserves at least 10pt text.
    Adjacent viewports overlap so connections can be followed across pages.
    """
    font = item.get("text_font_px")
    if item.get("kind") != "flowchart" or not isinstance(font, (int, float)) or font <= 0:
        return []
    width, height = item["width"], item["height"]
    minimum_scale = MIN_FLOW_TEXT_PT / (font * 0.75)
    max_px_width = max_width / 9525 / minimum_scale
    max_px_height = max_height / 9525 / minimum_scale
    if min(1, max_width / (width * 9525), max_height / (height * 9525)) >= minimum_scale:
        return []

    def intervals(total, limit):
        limit = math.floor(limit)
        if limit < 64:
            raise ValueError('The page is too narrow for a readable flowchart detail view.')
        if total <= limit:
            return [(0, total)]
        overlap = min(256, limit // 3)
        count = math.ceil((total - overlap) / (limit - overlap))
        step = (total - limit) / (count - 1)
        return [
            (math.floor(i * step), min(total, math.floor(i * step) + limit))
            for i in range(count - 1)
        ] + [(total - limit, total)]

    xs, ys = intervals(width, max_px_width), intervals(height, max_px_height)
    if len(xs) * len(ys) > 36:
        raise ValueError('The flowchart requires more than 36 enlarged views. Split the diagram instead of shrinking text to hide content.')
    return [
        {
            "box": (left, top, right, bottom),
            "row": row + 1,
            "column": col + 1,
            "rows": len(ys),
            "columns": len(xs),
        }
        for row, (top, bottom) in enumerate(ys)
        for col, (left, right) in enumerate(xs)
    ]


def word_crop(box, width, height):
    """Round outward: Word's 1/1000-percent crop cannot lose boundary pixels."""
    left, top, right, bottom = box
    return {
        "l": math.floor(left / width * 100000),
        "t": math.floor(top / height * 100000),
        "r": math.floor((width - right) / width * 100000),
        "b": math.floor((height - bottom) / height * 100000),
    }
