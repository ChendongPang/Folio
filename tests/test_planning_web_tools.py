"""Directory web search is an explicitly available native tool, never reply JSON."""

import pytest
from native_calls import native_call

from folio.research.native_tools import native_result, tool_definitions


@pytest.mark.parametrize("context,available", [({}, False), ({"web_search_available": False}, False),
                                               ({"web_search_available": True}, True)])
def test_directory_search_tool_is_offered_only_when_configured(context, available):
    names = {item["function"]["name"] for item in tool_definitions("directory_research", context, {})}
    assert ("search_web" in names) is available


def test_directory_search_preserves_native_call_and_public_purpose():
    arguments = {"query": "public certification scope", "purpose": "Clarify the compliance chapter scope",
                 "summary": "Check public certification guidance"}
    response = native_call("search_web", arguments, call_id="actual_search_call")
    result = native_result("directory_research", response, {"web_search_available": True}, {})
    step = result["steps"][0]
    assert step["tool"] == "search.web"
    assert step["arguments"] == {key: arguments[key] for key in ("query", "purpose")}
    assert step["tool_call"] == response.tool_calls[0]
    assert step["tool_call"]["id"] == "actual_search_call"


@pytest.mark.parametrize("arguments", [
    {"query": "public standards", "summary": "Look up public material"},
    {"query": "public standards", "purpose": "Check chapter scope", "summary": "Search", "authorization": "not-a-tool-argument"},
])
def test_directory_search_requires_purpose_and_rejects_unknown_provider_arguments(arguments):
    with pytest.raises(ValueError, match="Invalid arguments for search_web"):
        native_result("directory_research", native_call("search_web", arguments), {"web_search_available": True}, {})


def test_directory_search_does_not_bypass_phase_or_configuration():
    response = native_call("search_web", {"query": "public standards", "purpose": "Check chapter scope", "summary": "Search"})
    for task, context in [
        ("directory_research", {}),
        ("directory_research", {"web_search_available": True, "memory_checkpoint_required": True}),
        ("directory_research", {"web_search_available": True, "research_finalizing": True}),
        ("research_step", {"web_search_available": True}),
    ]:
        with pytest.raises(ValueError, match="not available"):
            native_result(task, response, context, {})
