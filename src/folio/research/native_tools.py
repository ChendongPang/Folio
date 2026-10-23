"""Native function definitions and validated internal execution records.

The model chooses a function through the API, never through reply text. Internal
records keep the existing executor/ledger vocabulary; they are not a wire format.
"""

import json
from copy import deepcopy

from jsonschema import Draft202012Validator, ValidationError

from folio.research.memory import ResearchNoteUpdate, normalize_research_note
from folio.runtime.tool_calls import MAX_TOOL_CALLS_PER_BATCH

NATIVE_TOOL_TASKS = frozenset({
    "directory_research", "research_step", "next_action", "query", "web_query", "coverage_obligations",
})
RESEARCH_TOOL_TASKS = frozenset({"directory_research", "research_step"})


def _compact(value):
    if isinstance(value, list):
        return [_compact(item) for item in value]
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items()
                if key not in {"title", "default"}}
    return value


def _object(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


def _function(name, description, parameters):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": _compact(parameters), "strict": False,
    }}


def _normalize_directory_finish(args):
    """Accept one provider-compatibility spelling without weakening validation."""
    actions = args.get("local_research_actions")
    if not isinstance(actions, str):
        return args
    try:
        normalized = json.loads(actions)
    except (TypeError, ValueError):
        return args
    if normalized is None or (
        isinstance(normalized, list)
        and all(isinstance(item, str) for item in normalized)
    ):
        return args | {"local_research_actions": normalized}
    return args


