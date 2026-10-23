"""An optional, durable two-call public-web supplement after internal retrieval."""

import asyncio
import json

from pydantic import Field

from folio.models import Evidence, Model, digest
from folio.orchestration.work import WebSearchAttempt


class WebQuery(Model):
    query: str = Field(min_length=1, max_length=500)


class WebResearchWorkflow:
    def web_search_available(self, work):
        return self.web_knowledge is not None and len(work.web_searches) < 2

    async def supplement_web(self, task, work, *, questions=None, previous_research=None):
        """Reserve the call before I/O; restarts cannot silently reset the allowance."""
        prior = (previous_research if previous_research is not None else
                 work.research.get("directory_research", {}).get("web_research", {}))
        previous_queries = list(dict.fromkeys([
            *[item["query"] for item in prior.get("searches", [])
              if item.get("status") == "completed" and item.get("query")],
            *[item.query for item in work.web_searches if item.query],
        ]))
        attempt = WebSearchAttempt()
        work.web_searches.append(attempt)
        self.store.save_work_state(self.job_id, work)
        self.store.work_event(
            self.job_id,
            f"“{task.title}”本地资料研究后仍有缺口，正在深度联网搜索"
            f"（第 {len(work.web_searches)}/2 次）。",
        )
        try:
            query = await self.call(
                "web_query",
                {
                    "topic": task.title,
                    "objective": task.objective,
                    "missing_information": (work.missing if questions is None else questions)[:12],
                    "previous_queries": previous_queries,
                    "directory_web_research": prior,
                    "scope": "public_web_only",
                },
                WebQuery,
            )
            if query.query in [a.query for a in work.web_searches[:-1]]:
                attempt.status = "skipped"
                attempt.error_code = "duplicate_query"
                return
            attempt.query = query.query
            self.store.save_work_state(self.job_id, work)
            key = "web:" + digest(task.task_id + ":" + query.query)
            cached = self.store.retrieval(self.job_id, key)
            if cached is None:
                from folio.orchestration.retrieval_audit import search_with_audit

                async with asyncio.timeout(35):
                    found = await search_with_audit(
                        self.store,
                        self.job_id,
                        self.web_knowledge,
                        query.query,
                        task_id=task.task_id,
                        source_kind="public_web",
                        limit=self.limits.retrieval_limit,
                        max_chars=self.limits.evidence_chars,
                    )
                cached = json.dumps([e.model_dump() for e in found], ensure_ascii=False)
                self.store.save_retrieval(self.job_id, key, cached)
            else:
                self.store.audit_event(
                    self.job_id,
                    "retrieval_cache_hit",
                    {
                        "work_task_id": task.task_id,
                        "queries": [query.query],
                        "source_kind": "public_web",
                        "retrieval_key": key,
                        "evidence_ids": [item["evidence_id"] for item in json.loads(cached)],
                    },
                )
            ids = {e.evidence_id for e in work.evidence}
            for value in json.loads(cached):
                evidence = Evidence.model_validate(value)
                attempt.evidence_ids.append(evidence.evidence_id)
                if evidence.evidence_id not in ids:
                    work.evidence.append(evidence)
                    ids.add(evidence.evidence_id)
            attempt.status = "completed"
            self.store.work_event(
                self.job_id,
                f"“{task.title}”联网搜索完成，取得 {len(attempt.evidence_ids)} 条公开资料，正在核对缺口。",
            )
        except Exception as exc:
            # Optional retrieval cannot prevent delivery; do not store exception text
            # because provider responses may echo private request information.
            attempt.status = "failed"
            attempt.error_code = type(exc).__name__
            self.store.work_event(
                self.job_id, f"“{task.title}”本次联网搜索不可用，继续处理剩余缺口。"
            )
        finally:
            self.store.save_work_state(self.job_id, work)
