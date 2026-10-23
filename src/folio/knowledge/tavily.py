"""Optional public-web evidence. One invocation is one bounded advanced search."""

from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from folio.models import Evidence, digest


class WebSearchUnavailable(RuntimeError):
    """A safe diagnostic: upstream response bodies and credentials are never retained."""


class TavilyKnowledge:
    provider = "tavily"
    endpoint = "https://api.tavily.com/search"
    locator = "Tavily advanced search excerpt"

    def payload(self, query, limit):
        return {"query": query, "search_depth": "advanced", "max_results": min(limit, 8),
                "include_answer": False, "include_raw_content": False,
                "include_images": False, "auto_parameters": False}

    def content(self, item):
        return item.get("content")

    def __init__(self, api_key: str):
        self._key = api_key

    async def search(self, query: str, *, limit: int, max_chars: int) -> list[Evidence]:
        from folio.runtime.interaction_audit import provider_attempt

        with provider_attempt(self.provider, "search") as audit:
            return await self._search(query, limit=limit, max_chars=max_chars, audit=audit)

    async def _search(self, query, *, limit, max_chars, audit):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.post(
                    self.endpoint,
                    headers={"Authorization": "Bearer " + self._key},
                    json=self.payload(query, limit),
                )
            audit["status_code"] = response.status_code
            if response.status_code != 200:
                raise WebSearchUnavailable(f"http_{response.status_code}")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise WebSearchUnavailable("invalid_response")
        except (httpx.HTTPError, ValueError) as exc:
            raise WebSearchUnavailable("connection_or_response_error") from exc
        found, seen, remaining = [], set(), max_chars
        retrieved = datetime.now(UTC).isoformat()
        for item in data["results"]:
            if not isinstance(item, dict):
                continue
            url, content = item.get("url"), self.content(item)
            if not isinstance(url, str) or not isinstance(content, str):
                continue
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                continue
            if url in seen or not content.strip() or remaining <= 0 or len(found) >= limit:
                continue
            text = content.strip()[:remaining]
            remaining -= len(text)
            seen.add(url)
            version = digest(text)
            found.append(
                Evidence(
                    evidence_id="web_" + digest(url + ":" + version),
                    source_id=url,
                    source_version=version,
                    locator=self.locator,
                    text=text,
                    retrieved_at=retrieved,
                    metadata={
                        "role": "public_web",
                        "title": str(item.get("title", ""))[:500],
                        "url": url,
                        "search_provider": self.provider,
                        "search_depth": "advanced" if self.provider == "tavily" else "search",
                        "project_fact": False,
                    },
                )
            )
        return found
