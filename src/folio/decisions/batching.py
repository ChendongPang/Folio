"""Lossless relevance pages with a shared character budget for either decision backend.

The limit counts the serialized context, including its shared purpose and caller-added
fields. Provider instructions, questions and response schemas have separate overhead.
Partial sources retain their outer reference: merge by that reference and keep the
complete original when any fragment passes the active relevance selection policy.
"""

import json
from copy import deepcopy

DECISION_CONTEXT_CHARS = 600000

_SOURCE_FIELDS = (
    "source_id",
    "evidence_id",
    "fact_id",
    "gap_id",
    "document_id",
    "source_uri",
    "url",
    "path",
    "page",
    "page_number",
    "title",
)


class DecisionBatchingError(ValueError):
    """A shared core or indivisible item cannot fit without changing its meaning."""

    def __init__(self, task, reason, *, context_ref=None):
        self.task = task
        self.reason = reason
        self.context_ref = context_ref
        suffix = f"; context_ref={context_ref}" if context_ref is not None else ""
        super().__init__(f"{task}: cannot safely batch decision input ({reason}){suffix}")


def context_chars(context):
    """Match the original routing limit: Unicode characters, default JSON spacing."""
    return len(json.dumps(context, ensure_ascii=False))


def _fragment_value(item):
    task, ref = "select_context", item.get("context_ref")
    if "value" not in item:
        raise DecisionBatchingError(task, "missing_fragmentable_value", context_ref=ref)
    parent = item.get("fragment")
    if parent is not None and (
        not isinstance(parent, dict)
        or parent.get("partial") is not True
        or not isinstance(item["value"], str)
    ):
        raise DecisionBatchingError(task, "invalid_fragment_metadata", context_ref=ref)
    original = item["value"]
    encoding = "text" if isinstance(original, str) else "json"
    text = original if encoding == "text" else json.dumps(original, ensure_ascii=False)
    metadata = {"partial": True, "encoding": encoding, "original_chars": len(text)}
    if parent is not None:
        # A serving model can reject a previously bounded fragment. Keep
        # its lineage without pretending the new string is a whole source.
        metadata["parent"] = deepcopy(parent)
        if parent.get("source"):
            metadata["source"] = deepcopy(parent["source"])
    if isinstance(original, dict):
        source = {key: original[key] for key in _SOURCE_FIELDS if key in original}
        if source:
            metadata["source"] = source

    def part(value, index, count):
        return deepcopy(item) | {
            "value": value,
            "fragment": deepcopy(metadata) | {"index": index, "count": count},
        }

    return text, part


def bisect_catalog_item(item):
    """Exactly two lossless value pieces; callers must measure both full requests."""
    text, part = _fragment_value(item)
    if len(text) < 2:
        raise DecisionBatchingError(
            "select_context", "value_cannot_be_bisected", context_ref=item.get("context_ref")
        )
    middle = len(text) // 2
    return [part(text[:middle], 0, 2), part(text[middle:], 1, 2)]


def decision_pages(
    task, context, max_items=24, *, max_chars=DECISION_CONTEXT_CHARS, measure_chars=None
):
    """Return complete-item relevance pages, fragmenting only an oversized value.

    A fragmented item's ``value`` is a string. Concatenating it in ``fragment.index``
    order restores the original string (encoding ``text``), or the complete JSON
    representation of its original value (encoding ``json``). Source metadata outside
    ``value`` stays unchanged; common identifiers inside it accompany every fragment.
    No shared purpose, request, feedback, source field or character is truncated.
    ``measure_chars`` may count the complete provider input, including questions
    or system/schema overhead. The serialized context limit still applies.
    """
    if task != "select_context":
        raise DecisionBatchingError(task, "unsupported_task")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ValueError("max_items must be a positive integer")
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars < 1
    ):
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(context, dict) or not isinstance(context.get("catalog"), list):
        raise DecisionBatchingError(task, "catalog_must_be_a_list")
    catalog = context["catalog"]
    if any(not isinstance(item, dict) for item in catalog):
        raise DecisionBatchingError(task, "catalog_items_must_be_objects")
    base = deepcopy({key: value for key, value in context.items() if key != "catalog"})

    def page(items):
        return deepcopy(base) | {"catalog": deepcopy(items)}

    def size(candidate):
        chars = context_chars(candidate)
        return max(chars, measure_chars(candidate)) if measure_chars else chars

    if size(page([])) > max_chars:
        raise DecisionBatchingError(task, "shared_context_exceeds_capacity")

    def fragment(item):
        ref = item.get("context_ref")
        text, part = _fragment_value(item)

        # The number of nonempty fragments cannot exceed the original length.
        # Reserve that digit width before slicing; final indices can only be smaller.
        upper = max(1, len(text))
        if not text or size(page([part("", upper, upper)])) >= max_chars:
            raise DecisionBatchingError(task, "item_metadata_exceeds_capacity", context_ref=ref)
        chunks = []
        start = 0
        while start < len(text):
            low, high = 0, len(text) - start
            while low < high:
                length = (low + high + 1) // 2
                candidate = page([part(text[start : start + length], upper, upper)])
                if size(candidate) <= max_chars:
                    low = length
                else:
                    high = length - 1
            if not low:
                raise DecisionBatchingError(task, "item_metadata_exceeds_capacity", context_ref=ref)
            chunks.append(text[start : start + low])
            start += low
        return [part(chunk, index, len(chunks)) for index, chunk in enumerate(chunks)]

    # A balanced binary tree leaves four to seven items on many pages even when
    # eight fit. Fill each page in source order; split only an individually
    # oversized source, never discard or reorder it to fit another page.
    pages, pending = [], []
    for item in catalog:
        parts = [item] if size(page([item])) <= max_chars else fragment(item)
        for part in parts:
            if pending and (len(pending) >= max_items or size(page([*pending, part])) > max_chars):
                pages.append(page(pending))
                pending = []
            pending.append(part)
    if pending or not pages:
        pages.append(page(pending))
    return pages
