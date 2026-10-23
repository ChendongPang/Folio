from collections import Counter
from pathlib import Path

import pytest
from docx import Document

from folio.models import JobInput
from folio.orchestration.engine import Engine
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed


class FailingCheck(DemoRuntime):
    def __init__(self, task, code="stalled_output"):
        self.failed_task = task
        self.code = code
        self.attempts = Counter()

    async def complete(self, task, context, schema, session_key):
        if task == self.failed_task and context["section"]["level"] == 1:
            self.attempts[context["section"]["section_id"]] += 1
            raise ModelCallFailed(
                "private provider response must not appear in progress", self.code
            )
        return await super().complete(task, context, schema, session_key)


@pytest.mark.parametrize("task", ["extract", "validate"])
async def test_failed_local_model_check_keeps_body_and_exports_explicit_pending_draft(
    store, inputs, task
):
    runtime = FailingCheck(task)
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    job = await Engine(store, runtime).run(job_id)

    assert job["status"] == "needs_confirmation"
    assert not job["snapshot"]["review_passed"]
    assert Path(job["artifact"]).is_file()
    assert len(store.states(job_id)) == 2
    section_id, attempts = next(iter(runtime.attempts.items()))
    assert attempts == 2
    state = next(s for s in store.states(job_id) if s.section_id == section_id)
    assert state.content.blocks
    assert all(c.status == "candidate" for c in state.extraction.claims)
    if task == "extract":
        assert not state.extraction.claims  # Never adopt the unfinished model response.
    key = f"local-check-incomplete:{task}:stalled_output"
    assert any(f.kind == "pending" and f.key == key for f in state.local_findings)
    assert any(f["key"] == key for f in job["snapshot"]["blocking_issues"])
    text = "\n".join(p.text for p in Document(job["artifact"]).paragraphs)
    assert state.content.blocks[0].text in text
    updates = [e["data"]["text"] for e in store.events(job_id) if e["kind"] == "work_update"]
    assert any("连续输出空白" in text and "已保留本节正文" in text for text in updates)
    assert not any("private provider" in text for text in updates)
    assert not any(e["kind"] == "job_failure" for e in store.events(job_id))


async def test_configuration_failure_still_fails_with_exact_call_attribution(store, inputs):
    runtime = FailingCheck("extract", "authentication")
    job_id = store.create(JobInput(**inputs, runtime="demo"))
    with pytest.raises(ModelCallFailed):
        await Engine(store, runtime).run(job_id)
    assert store.get(job_id)["status"] == "failed"
    failures = [e["data"] for e in store.events(job_id) if e["kind"] == "job_failure"]
    assert len(failures) == 1
    assert failures[0]["task"] == "extract"
    assert failures[0]["task_key"].startswith("extract:")
    assert failures[0]["code"] == "authentication"
    assert "private provider" not in str(failures[0])
