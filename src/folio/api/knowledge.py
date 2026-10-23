"""Optional RAGFlow catalogue for explicitly configured integrations.

The GUI source workflow does not call this service or select external datasets.
"""

import asyncio
import os
from copy import deepcopy
from time import monotonic

from fastapi import HTTPException

from folio.knowledge.ragflow import list_datasets

CATALOGUE_TIMEOUT = 15
CACHE_SECONDS = 60


class KnowledgeCatalogue:
    def __init__(self, url=None):
        self.url = (url or os.environ.get("FOLIO_RAGFLOW_URL", "")).rstrip("/")
        self.cache = {}

    async def page(self, page):
        if not self.url:
            raise HTTPException(
                503,
                "未配置外部知识服务。可设置 FOLIO_RAGFLOW_URL；上传资料与文档生成无需此服务。",
            )
        cached = self.cache.get(page)
        if cached and monotonic() - cached[0] < CACHE_SECONDS:
            return deepcopy(cached[1])
        try:
            async with asyncio.timeout(CATALOGUE_TIMEOUT):
                raw = await list_datasets(self.url, page=page)
            if not isinstance(raw, dict) or not isinstance(raw.get("datasets"), list):
                raise ValueError("Missing datasets")
            datasets, seen = [], set()
            for item in raw["datasets"]:
                dataset_id, name = item.get("id"), item.get("name")
                if not isinstance(dataset_id, str) or not dataset_id or not isinstance(name, str):
                    raise ValueError("Invalid dataset")
                if dataset_id not in seen:
                    datasets.append(
                        {
                            "id": dataset_id,
                            "name": name,
                            "description": item.get("description") or "",
                        }
                    )
                    seen.add(dataset_id)
            pagination = raw.get("pagination") or {}
            total = raw.get("total", pagination.get("total"))
            if total is not None and (type(total) is not int or total < 0):
                raise ValueError("Invalid total")
            result = {
                "datasets": datasets,
                "page": page,
                "has_more": page * 100 < total
                if total is not None
                else len(raw["datasets"]) >= 100,
                "total": total,
            }
        except TimeoutError as exc:
            raise HTTPException(504, "知识库服务响应超时，请稍后重试。") from exc
        except Exception as exc:
            raise HTTPException(
                503, "暂时无法读取知识库列表，请检查知识库服务后重试。"
            ) from exc
        self.cache[page] = (monotonic(), result)
        return deepcopy(result)
