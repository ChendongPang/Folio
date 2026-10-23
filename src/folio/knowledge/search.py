"""Selectable public search providers with a shared evidence contract."""

from folio.knowledge.tavily import TavilyKnowledge, WebSearchUnavailable
from folio.model_settings import feature_enabled


class DeepSeekKnowledge(TavilyKnowledge):
    provider = "deepseek"
    endpoint = "https://api.deepseek.com/v1/tools/search"
    locator = "DeepSeek Search excerpt"

    def payload(self, query, limit):
        # The official API caps a query at 128 Unicode characters. Never silently
        # truncate a query, which could change a domain filter or quoted phrase.
        if not query.strip() or len(query) > 128:
            raise WebSearchUnavailable("query_requires_1_to_128_characters")
        return {"query": query, "count": max(1, min(limit, 50)), "need_content": True}

    def content(self, item):
        return item.get("content") or item.get("summary") or item.get("snippet")


def search_provider(config):
    if not feature_enabled(config, "tavily"):
        return None
    if config.get("search_provider") == "deepseek":
        return DeepSeekKnowledge(config["deepseek_api_key"])
    return TavilyKnowledge(config["tavily_api_key"])
