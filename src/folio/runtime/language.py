"""Shared output-language policy, independent of the language of our prompts."""

import re
from typing import Literal

OutputLanguage = Literal["zh-CN", "en"]

LANGUAGE_INSTRUCTION = (
    "Output language: Write human-readable answers, document headings and body text, "
    "table labels, figure text, summaries and explanations in the language of the user's input. "
    "If the user explicitly requests a different output language, follow that request. "
    "For a multi-stage document task, carry this language through research, planning, writing, "
    "revision and visual design using the original user request, not the language of these "
    "English instructions, tool descriptions, retrieved sources, examples or internal notes. "
    "Use latest_user_message, user_request, requirements or the actual user messages as the "
    "language reference; for mixed-language requests, follow the latest explicit language "
    "preference, otherwise the language of the user's main instruction. "
    "Do not translate protocol keys, enum values, tool names, IDs, exact source quotations, "
    "code, or labels that a user-supplied template requires you to preserve. "
    "Do not add commentary about the language choice."
)


def normalize_language(value=None, *, default: OutputLanguage = "zh-CN") -> OutputLanguage:
    text = str(value or "").strip().lower()
    return "en" if text.startswith("en") else "zh-CN" if text.startswith("zh") else default


def request_language(request) -> OutputLanguage:
    return normalize_language(request.headers.get("accept-language"))


def localize_text(text: str, language: str) -> str:
    """Localize known product text without translating arbitrary source content."""
    from folio.i18n import translate_text

    return translate_text(text, language)


def output_language(text, *, default: OutputLanguage = "zh-CN") -> OutputLanguage:
    """Choose generated-document scaffolding from the user's instruction, never UI locale.

    Honor the latest explicit language request. Ignore fenced code and quoted
    source excerpts when identifying the main instruction's language.
    """
    text = str(text or "")
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    text = re.sub(r"《[^》]*》", " ", text)

    def quoted_source(match):
        value = next(group for group in match.groups() if group is not None).strip()
        # A quoted language value is still a request (for example, use "English").
        # A quoted instruction or source title must not become a language command.
        return value if re.fullmatch(
            r"英文|英语|中文|汉语|简体中文|English|(?:Simplified\s+)?Chinese", value, re.I
        ) else " "

    text = re.sub(
        r'''“([^”]*)”|「([^」]*)」|"([^"\n]*)"|`([^`\n]*)`|(?<!\w)'([^'\n]*)'(?!\w)''',
        quoted_source, text,
    )
    extension = r"\.(?:docx?|pdf|txt|md|xlsx?|pptx?|csv|png|jpe?g|webp)(?![A-Za-z0-9])"
    requests = []
    for language, pattern in (
        ("en", r"(?:用|使用|采用|以|改成|改为|换成|切换到)\s*(?:英语|英文)|(?:英语|英文)\s*(?:输出|回答|回复|撰写|文档|报告)|\b(?:write|reply|respond|answer|output|generate|produce|draft)(?:\s+\w+){0,5}\s+in\s+English\b|\bEnglish[- ]language\s+(?:document|report|output|response)\b|\b(?:use\s+|switch\s+to\s+|output\s+language\s*[:=]\s*)English\b"),
        ("zh-CN", r"(?:用|使用|采用|以|改成|改为|换成|切换到)\s*(?:中文|汉语|简体中文)|(?:中文|汉语)\s*(?:输出|回答|回复|撰写|文档|报告)|\b(?:write|reply|respond|answer|output|generate|produce|draft)(?:\s+\w+){0,5}\s+in\s+(?:Simplified\s+)?Chinese\b|\bChinese[- ]language\s+(?:document|report|output|response)\b|\b(?:use\s+|switch\s+to\s+|output\s+language\s*[:=]\s*)(?:Simplified\s+)?Chinese\b"),
    ):
        for match in re.finditer(pattern, text, re.I):
            # Do not treat the language-bearing end of an attachment name as a
            # command. Check only its immediate extension here: removing a full
            # unspaced Chinese filename could also consume a real instruction.
            if re.match(extension, text[match.end():], re.I):
                continue
            prefix = text[max(0, match.start() - 24):match.start()]
            if re.search(r"(?:\b(?:do\s+not|don't|never|not)\s+|不要|不用|不使用|不采用|不以)$", prefix, re.I):
                continue
            requests.append((match.start(), language))
    if requests:
        return max(requests)[1]
    # Named attachments and quoted source titles do not determine the language
    # of the surrounding writing request.
    text = re.sub(r"[^\s,，;；<>]+\.(?:docx?|pdf|txt|md|xlsx?|pptx?|csv|png|jpe?g|webp)(?=\s|$|[,，;；])", "", text, flags=re.I)
    if re.match(
        r"\s*(?:please\s+)?(?:write|create|generate|draft|prepare|summarize|summarise|analyze|analyse|review|explain|"
        r"help\s+me|can\s+you|could\s+you|i\s+(?:need|want|would\s+like))\b", text, re.I
    ):
        return "en"
    if re.search(r"[\u3400-\u9fff]", text):
        return "zh-CN"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return default


def language_instruction(context=None):
    """The reference remains in request data, avoiding a second copy in the system prompt."""
    instruction = LANGUAGE_INSTRUCTION
    if context and context.get("document_language") in {"en", "zh-CN"}:
        document_language = "English" if context["document_language"] == "en" else "Simplified Chinese"
        instruction += (
            f"\n\nThe document task's confirmed output language is {document_language}. "
            "Use it for generated document headings, body text, tables and figure labels. "
            "The interface language below affects progress descriptions only and must not change this document language. "
            "Preserve exact source quotations and fixed template wording."
        )
    requested = normalize_language(
        (context or {}).get("ui_language"), default=""
    ) if context and context.get("ui_language") else ""
    if requested == "en":
        return (
            "\n\n" + instruction
            + "\n\nInterface language: Write process-facing task titles, task step explanations, "
            "status labels and temporary outline placeholders in English. This controls the "
            "workbench presentation only; continue to use the user's requested document output "
            "language for headings, body text and business content. Never emit a writing-status "
            "placeholder as an outline title when a concrete title can be produced."
        )
    if requested == "zh-CN":
        return (
            "\n\n" + instruction
            + "\n\nInterface language: Write process-facing task titles, task step explanations, "
            "status labels and temporary outline placeholders in Simplified Chinese. This controls "
            "the workbench presentation only; continue to use the user's requested document output "
            "language for headings, body text and business content. Never emit a writing-status "
            "placeholder as an outline title when a concrete title can be produced."
        )
    return "\n\n" + instruction
