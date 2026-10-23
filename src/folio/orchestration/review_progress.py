"""Human-readable, durable review progress without changing review decisions."""

import asyncio
from contextlib import contextmanager
from time import monotonic
from uuid import uuid4

from folio.orchestration.review_trace import review_calls
from folio.runtime.errors import ModelCallFailed, TaskRetriesExhausted, public_model_error


def finding_detail(finding):
    return {"finding_id": finding.fingerprint, **finding.model_dump()}


def call_summary(calls):
    count, cached = calls["model_calls"], calls["cache_hits"]
    if not count and cached:
        return f"复用 {cached} 次已有结果，未新增模型任务调用"
    return f"模型任务调用 {count} 次，复用 {cached} 次已有结果"


class ReviewProgress:
    def __init__(self, engine, phase, states, total):
        self.engine, self.phase, self.total = engine, phase, total
        self.round_id = f"{phase}:{uuid4().hex[:12]}"
        self.states = {s.section_id: s for s in states}
        self.definitions = {s.section_id: s for s in engine.plan.sections}
        self.units = {}

    def sections(self, ids):
        return [
            {
                "section_id": sid,
                "title": (
                    f"{self.definitions[sid].display_number} {self.definitions[sid].title}".strip()
                    if sid in self.definitions
                    else sid
                ),
                **({"revision": self.states[sid].revision} if sid in self.states else {}),
            }
            for sid in ids
        ]

    def titles(self, ids):
        values = self.sections(ids)
        return "、".join(f"《{s['title']}》" for s in values[:2]) + (
            f"等 {len(values)} 节" if len(values) > 2 else ""
        )

    def emit(self, text, **data):
        self.engine.store.work_event(
            self.engine.job_id,
            text,
            review={"phase": self.phase, "round_id": self.round_id, "total": self.total, **data},
        )

    @contextmanager
    def unit(self, index, section_ids, text, **details):
        data = self.units[index] = {
            "unit_id": str(index),
            "index": index,
            "sections": self.sections(section_ids),
            **details,
        }
        started = monotonic()
        self.emit(text, **data, status="started")
        with review_calls() as stats:
            try:
                yield data
            except (Exception, asyncio.CancelledError) as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                if cancelled:
                    reason = "本项检查已中止，尚无完整结论。"
                elif isinstance(exc, TaskRetriesExhausted):
                    reason = "该请求此前已达到重试上限，本次未重新调用模型。"
                elif isinstance(exc, ValueError) and not isinstance(exc, ModelCallFailed):
                    reason = "输入容量或响应结构校验未通过，未采用不完整结论。"
                else:
                    reason = public_model_error(exc)["text"]
                data.update(
                    status="cancelled" if cancelled else "failed",
                    outcome="needs_confirmation",
                    reason=reason,
                    error_type=type(exc).__name__,
                    calls=stats.snapshot(),
                    elapsed_ms=round((monotonic() - started) * 1000),
                )
                label = "跨章节检查" if self.phase == "consistency" else "问题精查"
                self.emit(f"{label}第 {index}/{self.total} 项未完成：{reason}", **data)
                raise
            finally:
                data.update(
                    calls=stats.snapshot(), elapsed_ms=round((monotonic() - started) * 1000)
                )

    def complete(self, index, completed, text):
        data = self.units[index]
        data.setdefault("status", "completed")
        self.emit(text, **data, completed=completed)
