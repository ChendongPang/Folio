import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from folio.knowledge.ragflow import RagflowKnowledge, call_tool
from folio.models import RagflowConfig


def tool_result(data, error=False):
    return SimpleNamespace(
        isError=error, content=[SimpleNamespace(type="text", text=json.dumps(data))]
    )


class Session:
    def __init__(self, data):
        self.data = data
        self.calls = []

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name="ragflow_retrieval")])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return tool_result(self.data)


async def test_mcp_mapping_scoping_dedup_and_budget(monkeypatch):
    raw = dict(
        id="chunk1",
        document_id="doc1",
        dataset_id="ds1",
        content="撤销需要主管审批。",
        document_name="业务需求",
        document_metadata={"name": "业务需求"},
    )
    session = Session({"chunks": [raw, raw]})

    @asynccontextmanager
    async def connection(*args):
        yield session

    monkeypatch.setattr("folio.knowledge.ragflow.connect", connection)
    provider = RagflowKnowledge(RagflowConfig(url="http://ragflow/mcp", dataset_ids=["ds1"]))
    result = await provider.search(["交易撤销", "审批权限"], limit=3, max_chars=1000)
    assert len(result) == 1
    assert result[0].source_version.startswith("chunk-sha256:")
    assert result[0].metadata["chunk_id"] == "chunk1"
    assert result[0].retrieved_at
    name, arguments = session.calls[0]
    assert name == "ragflow_retrieval"
    assert arguments["dataset_ids"] == ["ds1"]
    assert arguments["question"] == "交易撤销\n审批权限"
    assert "api_key" not in arguments and "chat_id" not in arguments


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "c", "document_id": "d", "dataset_id": "wrong", "content": "text"},
        {"id": "c", "content": "missing document identity"},
        {"id": "c", "document_id": "d", "content": ""},
    ],
)
def test_bad_or_out_of_scope_chunks_fail_closed(raw):
    provider = RagflowKnowledge(RagflowConfig(url="http://ragflow/mcp", dataset_ids=["ds1"]))
    with pytest.raises(ValueError):
        provider.map_chunk(raw)


async def test_mcp_tool_error_is_not_an_empty_search():
    class BrokenSession:
        async def call_tool(self, *args, **kwargs):
            return tool_result({"error": "Embedding timeout"}, error=True)

    with pytest.raises(RuntimeError, match="Embedding timeout"):
        await call_tool(BrokenSession(), "ragflow_retrieval", {"question": "撤销"})


async def test_oversized_evidence_is_not_silently_truncated(monkeypatch):
    session = Session({"chunks": [{"id": "c", "document_id": "d", "content": "x" * 100}]})

    @asynccontextmanager
    async def connection(*args):
        yield session

    monkeypatch.setattr("folio.knowledge.ragflow.connect", connection)
    provider = RagflowKnowledge(RagflowConfig(url="http://ragflow/mcp", dataset_ids=["ds1"]))
    with pytest.raises(ValueError, match="budget"):
        await provider.search(["query"], limit=1, max_chars=50)


async def test_transient_group_reconnects_without_changing_query(monkeypatch):
    import httpx

    connections = 0
    closed = 0
    session = Session({"chunks": [{"id": "c", "document_id": "d", "content": "原文"}]})

    @asynccontextmanager
    async def connection(*args):
        nonlocal connections, closed
        connections += 1
        try:
            if connections < 3:
                raise ExceptionGroup("MCP transport", [httpx.ReadError("connection reset")])
            yield session
        finally:
            closed += 1

    monkeypatch.setattr("folio.knowledge.ragflow.connect", connection)
    provider = RagflowKnowledge(RagflowConfig(url="http://ragflow/mcp", dataset_ids=["ds1"]))
    result = await provider.search(["原问题"], limit=3, max_chars=1000)
    assert connections == closed == 3
    assert result[0].text == "原文"
    assert session.calls[0][1]["question"] == "原问题"


async def test_transport_retry_is_bounded_and_data_errors_are_not_retried(monkeypatch):
    import httpx

    from folio.knowledge.ragflow import transient_transport_error

    assert not transient_transport_error(
        ExceptionGroup("mixed", [httpx.ReadError("x"), ValueError("bad data")])
    )
    calls = 0
    provider = RagflowKnowledge(RagflowConfig(url="http://ragflow/mcp", dataset_ids=["ds1"]))

    async def broken(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadError("reset")

    monkeypatch.setattr(provider, "_search_once", broken)
    with pytest.raises(httpx.ReadError):
        await provider.search(["q"], limit=1, max_chars=100)
    assert calls == 3

    async def invalid(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("outside selected datasets")

    calls = 0
    monkeypatch.setattr(provider, "_search_once", invalid)
    with pytest.raises(ValueError):
        await provider.search(["q"], limit=1, max_chars=100)
    assert calls == 1
