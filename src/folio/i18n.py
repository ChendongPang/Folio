"""Translate application-owned text without translating source or user content.

Catalog keys are the historical Chinese messages. Numbered or named braces
denote interpolated values; the same catalogs also localize English diagnostics
for Chinese readers. Unknown strings are preserved, never replaced by a generic
status that would discard useful information.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from folio.runtime.language import normalize_language

_SLOT = re.compile(r"\{([\w]+)\}")


@lru_cache(maxsize=1)
def catalog():
    root = Path(__file__).with_name("locales")
    result = {}
    for path in sorted(root.glob("*.json"), key=lambda p: (p.name != "interface.json", p.name)):
        result.update(json.loads(path.read_text(encoding="utf-8")))
    return result


def _pattern(template):
    parts, names, quoted, cursor = [], [], set(), 0
    for match in _SLOT.finditer(template):
        parts.append(re.escape(template[cursor:match.start()]))
        name = match.group(1)
        if match.start() and match.end() < len(template):
            if (template[match.start() - 1], template[match.end()]) in {
                ("《", "》"), ("“", "”"), ('"', '"'), ("'", "'"), ("`", "`"),
            }:
                quoted.add(name)
        if name in names:
            parts.append(f"(?P=s{names.index(name)})")
        else:
            parts.append(f"(?P<s{len(names)}>.*?)")
            names.append(name)
        cursor = match.end()
    parts.append(re.escape(template[cursor:]))
    return re.compile("".join(parts), re.DOTALL), names, quoted


@lru_cache(maxsize=2)
def _translations(language):
    items = list(catalog().items())
    if language == "zh-CN":
        items = [(en, zh) for zh, en in items]
    exact = {source: target for source, target in items if not _SLOT.search(source)}
    templates = []
    for source, target in items:
        if _SLOT.search(source):
            # Try the most specific sentence before short fragments.
            literal = _SLOT.sub("", source)
            if len(literal.strip()) >= 2:
                pattern, names, quoted = _pattern(source)
                templates.append((len(literal), pattern, names, quoted, target))
    templates.sort(key=lambda item: -item[0])
    return exact, templates


def translate_text(text, language="zh-CN", *, _depth=0):
    if not isinstance(text, str) or not text or _depth > 6:
        return text
    language = normalize_language(language)
    exact, templates = _translations(language)
    if text in exact:
        return exact[text]
    for _, pattern, names, quoted, target in templates:
        match = pattern.fullmatch(text)
        if match:
            values = {name: match.group(f"s{index}") if name in quoted else translate_text(match.group(f"s{index}"), language, _depth=_depth + 1)
                      for index, name in enumerate(names)}
            return _SLOT.sub(lambda slot, values=values: values.get(slot.group(1), slot.group()), target)
    # Product messages often compose complete sentences or markdown bullets.
    # Match each complete fragment, not arbitrary substrings in source prose.
    parts = re.split(r"(\n|(?<=。)|(?<=；))", text)
    if len(parts) > 1:
        translated = [translate_text(part, language, _depth=_depth + 1) if part.strip() else part
                      for part in parts]
        result = ""
        previous = ""
        for original, localized in zip(parts, translated, strict=True):
            if not original:
                continue
            if (language == "en" and result and localized and not localized[0].isspace()
                    and previous.endswith(("。", "；")) and result.endswith((".", ";", "!", "?"))):
                result += " "
            result += localized
            previous = original
        return result
    if text.startswith("Value error, "):
        return "Value error, " + translate_text(text[13:], language, _depth=_depth + 1)
    for prefix in ("- ", "# ", "## ", "### "):
        if text.startswith(prefix):
            return prefix + translate_text(text[len(prefix):], language, _depth=_depth + 1)
    return text


_DISPLAY_FIELDS = frozenset({
    "text", "content", "message", "msg", "detail", "error", "reason", "summary", "label",
    "stage_label", "action_label", "activity_label", "description", "unresolved", "warnings",
    "limitations", "validation_issues", "confirmation_issues", "preserved_progress", "notes",
    "failure_reason", "status_text", "hint", "instructions", "notice", "progress_note",
    "layout_note", "purpose_note", "issues",
})
_SOURCE_FIELDS = frozenset({
    "input", "inputs", "requirements", "requirement_sources", "user_request", "request", "draft",
    "advice", "answer", "answers", "evidence", "source_text", "quotation", "quote", "excerpts",
    "blocks", "sections", "plan", "facts", "record", "schema", "messages_raw", "raw_response",
})


def localize_payload(value, language="zh-CN", *, event=None):
    """Create a display copy and reversible translations; never mutate persistence."""
    language = normalize_language(language)
    existing = value.get("_translations", {}) if isinstance(value, dict) else {}
    pairs = {original: translated for original, translated in existing.items()
             if isinstance(original, str) and isinstance(translated, str)} if isinstance(existing, dict) else {}
    originals = {translated: original for original, translated in pairs.items()}

    def text(source):
        english = pairs.get(source) or translate_text(source, "en")
        chinese = originals.get(source) or translate_text(source, "zh-CN")
        if chinese != english:
            pairs[chinese] = english
        return english if language == "en" else chinese

    def visit(item, key="", protected=False):
        if protected:
            return item
        if isinstance(item, str):
            return text(item) if key in _DISPLAY_FIELDS else item
        if isinstance(item, list):
            return [visit(child, key) for child in item]
        if not isinstance(item, dict):
            return item
        user_message = item.get("role") == "user"
        model_content = item.get("content_origin") == "model"
        # Historical assistant prose has no reliable language provenance.
        # Only explicit system messages or structured failures may be translated.
        if (item.get("role") == "assistant" and item.get("content_origin") is None
                and not item.get("error_code") and not item.get("error_diagnostics")):
            model_content = True
        result = {}
        for name, child in item.items():
            if name == "_translations":
                continue
            if name == "title" and item.get("title_is_default") is True and isinstance(child, str):
                result[name] = text(child)
                continue
            is_source = name in _SOURCE_FIELDS or ((user_message or model_content) and name in {"text", "content"})
            result[name] = visit(child, name, is_source)
        return result

    # Model streaming prose is controlled by the user's requested output
    # language. It must not be translated by the workbench preference.
    if event == "message.delta":
        return value
    result = visit(value)
    if pairs and isinstance(result, dict):
        result["_translations"] = pairs
    return result
