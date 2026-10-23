"""Writing preferences must not create another repair or review loop."""

import json
from collections import Counter
from pathlib import Path

from folio.document.guidance import load_guide
from folio.models import JobInput, Review
from folio.orchestration.engine import Engine
from folio.runtime.base import RuntimeResult
from folio.runtime.demo import DemoRuntime


class AdviceRuntime(DemoRuntime):
    def __init__(self, suggestions=False):
        self.suggestions = suggestions
        self.calls = []

    async def complete(self, task, context, schema, session_key):
        self.calls.append(task)
        result = await super().complete(task, context, schema, session_key)
        if self.suggestions and task in {"validate", "review", "review_work", "review_focus"}:
            data = json.loads(result.content)
            ids = (
                [context["section"]["section_id"]]
                if task == "validate"
                else [section["section_id"] for section in context["sections"]]
            )
            data.setdefault("findings", []).extend(
                {
                    "kind": "coverage" if number % 2 else "pending",
                    "basis": "guidance",
                    "description": f"可增加示例帮助理解，属于写作建议 {number}",
                    "key": f"optional-example-{number}",
                    "section_ids": ids,
                }
                for number in range(12)
            )
            return RuntimeResult(json.dumps(data))
        return result


async def test_guide_advice_does_not_add_calls_repairs_or_delivery_blockers(store, inputs):
    request = JobInput(**inputs, runtime="demo", writing_guide=load_guide("srs"))
    baseline = AdviceRuntime()
    await Engine(store, baseline).run(store.create(request))

    job_id = store.create(request)
    runtime = AdviceRuntime(suggestions=True)
    job = await Engine(store, runtime).run(job_id)

    assert job["status"] == "draft"
    assert Path(job["artifact"]).is_file()
    assert job["snapshot"]["review_passed"]
    assert job["snapshot"]["blocking_issues"] == []
    assert all(state.revision == 1 and not state.local_findings for state in store.states(job_id))
    assert Counter(runtime.calls) == Counter(baseline.calls)
    assert not {"repair", "drill_down"} & set(runtime.calls)
    notes = list((store.root / "jobs" / job_id / "contexts" / "advisory_notes").glob("*.json"))
    assert notes
    assert any("optional-example" in path.read_text(encoding="utf-8") for path in notes)

    before = runtime.calls.copy()
    await Engine(store, runtime).run(job_id)
    assert runtime.calls == before
    assert all(path.exists() for path in notes)


async def test_mixed_review_preserves_real_conflict_even_if_misclassified_as_guide(store, inputs):
    class MixedRuntime(DemoRuntime):
        calls = 0

        async def complete(self, task, context, schema, session_key):
            self.calls += 1
            return RuntimeResult(
                json.dumps(
                    {
                        "findings": [
                            {
                                "kind": kind,
                                "basis": "guidance",
                                "description": description,
                                "section_ids": ["a", "b"],
                                "key": key,
                            }
                            for kind, description, key in [
                                ("coverage", "可增加验收示例", "optional-example"),
                                (
                                    "conflict",
                                    "同一撤销操作，a要求主管审批，b明确禁止经过主管审批",
                                    "approval-conflict",
                                ),
                            ]
                        ]
                    }
                )
            )

    runtime = MixedRuntime()
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo", writing_guide=load_guide("srs"))
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    context = {"sections": [{"section_id": "a"}, {"section_id": "b"}]}
    result = await engine._call("review", context, Review)
    assert [finding.key for finding in result.findings] == ["approval-conflict"]
    assert runtime.calls == 1
    cached = await engine._call("review", context, Review)
    assert cached == result
    assert runtime.calls == 1
