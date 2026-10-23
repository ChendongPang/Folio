"""Detect a stalled writer without accepting or repairing partial JSON."""

import json
import re

from folio.runtime.errors import ModelOutputRepetition


def outside_json_string(text):
    """Padding outside JSON strings is not user content; do not mistake escaped quotes."""
    quoted = escaped = False
    for char in text:
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
    return not quoted


class _ParagraphSequence:
    """Compare exact blocks within one uninterrupted prose scope, never edit them."""

    def __init__(self):
        self.previous = None
        self.consecutive = 0
        self.history = []
        self.scope = None

    def reset(self):
        self.previous = None
        self.consecutive = 0
        self.history.clear()
        self.scope = None

    def accept(self, block):
        if not isinstance(block, dict) or block.get("kind", "paragraph") != "paragraph":
            self.reset()
            return
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            self.reset()
            return
        # Usually scope is represented by a preceding heading. Also respect an
        # explicit scope in a transport or future block shape, without dropping
        # any fields from the exact signature except the generated identifier.
        scope = json.dumps(
            {key: block[key] for key in ("scope", "section_id") if key in block},
            ensure_ascii=False,
            sort_keys=True,
        )
        if self.scope != scope:
            self.reset()
            self.scope = scope
        signature = json.dumps(
            {key: value for key, value in block.items() if key != "block_id"},
            ensure_ascii=False,
            sort_keys=True,
        )
        significant_chars = len("".join(text.split()))
        self.consecutive = self.consecutive + 1 if signature == self.previous else 1
        self.previous = signature
        if significant_chars >= 80 and self.consecutive >= 3:
            raise ModelOutputRepetition()
        self.history.append((signature, significant_chars))
        self.history = self.history[-24:]  # Three cycles of at most eight paragraphs.
        for period in range(2, min(8, len(self.history) // 3) + 1):
            cycle = self.history[-period:]
            # Use the shortest exact period. Repeating short labels must not
            # become "substantial" merely by counting them twice in a cycle.
            if any(
                period % smaller == 0 and cycle == cycle[:smaller] * (period // smaller)
                for smaller in range(1, period)
            ):
                continue
            if sum(size for _, size in cycle) < 60:
                continue
            if self.history[-3 * period :] == cycle * 3:
                raise ModelOutputRepetition()


def validate_paragraph_cycles(value):
    """Reject a completed/cached writer loop without deleting or rewriting prose."""
    guard = _ParagraphSequence()
    for block in value.get("blocks", []):
        guard.accept(block)


class RepeatedParagraphGuard:
    def __init__(self):
        self.buffer = ""
        self.offset = None
        self.finished = False
        self.sequence = _ParagraphSequence()
        self.decoder = json.JSONDecoder()

    @property
    def consecutive(self):
        return self.sequence.consecutive

    def feed(self, text):
        if self.finished:
            return
        self.buffer += text
        if self.offset is None:
            match = re.search(r'(?<!\\)"blocks"\s*:\s*\[', self.buffer)
            if not match:
                return
            self.offset = match.end()
        while True:
            while self.offset < len(self.buffer) and self.buffer[self.offset] in " \r\n\t,":
                self.offset += 1
            if self.offset >= len(self.buffer):
                return
            if self.buffer[self.offset] == "]":
                self.finished = True
                return
            try:
                block, end = self.decoder.raw_decode(self.buffer, self.offset)
            except json.JSONDecodeError:
                return  # Wait for a whole block; incomplete output is never adopted.
            self.offset = end
            self.sequence.accept(block)
