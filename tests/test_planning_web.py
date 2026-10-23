"""Directory web research stays optional, reusable, and separate from project facts."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from folio.knowledge.tavily import WebSearchUnavailable
from folio.model_settings import probe_fingerprint
from folio.models import Evidence, digest
from folio.research.planning_web import DirectoryWebResearch, validate_web_evidence


def runtime():
    config = {"tavily_enabled": True, "tavily_api_key": "test-key-never-used-online"}
    config["verified_capabilities"] = {"tavily": probe_fingerprint(config, "tavily")}
    return SimpleNamespace(config=config)


def evidence(url="https://example.com/standard", text="Published reference excerpt."):
    version = digest(text)
    return Evidence(evidence_id="web_" + digest(url + ":" + version), source_id=url,
                    source_version=version, locator="Tavily advanced search excerpt", text=text,
                    retrieved_at="2026-10-21T08:00:00+00:00",
                    metadata={"role": "public_web", "project_fact": False,
                              "url": url, "title": "Published standard"})


class Provider:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        value = self.results.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Unit tests must never call Tavily")

    monkeypatch.setattr("folio.knowledge.tavily.TavilyKnowledge.search", forbidden)


@pytest.mark.parametrize("config", [{}, {"tavily_enabled": True, "tavily_api_key": "unverified"}])
async def test_web_tool_requires_verified_settings_even_with_injected_provider(tmp_path, config):
    provider = Provider()
    helper = DirectoryWebResearch(SimpleNamespace(config=config), tmp_path, provider=provider)
    assert not helper.available
    result = await helper.search("reference standard", "Confirm a public technical standard")
    assert result["status"] == "unavailable"
    assert not provider.calls
    assert helper.report()["evidence"] == []


async def test_success_preserves_canonical_identity_and_exact_readable_delivery_spans(tmp_path):
    original = evidence(text="First line.\nSecond line with 文本.")
    provider = Provider([original])
    helper = DirectoryWebResearch(runtime(), tmp_path, provider=provider)
    result = await helper.search("published standard", "Clarify the outline's external references", max_chars=12345)
    assert provider.calls == [("published standard", {"limit": 8, "max_chars": 12345})]
    assert result["status"] == "completed" and not result["cached"]
    item = Evidence.model_validate(result["evidence"][0])
    assert item.evidence_id == original.evidence_id
    assert item.metadata["reference"] == "W1"
    assert item.metadata["role"] == "public_web" and item.metadata["project_fact"] is False
    text = helper.format_result(result)
    span = result["model_evidence_spans"][0]
    assert text[span["start"]:span["end"]] == original.text
    assert span["evidence_id"] == original.evidence_id
    assert "[W1]" in text and original.source_id in text and original.retrieved_at in text
    assert "not verified project facts" in text
    assert helper.resolve_reference("W1").evidence_id == original.evidence_id
    assert helper.resolve_reference(original.evidence_id).text == original.text
    assert "test-key" not in helper.path.read_text()


async def test_duplicate_queries_reuse_results_across_restart_and_keep_each_purpose(tmp_path):
    provider = Provider([evidence()])
    helper = DirectoryWebResearch(runtime(), tmp_path, provider=provider)
    await helper.search("Published   STANDARD", "First question")
    cached = await helper.search(" published standard ", "Another question")
    assert cached["cached"] and cached["purpose"] == "Another question"
    restored = DirectoryWebResearch(runtime(), tmp_path, provider=provider)
    again = await restored.search("PUBLISHED STANDARD", "Resumed question")
    assert again["cached"] and len(provider.calls) == 1
    report = restored.report()
    assert [item["purpose"] for item in report["searches"]] == ["First question", "Another question", "Resumed question"]
    assert len(report["evidence"]) == 1
    assert all(item["started_at"] and item["completed_at"] for item in report["searches"])


async def test_concurrent_identical_queries_share_one_provider_request(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(Provider):
        async def search(self, query, **kwargs):
            self.calls.append(query)
            started.set()
            await release.wait()
            return [evidence()]

    provider = WaitingProvider()
    helper = DirectoryWebResearch(runtime(), tmp_path, provider=provider)
    first = asyncio.create_task(helper.search("same query", "First purpose"))
    await started.wait()
    second = asyncio.create_task(helper.search("same query", "Second purpose"))
    release.set()
    results = await asyncio.gather(first, second)
    assert len(provider.calls) == 1
    assert sorted(result["cached"] for result in results) == [False, True]
    assert len(helper.report()["searches"]) == 2


async def test_failed_search_is_safe_retryable_and_retains_previous_sources(tmp_path):
    provider = Provider([evidence()], RuntimeError("secret-token and private response body"),
                        [evidence("https://example.com/other", "Another standard")])
    helper = DirectoryWebResearch(runtime(), tmp_path, provider=provider)
    await helper.search("first query", "Obtain one source")
    failed = await helper.search("second query", "Find a second source")
    assert failed["status"] == "failed" and failed["error_code"] == "search_failed"
    assert helper.resolve_reference("W1").text == evidence().text
    assert "secret-token" not in helper.path.read_text()
    assert "private response" not in json.dumps(failed)
    recovered = await helper.search("second query", "Retry the missing source")
    assert recovered["status"] == "completed" and not recovered["cached"]
    assert len(helper.report()["evidence"]) == 2 and len(provider.calls) == 3


async def test_empty_success_is_cached_and_provider_error_code_is_sanitized(tmp_path):
    provider = Provider([], WebSearchUnavailable("http_429"), WebSearchUnavailable("sensitive response body"))
    helper = DirectoryWebResearch(runtime(), tmp_path, provider=provider)
    first = await helper.search("nothing", "Check an unavailable subject")
    second = await helper.search("nothing", "Reuse the empty search")
    assert not first["evidence"] and second["cached"]
    limited = await helper.search("other", "Check another subject")
    assert limited["error_code"] == "http_429"
    unsafe = await helper.search("another", "Check one more subject")
    assert unsafe["error_code"] == "search_failed"
    assert "sensitive response body" not in helper.path.read_text()


async def test_cancelled_search_is_durable_and_can_resume(tmp_path):
    provider = Provider(asyncio.CancelledError(), [evidence()])
    helper = DirectoryWebResearch(runtime(), tmp_path, provider=provider)
    with pytest.raises(asyncio.CancelledError):
        await helper.search("query", "Cancelled task")
    restored = DirectoryWebResearch(runtime(), tmp_path, provider=provider)
    assert restored.report()["searches"][0]["status"] == "interrupted"
    result = await restored.search("query", "Explicit retry")
    assert result["status"] == "completed" and not result["cached"]


async def test_findings_persist_canonical_refs_and_reports_are_detached(tmp_path):
    helper = DirectoryWebResearch(runtime(), tmp_path, provider=Provider([evidence()]))
    await helper.search("query", "Find context")
    report = helper.report(findings=[{"text": "This standard informs the outline.", "evidence_ids": ["W1"]}])
    assert report["findings"][0]["evidence_ids"] == [evidence().evidence_id]
    report["evidence"][0]["text"] = "modified outside"
    assert helper.resolve_reference("W1").text == evidence().text
    restored = DirectoryWebResearch(runtime(), tmp_path, provider=Provider())
    assert restored.report()["findings"] == helper.report()["findings"]
    with pytest.raises(ValueError, match="Unknown"):
        helper.report(findings=[{"text": "Unsupported claim", "evidence_ids": ["W999"]}])
    assert helper.report()["findings"]


@pytest.mark.parametrize("change", [
    {"text": "tampered"}, {"source_id": "file:///private/document"},
    {"source_version": "wrong"}, {"retrieved_at": None},
    {"metadata": {"role": "public_web", "project_fact": True, "url": "https://example.com/standard"}},
])
def test_frozen_evidence_validation_rejects_tampering_and_project_fact_promotion(change):
    with pytest.raises(ValueError):
        validate_web_evidence(evidence().model_dump() | change)


async def test_bad_provider_evidence_does_not_replace_previous_references(tmp_path):
    provider = Provider([evidence()], [evidence().model_copy(update={"evidence_id": "invented"})])
    helper = DirectoryWebResearch(runtime(), tmp_path, provider=provider)
    await helper.search("good", "Read a source")
    failed = await helper.search("bad", "Read another source")
    assert failed["status"] == "failed"
    assert len(helper.report()["evidence"]) == 1
    assert helper.resolve_reference("W1").evidence_id == evidence().evidence_id


async def test_search_count_uses_enclosing_budget_without_a_second_two_call_quota(tmp_path):
    provider = Provider(*[[evidence(f"https://example.com/source-{number}")] for number in range(5)])
    helper = DirectoryWebResearch(runtime(), tmp_path / "new-research-folder", provider=provider)
    for number in range(5):
        result = await helper.search(f"distinct query {number}", "Resolve the next public-information question")
        assert result["status"] == "completed"
    assert len(provider.calls) == 5
    assert len(helper.report()["evidence"]) == 5
    assert helper.resolve_reference("W5").source_id.endswith("source-4")


def test_started_receipts_become_interrupted_on_process_restart(tmp_path):
    state = {"available": True, "searches": [{"query": "query", "purpose": "purpose", "status": "started",
                                             "cached": False, "evidence_ids": [], "started_at": "2026-10-21"}],
             "evidence": [], "findings": []}
    (tmp_path / "web-research.json").write_text(json.dumps(state))
    helper = DirectoryWebResearch(runtime(), tmp_path, provider=Provider())
    assert helper.report()["searches"][0]["status"] == "interrupted"
    assert json.loads(helper.path.read_text())["searches"][0]["error_code"] == "search_interrupted"