def tool_definitions(task: str, context: dict, schema: dict) -> list[dict]:
    """Describe only functions permitted in this phase, with bounded arguments."""
    if task not in NATIVE_TOOL_TASKS:
        raise ValueError("Unsupported native tool task: " + task)
    if task == "coverage_obligations":
        parameters = deepcopy(schema)
        parameters["properties"].pop("tool_call", None)
        parameters["required"] = [key for key in parameters.get("required", []) if key != "tool_call"]
        final = deepcopy(parameters)
        for key in ("catalog_requests", "structure_requests"):
            final["properties"].pop(key, None)
            final["required"] = [field for field in final.get("required", []) if field != key]
        return [
            _function("inspect_coverage_sources", "Inspect the file catalog or structure needed for coverage review; preserve established coverage obligations.", parameters),
            _function("submit_coverage", "Submit confirmed exhaustive-coverage scopes and unresolved scopes.", final),
        ]
    if task == "next_action":
        parameters = deepcopy(schema)
        parameters["properties"].pop("action", None)
        parameters["required"] = [key for key in parameters.get("required", []) if key != "action"]
        descriptions = {"search": "Continue searching for original materials still missing for this task.",
                        "write": "The available evidence is sufficient; proceed to body writing.",
                        "pending": "Record insufficient evidence and defer to bounded follow-up research and completion."}
        return [_function(name, descriptions[name], parameters)
                for name in context.get("available_actions", descriptions) if name in descriptions]
    if task in {"query", "web_query"}:
        name, description = (
            ("search_sources", "Search original materials for this task's gaps; do not repeat completed queries.")
            if task == "query" else
            ("search_web", "Search public materials through Tavily; do not send internal source text, personal data, or credentials.")
        )
        return [_function(name, description, schema)]

    string = {"type": "string"}
    strings = {"type": "array", "items": string}
    integer = {"type": "integer", "minimum": 0}
    page = {"type": "integer", "minimum": 1}
    paging = {"offset": integer, "limit": page, "max_chars": page}
    cursor = {"type": "string", "description": "Continue with the C ID returned by the previous tool; it already identifies the file and reading location. "
              "Repeating the same file or location is allowed, but conflicting locations are not."}
    source = {"file": string, "node": string, "path": string, "node_id": string}
    common = {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1000,
                    "description": "One sentence explaining this specific action to the user; no private reasoning."},
        "notes": {"type": "string", "maxLength": 4000,
                  "description": "Legacy brief work remark; omission or an empty value preserves earlier content. Put research findings in research_notes."},
        "research_notes": {"type": "array",
                           "items": ResearchNoteUpdate.model_json_schema(),
                           "description": "Incremental notes on source text actually read; cite only E evidence IDs shown in the text. The program associates reading records. "
                           "Omit note_id for additions; use an M ID for revisions. Distinguish source statements, inferences, and irrelevant content; no private reasoning."},
        "review_reason": {"type": "string", "maxLength": 1000,
                          "description": "When rereading a supplied range, identify the specific number, conflict, or question to check. Ordinary pagination does not require this."},
    }
    if task == "research_step":
        common["missing_information"] = {"type": "array",
                                         "items": {"type": "string", "maxLength": 3500}}
    else:
        from folio.research.planning import PlanningResearchStep

        common["review_reason"] |= PlanningResearchStep.model_json_schema()["properties"]["review_reason"]
    definitions = []

    def add(name, description, properties, required=()):
        definitions.append(_function(name, description, _object(
            properties | common, (*required, "summary"))))

    add("fs_list", "List source files and types; continue by offset. scope=artifacts is only for registered business-research artifacts.",
        {"path": string, "offset": integer, "limit": page,
         **({"scope": {"type": "string", "enum": ["artifacts"]}} if task == "research_step" else {})})
    add("fs_inspect", "Read a source file's structure.md; prefer its short file ID. Structure is not body text; continue by cursor if needed.",
        source | paging | {"cursor": cursor})
    add("fs_search", "Locate relevant source passages. Search hits do not establish complete reading; use returned locations in fs_read.",
        {"query": string, "cursor": cursor, "file": string, "path": string, **paging,
         **({"scope": {"type": "string", "enum": ["knowledge"]}} if task == "research_step" else {})})
    definitions[-1]["function"]["parameters"]["anyOf"] = [
        {"required": ["query"]}, {"required": ["cursor"]},
    ]
    read = source | paging | {
        "character_offset": {"type": "integer", "minimum": 0,
                             "description": "Character position within the selected source block, only for exact continuation. Prefer the returned cursor."},
        "cursor": cursor, "locator": string, "sheet": string, "cell_range": string,
        "page": page, "slide": page,
        "scope": {"type": "string", "enum": ["history", "evidence", "context"]},
        "request_id": string, "evidence_id": string, "field": string,
    }
    if task == "research_step":
        read |= {"query": string, "artifact_id": string, "execution_id": string, "output_path": string}
    add("fs_read", "Read a complete section and its subsections by file/node, or a sheet/cell_range/page/slide. "
        "Omit limit and max_chars for source reads: the entire selected range is returned by default. "
        "Request several sections together using parallel fs_read calls. Their results share the available request capacity; "
        "only content that does not fit is paginated, with an exact cursor and remaining range. Continue with that cursor. "
        "scope=history retrieves results by request_id; evidence retrieves evidence by evidence_id; context retrieves complete task context by field.",
        read)
    if task == "directory_research" and context.get("web_search_available"):
        add("search_web", "Search public references only for a concrete question that changes outline scope or section choices. "
            "Understand relevant uploaded materials first. Prefer official sources; excerpts are not verified full pages. "
            "Do not send private source text, personal data or credentials, or search for missing bidder details/prices. "
            "Results are supplementary references, never uploaded tender requirements. Reuse earlier searches.",
            {"query": {"type": "string", "minLength": 1},
             "purpose": {"type": "string", "minLength": 1,
                         "description": "The concrete public question and how its answer affects the outline."}},
            ("query", "purpose"))
    if task == "research_step":
        add("fs_view", "View an original PDF/image page or a normalized crop. View at most two original pages per response; defer additional pages. "
            "Convert Office documents first and supply execution_id.",
            source | {"page": page, "region": {"type": "array", "items": {"type": "number"},
                                                "minItems": 4, "maxItems": 4}, "execution_id": string})
        add("sandbox_exec", "Execute Python or controlled Office/OCR conversions in an isolated container, or request an allowlisted dependency. "
            "python requires code; convert requires conversion/file; dependency requires package/version/reason. "
            "Originals are read-only; write outputs to FOLIO_OUTPUT_DIR. Network and host access are prohibited.",
            {"operation": {"type": "string", "enum": ["python", "convert", "dependency"]},
             "code": {"type": "string", "maxLength": 60000}, "file": string, "path": string,
             "conversion": {"type": "string", "enum": ["ocr", "office_pdf", "office_recalculate", "office_ocr"]},
             "pages": {"type": "array", "items": page, "maxItems": 20},
             "package": string, "version": string, "reason": string,
             "profile": {"type": "string", "enum": ["base", "data", "office", "ocr"]}}, ("operation",))
        reference = _object({"section_id": string, "block_id": string}, ("section_id", "block_id"))
        resolution = _object({
            "item_id": string, "status": {"type": "string", "enum": ["pending", "not_applicable", "covered"]},
            "reason": string, "references": {"type": "array", "items": reference},
        }, ("item_id", "status", "reason", "references"))
        add("artifact_save", "Register/reuse derived results, create/check coverage inventories, or request independent visual verification. "
            "register requires execution_id/output_path/evidence_ids/methodology/description; reuse requires artifact_id; "
            "coverage.create requires file and scope; coverage.resolve requires coverage_id/resolutions; "
            "extraction.verify accepts one or two pages per call; visual.extract submits file/page/text for independent approval.",
            source | {"operation": {"type": "string", "enum": ["register", "reuse", "coverage.create", "coverage.resolve", "extraction.verify", "visual.extract"]},
                      "execution_id": string, "output_path": string, "artifact_id": string,
                      "evidence_ids": strings, "methodology": string, "description": string,
                      "locator": string, "coverage_id": string,
                      "resolutions": {"type": "array", "items": resolution},
                      "pages": {"type": "array", "items": page, "minItems": 1, "maxItems": 2},
                      "page": page, "text": string}, ("operation",))
        add("finish", "Submit this round's conclusions and missing materials. The program still checks evidence independently; this is not approval or completion of the body.", {})
    else:
        from folio.research.planning import ResearchConclusion

        conclusion = ResearchConclusion.model_json_schema()
        for key in ("notes", "research_notes", "review_reason"):
            conclusion["properties"][key] = common[key]
        definitions.append(_function("finish", "Submit source-cited outline requirements, file selections/exclusions, and unresolved questions. The program associates read ranges and verifies provenance.", conclusion))
    remember = _function("research_save", "Summarize source text already read and persist notes without reading new material. Summarize first when capacity is insufficient, then continue research.",
                         _object({"research_notes": common["research_notes"] | {"minItems": 1},
                                  "summary": common["summary"]}, ("research_notes", "summary")))
    if task == "directory_research" and context.get("research_finalizing"):
        return [item for item in definitions if item["function"]["name"] == "finish"]
    if context.get("memory_checkpoint_required"):
        return [remember]
    definitions.append(remember)
    return definitions


