"""Bounded model views with explicit navigation back to the unchanged originals."""

from copy import deepcopy

from folio.research.ledger import ResearchLimitReached


def observation_page(item, max_chars):
    """Keep the latest result visible, with an exact archived-text continuation."""
    text = item.get("result", {}).get("model_text", item.get("model_text", ""))
    if len(text) <= max_chars:
        return item | {"model_text": text, "delivery_complete": True,
                       "delivered_text_chars": len(text), "total_text_chars": len(text)}
    # Reserve room for a complete continuation instruction; never emit partial
    # JSON or pretend a clipped paragraph was delivered in full.
    end = max(0, max_chars - 240)
    footer = (f"\n\nThis result shows only characters 0–{end}. Unshown content must not be treated as read.\n"
              f"Continue reading: fs_read(scope='history', request_id='{item['request_id']}', offset={end}, limit=2000)")
    return item | {"model_text": text[:end] + footer, "delivery_complete": False,
                   "delivered_text_chars": end, "total_text_chars": len(text)}


def read_text_page(text, *, offset=0, limit=600000, max_chars=600000):
    if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_chars):
        raise ValueError("Invalid context read range")
    end = min(len(text), offset + limit)
    return {"text": text[offset:end], "offset": offset, "total_chars": len(text),
            "next_offset": end if end < len(text) else None,
            "complete": end >= len(text)}


def page_evidence_spans(spans, offset, text_chars):
    """Project trusted quote offsets into a character page, never infer labels."""
    result = []
    end = offset + text_chars
    for span in spans:
        start, stop = span["start"], span["end"]
        if stop <= offset or start >= end:
            continue
        result.append(span | {
            "start": max(start, offset) - offset, "end": min(stop, end) - offset,
            "evidence_offset": span.get("evidence_offset", 0) + max(0, offset - start),
            "complete": span.get("complete", True) and offset <= start and stop <= end,
        })
    return result


def context_field_text(context, field):
    """Use the same explicit view for navigation sizes and paged retrieval."""
    from folio.research.transport import research_text

    value = {field: context[field]}
    if field + "_text" in context:
        value[field + "_text"] = context[field + "_text"]
    return research_text(value)


async def fit_context(engine, context, schema):
    """Only shorten navigable views, never mutate or silently discard source facts."""
    if not hasattr(engine, "request_capacity"):
        return context

    async def fits(value):
        return (await engine.request_capacity("research_step", value, schema))["fits"]

    if await fits(context):
        return context
    view = deepcopy(context)
    navigation = {}
    # Older outputs have durable history entries. The latest tool output must
    # remain visible so a successful read cannot turn into an empty next call.
    observations = view.get("observations", [])
    protected = set(context.get("memory_pending_requests", []))
    while len(observations) > 1:
        removable = next((index for index, item in enumerate(observations[:-1])
                          if item["request_id"] not in protected), None)
        if removable is None:
            break
        observations.pop(removable)
        if await fits(view):
            return view
    if protected and len(observations) > 1:
        # Focus on one pending batch for a checkpoint turn. Every other batch
        # remains durable and explicitly pending; it is not treated as reviewed.
        candidates = [item for item in observations if item["request_id"] in protected]
        delivered = set(context.get("memory_delivered_requests", []))
        selected = next((item for item in candidates if item["request_id"] in delivered),
                        candidates[0] if candidates else None)
        if selected:
            view["observations"] = observations = [selected]
            if selected["request_id"] in delivered:
                view["memory_checkpoint_required"] = True
            view["memory_checkpoint_note"] = (
                "Capacity allows only this batch. Other unsummarized originals remain in reading history; research is not thereby complete. "
                "Summarize only content actually sent; continue reading portions not fully supplied."
            )
            if await fits(view):
                return view
    # Every field is available through fs_read(scope='context', field=...).
    for field in ("research_notes", "research_history", "evidence_catalog", "read_progress", "global_facts", "dependencies", "directory_research",
                  "coverage_requirements", "catalog", "sections", "task", "writing_gaps",
                  "instruction", "notes", "missing_information"):
        if field not in view:
            continue
        full = context[field]
        navigation[field] = {"total_chars": len(context_field_text(context, field)),
                             "read": {"scope": "context", "field": field}}
        if field == "sections":
            view[field] = [{"section_id": s["section_id"], "title": s.get("title", "")[:100]}
                           for s in full[:20]]
        elif field == "task":
            view[field] = {k: v for k, v in full.items() if k in {"task_id", "title", "section_ids"}}
        else:
            view.pop(field)
        view.pop(field + "_text", None)
        view["context_navigation"] = navigation
        view["context_note"] = (
            "This is a capacity-limited navigation view. Hidden content still exists; original responsibilities, conflicts, and global facts remain fully preserved. "
            "Read in pages with fs_read(scope='context',field=...). Read complete responsibilities before choosing research scope. "
            "Global facts are a fixed snapshot for this round; do not rewrite them or substitute guesses."
        )
        if await fits(view):
            return view
    if observations and observations[0].get("model_text"):
        latest = observations[0]
        # Check using the actual runtime request renderer. The original text
        # stays in the ledger and its returned continuation uses character
        # offsets within that text, not offsets within serialized JSON.
        lower, upper, fitted = 256, len(latest["model_text"]), None
        while lower <= upper:
            middle = (lower + upper) // 2
            view["observations"] = [observation_page(latest, middle)]
            if await fits(view):
                fitted = deepcopy(view)
                lower = middle + 1
            else:
                upper = middle - 1
        if fitted is not None:
            return fitted
    raise ResearchLimitReached('Even the minimum research navigation context exceeds model capacity; switching to gap handling with source labels')
