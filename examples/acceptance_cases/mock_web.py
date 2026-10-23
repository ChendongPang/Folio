"""Offline Tavily-shaped fixture adapter for Engine(web_knowledge=...).

Never installed globally and never performs HTTP. Use one instance per tested task.
"""

import json
from pathlib import Path

from folio.models import Evidence, digest


class FixtureWebKnowledge:
    def __init__(self, fixture: str | Path):
        self.data = json.loads(Path(fixture).read_text(encoding="utf-8"))
        self.calls = []
        self.attempts = 0

    async def search(self, query: str, *, limit: int, max_chars: int) -> list[Evidence]:
        index = len(self.calls)
        self.attempts += 1
        if index >= len(self.data["rounds"]):
            raise AssertionError("Offline fixture exhausted: a third web round is not allowed")
        record = self.data["rounds"][index]
        self.calls.append({"round": index + 1, "actual_query": query, "fixture_query": record["query"]})
        evidence = []
        remaining = max_chars
        for item in record["results"][:limit]:
            if remaining <= 0:
                break
            content = item["content"][: min(6000, remaining)]
            remaining -= len(content)
            if not content.strip():
                continue
            version = digest(content)
            evidence.append(Evidence(
                evidence_id="mock_web_" + digest(item["url"] + version),
                source_id=item["url"], source_version=version,
                locator="Offline synthetic Tavily result",
                text=content,
                metadata={"role": "public_web", "project_fact": False,
                          "synthetic_fixture": True, "title": item["title"],
                          "url": item["url"], "search_depth": "advanced"},
            ))
        return evidence
