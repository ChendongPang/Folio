"""RAGFlow's self-host MCP contract, using the official Streamable HTTP client."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from folio.models import Evidence, RagflowConfig, digest
from folio.runtime.base import parse_json


def transient_transport_error(exc):
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(transient_transport_error(e) for e in exc.exceptions)
    return isinstance(exc, (httpx.TransportError, anyio.EndOfStream, anyio.BrokenResourceError))


@asynccontextmanager
async def connect(url: str, timeout: float = 120):
    async with streamablehttp_client(
        url,
        timeout=timedelta(seconds=timeout),
        sse_read_timeout=timedelta(seconds=timeout),
    ) as (read, write, _):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=timeout)
        ) as session:
            await session.initialize()
            yield session


async def call_tool(session, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments=arguments)
    texts = [item.text for item in result.content if item.type == "text"]
    if result.isError:
        raise RuntimeError(f"{name} failed: {' '.join(texts) or 'No error details'}")
    if not texts:
        raise ValueError(f"{name} returned no JSON text block")
    return parse_json(texts[0])


async def list_datasets(url: str, *, name: str | None = None, page: int = 1) -> dict:
    async with connect(url) as session:
        tools = await session.list_tools()
        if "ragflow_list_datasets" not in {tool.name for tool in tools.tools}:
            raise ValueError("Server does not expose ragflow_list_datasets")
        arguments: dict[str, Any] = {"page": page, "page_size": 100}
        if name:
            arguments["name"] = name
        return await call_tool(session, "ragflow_list_datasets", arguments)


class RagflowKnowledge:
    def __init__(self, config: RagflowConfig):
        self.config = config

    async def search(self, queries: list[str], *, limit: int, max_chars: int) -> list[Evidence]:
        # Read-only retry, with a fresh MCP session after all old tasks have closed.
        # This is one logical retrieval round, not a new model-generated search.
        for attempt in range(3):
            try:
                return await self._search_once(queries, limit=limit, max_chars=max_chars)
            except Exception as exc:
                if not transient_transport_error(exc) or attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))
        raise AssertionError("unreachable")

    async def _search_once(
        self, queries: list[str], *, limit: int, max_chars: int
    ) -> list[Evidence]:
        chunks, seen = [], set()
        async with connect(self.config.url, self.config.timeout_seconds) as session:
            tools = await session.list_tools()
            if "ragflow_retrieval" not in {tool.name for tool in tools.tools}:
                raise ValueError("Server does not expose ragflow_retrieval")
            # One combined retrieval avoids multiplying server calls and preserves relevance order.
            arguments = self.config.model_dump(
                exclude={"url", "timeout_seconds"}, exclude_none=True
            )
            arguments.update(question="\n".join(queries), page=1, page_size=min(limit, 100))
            from folio.runtime.interaction_audit import provider_attempt

            with provider_attempt("ragflow", "ragflow_retrieval"):
                data = await call_tool(session, "ragflow_retrieval", arguments)
            if not isinstance(data.get("chunks"), list):
                raise ValueError("ragflow_retrieval must return a chunks array")
            size = 0
            for raw in data["chunks"]:
                chunk = self.map_chunk(raw)
                if chunk.evidence_id in seen:
                    continue
                seen.add(chunk.evidence_id)
                if len(chunks) >= limit:
                    break
                if size + len(chunk.text) > max_chars:
                    # Do not silently truncate evidence. Explicit budget failure if nothing fits.
                    continue
                chunks.append(chunk)
                size += len(chunk.text)
            if data["chunks"] and not chunks:
                raise ValueError(
                    "Retrieved chunks exceed the evidence budget; increase evidence_chars"
                )
        return chunks

    def map_chunk(self, raw: dict) -> Evidence:
        if not isinstance(raw, dict):
            raise ValueError("Invalid RAGFlow chunk")
        chunk_id = raw.get("id") or raw.get("chunk_id")
        document_id = raw.get("document_id") or raw.get("doc_id")
        dataset_id = raw.get("dataset_id") or raw.get("kb_id")
        text = raw.get("content") or raw.get("content_with_weight")
        if not chunk_id or not document_id or not isinstance(text, str) or not text.strip():
            raise ValueError("RAGFlow chunk is missing id, document_id, or content")
        if dataset_id and dataset_id not in self.config.dataset_ids:
            raise ValueError("RAGFlow returned a chunk outside selected datasets")
        if self.config.document_ids and document_id not in self.config.document_ids:
            raise ValueError("RAGFlow returned a chunk outside selected documents")
        # RAGFlow does not promise immutable document revisions; use the returned content hash.
        version = digest(text)
        source = f"ragflow:{dataset_id or 'selected'}:{document_id}"
        return Evidence(
            evidence_id="ev_" + digest(f"{source}:{chunk_id}:{version}"),
            source_id=source,
            source_version=f"chunk-sha256:{version}",
            locator=f"chunk:{chunk_id}",
            text=text,
            retrieved_at=datetime.now(UTC).isoformat(),
            metadata={
                "dataset_id": dataset_id,
                "document_id": document_id,
                "chunk_id": chunk_id,
                "document_name": raw.get("document_name") or raw.get("document_keyword"),
                "similarity": raw.get("similarity"),
                "document_metadata": raw.get("document_metadata", {}),
            },
        )
