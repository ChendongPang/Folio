"""Lexical retrieval retains long source blocks and respects real query budgets."""

import pytest

from folio.knowledge.local import LocalKnowledge
from folio.research.workspace import WorkspaceManager


@pytest.mark.asyncio
async def test_long_source_is_complete_when_it_fits(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    text = "ordinary background " * 300 + "unique_requirement"
    (sources / "source.txt").write_text(text)
    result = await LocalKnowledge(sources).search(["unique_requirement"], limit=1, max_chars=20000)
    assert len(result) == 1
    assert result[0].text == text
    assert result[0].locator == "line 1"


@pytest.mark.asyncio
async def test_long_source_search_excerpt_remains_resolvable(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    text = "ordinary background " * 300 + "unique_requirement " + "following text " * 300
    (sources / "source.txt").write_text(text)
    result = await LocalKnowledge(sources).search(["unique_requirement"], limit=1, max_chars=500)
    assert len(result) == 1
    assert len(result[0].text) == 500
    assert "unique_requirement" in result[0].text
    workspace = WorkspaceManager(sources, tmp_path / "work")
    assert workspace.resolve_evidence(result[0]).text == result[0].text