def native_results(task, result, context, schema) -> list[dict]:
    """Validate every call before execution; response prose never becomes an action."""
    calls = getattr(result, "tool_calls", [])
    if not isinstance(calls, list) or not 1 <= len(calls) <= MAX_TOOL_CALLS_PER_BATCH:
        raise ValueError(f"Expected 1 to {MAX_TOOL_CALLS_PER_BATCH} native tool_calls; text actions are not supported")
    if task not in RESEARCH_TOOL_TASKS and len(calls) != 1:
        raise ValueError("Expected exactly one native tool_call for this decision task")
    offered = {item["function"]["name"]: item["function"] for item in tool_definitions(task, context, schema)}
    ids = set()
    steps = []
    for call in calls:
        if (not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"].strip()
            or call.get("type") != "function" or not isinstance(call.get("function"), dict)):
            raise ValueError("Invalid native function call identity")
        if call["id"] in ids:
            raise ValueError("Duplicate native tool_call ID in one response")
        ids.add(call["id"])
        steps.append(_native_step(task, call, offered, context))
    return steps


def native_result(task, result, context, schema) -> dict:
    steps = native_results(task, result, context, schema)
    continuation = (
        {"reasoning_content": result.reasoning_content}
        if isinstance(getattr(result, "reasoning_content", None), str) else {}
    )
    if task in RESEARCH_TOOL_TASKS:
        return {"steps": steps, "assistant_content": result.content, **continuation}
    if task == "coverage_obligations":
        return steps[0] | continuation
    return steps[0]


def _native_step(task, call, offered, context=None):
    function = call["function"]
    name = function.get("name")
    if not isinstance(name, str) or name not in offered:
        raise ValueError("Function is not available in this phase: " + str(name))
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        raise ValueError("Native function arguments must be a JSON string")
    args = json.loads(arguments)
    # Earlier recordings may contain bookkeeping that is now derived from
    # verified evidence. Ignore only those retired fields; preserve the original
    # provider call below for audit and native tool_call_id pairing.
    if task in RESEARCH_TOOL_TASKS and isinstance(args, dict):
        if task == "directory_research" and name == "finish":
            args.pop("selected_ranges", None)
            args = _normalize_directory_finish(args)
        notes = args.get("research_notes")
        if isinstance(notes, list):
            args["research_notes"] = [normalize_research_note(note) for note in notes]
    if task == "coverage_obligations" and isinstance(args, dict):
        from folio.research.coverage_obligations import normalize_coverage_arguments

        args = normalize_coverage_arguments(args, context)
    try:
        Draft202012Validator(offered[name]["parameters"]).validate(args)
    except ValidationError as exc:
        raise ValueError(f"Invalid arguments for {name}: {exc.message}") from exc
    if task == "next_action":
        return args | {"action": name}
    if task in {"query", "web_query"}:
        return args
    if task == "coverage_obligations":
        if name == "inspect_coverage_sources" and not (args.get("catalog_requests") or args.get("structure_requests")):
            raise ValueError("inspect_coverage_sources needs at least one catalog or structure request")
        return args | {"tool_call": deepcopy(call)}
    metadata = {key: args[key] for key in ("summary", "notes", "missing_information", "research_notes", "review_reason") if key in args}
    params = {key: value for key, value in args.items() if key not in metadata}
    if name == "finish" and task == "directory_research":
        params["summary"] = args.get("summary", "")
        metadata["summary"] = 'Submit outline research conclusions'
    return {"tool": name.replace("_", ".", 1) if name != "finish" else "finish",
            "arguments": params, "tool_call": deepcopy(call), **metadata}
