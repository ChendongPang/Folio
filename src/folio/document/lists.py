"""Render prose lists with separate parent/child labels and hanging indentation.

Legacy model prose keeps its explicit numbers; only an explicit child marker or a
numbered introduction followed by a restart at 1 establishes a nested list.
Heading numbers, decimal values and business text are not renumbered.
"""

import re
from copy import deepcopy

from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from folio.document.typography import prop, resolved_format

ITEM = re.compile(r"^(?:([1-9]\d*)[.、．](?!\d)|[（(]([1-9]\d*)[）)])\s*(\S.*)$", re.S)


class ProseListLayout:
    def __init__(self):
        self.reset()

    def reset(self):
        self.numbers = []
        self.introduces_children = False
        self.anchors = {}
        self.split_parent = None
        self.explicit_children = False
        self.groups = {}
        self.label_widths = {}

    def apply(self, paragraph, text, block_id):
        match = ITEM.match(text)
        if not match:
            self.reset()
            return
        number = int(match[1] or match[2])
        root = re.sub(r"_p[1-9]\d*$", "", block_id)
        # normalize_content retains the original block ID on an introduction and
        # gives its split paragraphs _pN suffixes. Use that relationship only when
        # the original introduction actually ends with a colon.
        split_level = self.anchors.get(root) if root != block_id else None
        if split_level is not None:
            level = split_level + 1
            self.split_parent = root
        elif match[2]:
            level = 1
            self.split_parent = None
            self.explicit_children = True
        elif self.explicit_children:
            level = 0
            self.explicit_children = False
        elif self.split_parent is not None:
            level = self.anchors[self.split_parent]
            self.split_parent = None
        elif self.numbers and self.introduces_children and number == 1:
            level = len(self.numbers)
        else:
            level = max(0, len(self.numbers) - 1)
            # A return to the parent's next number ends a legacy nested sequence.
            # If the two interpretations coincide, retain the current level.
            if self.numbers and number != self.numbers[-1] + 1:
                for parent in range(len(self.numbers) - 2, -1, -1):
                    if number == self.numbers[parent] + 1:
                        level = parent
                        break
        self.numbers = (self.numbers + [0] * (level + 1))[: level + 1]
        self.numbers[level] = number
        self.introduces_children = text.rstrip().endswith((":", "："))
        if self.introduces_children:
            self.anchors[block_id] = level
        label = f"{number}." if level == 0 else f"（{number}）"
        # Preserve the document font and spacing. Only replace the list label and
        # paragraph indentation; do not use document body first-line indentation.
        document = paragraph.part.document
        _, font = resolved_format(document, paragraph._p)
        sizes = font.xpath("./w:sz/@w:val")
        points = int(sizes[0]) / 2 if sizes else 11
        original_font = deepcopy(paragraph.runs[0]._r.rPr) if paragraph.runs else None
        tabs = self._inherited_tabs(paragraph)
        # Right-align the label, then use a separate small, fixed gap. A single
        # left tab after '1.' reserved four full characters and produced a large
        # hole, especially for single digits. The second stop also aligns wraps.
        paragraph.text = "\t" + label + "\t" + match[3]
        if original_font is not None:
            paragraph.runs[0]._r.insert(0, original_font)
        label_width = (0.65 * len(str(number)) + (2 if level else 0.35)) * points
        label_width = max(1.2 * points, label_width)
        group = self.groups.setdefault(level, [])
        group.append((paragraph, points, tabs))
        previous_width = self.label_widths.get(level, 0)
        self.label_widths[level] = max(previous_width, label_width)
        # A later 10./100. widens this list's label column once, including earlier
        # items, so single/double/triple-digit items retain one body alignment.
        for item, font_points, inherited in group if label_width > previous_width else group[-1:]:
            self._indent(item, level, font_points, self.label_widths[level], inherited)

    @staticmethod
    def _inherited_tabs(paragraph):
        document = paragraph.part.document
        positions = set(
            document.styles.element.xpath("./w:docDefaults/w:pPrDefault/w:pPr/w:tabs/w:tab/@w:pos")
        )
        positions.update(paragraph._p.xpath("./w:pPr/w:tabs/w:tab/@w:pos"))
        style, seen = paragraph.style, set()
        while style is not None and style.style_id not in seen:
            seen.add(style.style_id)
            positions.update(style.element.xpath("./w:pPr/w:tabs/w:tab/@w:pos"))
            style = style.base_style
        return positions

    @staticmethod
    def _indent(paragraph, level, points, label_width, inherited_tabs):
        width = label_width + 0.4 * points
        left = level * 2 * points + width
        ppr = paragraph._p.get_or_add_pPr()
        ind = prop(ppr, "ind")
        for key in (
            "leftChars",
            "startChars",
            "firstLineChars",
            "hangingChars",
            "firstLine",
            "start",
        ):
            ind.attrib.pop(qn("w:" + key), None)
        paragraph.paragraph_format.left_indent = Pt(left)
        paragraph.paragraph_format.first_line_indent = Pt(-width)
        # Templates can inherit character-based indents, which override twips in
        # Word/WPS. Override BOTH representations with equivalent values.
        prop(
            ppr,
            "ind",
            leftChars=round(left / points * 100),
            firstLineChars=0,
            hangingChars=round(width / points * 100),
        )
        paragraph.paragraph_format.tab_stops.clear_all()
        # Removing local tabs alone resurrects stops inherited from the base styles.
        # Explicit clear entries suppress them without changing document styles.
        tab_nodes = prop(ppr, "tabs")
        for position in sorted(inherited_tabs, key=int):
            if int(position) in {Pt(left - 0.4 * points).twips, Pt(left).twips}:
                continue
            clear = OxmlElement("w:tab")
            clear.set(qn("w:val"), "clear")
            clear.set(qn("w:pos"), position)
            tab_nodes.append(clear)
        paragraph.paragraph_format.tab_stops.add_tab_stop(
            Pt(left - 0.4 * points), WD_TAB_ALIGNMENT.RIGHT
        )
        paragraph.paragraph_format.tab_stops.add_tab_stop(Pt(left), WD_TAB_ALIGNMENT.LEFT)
        ppr.get_or_add_numPr().get_or_add_numId().val = 0
