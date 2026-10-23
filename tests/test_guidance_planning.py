import json
import sys

import httpx
import pytest

from folio.cli import main
from folio.document.guidance import guidance_context, load_guide
from folio.document.planner import planning_context
from folio.document.planning_transport import planning_payload, planning_text
from folio.document.prepare import prepare_with_model
from folio.model_settings import ModelSettingsInput, ModelSettingsStore
from folio.models import PlanItem, PlanProposal
from folio.runtime.base import RuntimeResult, prompt_chars
from folio.runtime.llm import LLMRuntime
from folio.store import Store


def proposal(context):
    return PlanProposal(
        sections=[
            PlanItem(
                node_id="business",
                title="交易需求",
                level=1,
                instruction="按用户要求描述交易",
            )
        ]
    )


@pytest.mark.parametrize("mode", ["plan", "repair_plan", "revise_plan"])
async def test_preview_keeps_guide_in_measured_request_and_existing_retry_flow(
    inputs, tmp_path, monkeypatch, mode
):
    guide = load_guide("srs")
    measured = []
    calls = []
    rendered = []
    source = planning_context(inputs["user_request"], [])

    def measure(runtime, task, context, schema):
        measured.append((task, json.loads(json.dumps(context))))
        return prompt_chars(runtime, task, context, schema)

    class Runtime:
        async def complete(self, task, context, schema, session):
            assert measured[-1] == (task, context)
            assert context["writing_guidance"]
            assert context["requirements"] == source["requirements"]
            wire = planning_payload(context)
            assert wire["writing_guidance"] == context["writing_guidance"]
            assert "writing_guidance" in planning_text(wire)
            calls.append(task)
            result = proposal(context)
            if task == "revise_plan":
                return RuntimeResult(
                    json.dumps(
                        {
                            "operations": [
                                {
                                    "op": "update",
                                    "node_id": "business",
                                    "field": "title",
                                    "value_json": json.dumps("用户修改的交易需求"),
                                }
                            ],
                            "summary": "按用户要求调整标题",
                        }
                    )
                )
            if task == "repair_plan":
                return RuntimeResult(
                    json.dumps(
                        {
                            "edits": [
                                {
                                    "op": "replace",
                                    "path": "/sections/0/instruction",
                                    "value_json": json.dumps(result.sections[0].instruction),
                                }
                            ],
                        }
                    )
                )
            if mode == "repair_plan":
                result.sections[0].instruction = ""
            return RuntimeResult(result.model_dump_json())

    def render_preview(*args, plan, **kwargs):
        rendered.append(plan)
        return {"status": "layout_preview"}

    monkeypatch.setattr("folio.runtime.base.prompt_chars", measure)
    monkeypatch.setattr("folio.document.prepare.prepare", render_preview)
    extra = {"previous_proposal": proposal(source).model_dump()} if mode == "revise_plan" else {}
    output = tmp_path / "preview"
    result = await prepare_with_model(
        inputs["user_request"],
        [],
        output,
        runtime=Runtime(),
        writing_guide=guide,
        context_extra=extra,
    )

    assert result["status"] == "layout_preview"
    assert calls == (["plan", "repair_plan"] if mode == "repair_plan" else [mode])
    assert len(rendered) == 1
    assert rendered[0].sections[0].instruction == "按用户要求描述交易"
    assert json.loads((output / "writing-guide.json").read_text()) == guide.model_dump()
    assert json.loads((output / "planning-input.json").read_text())["writing_guidance"]


@pytest.mark.parametrize("structured", ["json_schema", "prompt"])
async def test_planning_http_measurement_includes_advisory_guide(inputs, tmp_path, structured):
    settings = ModelSettingsStore(tmp_path / "settings")
    settings.save(
        ModelSettingsInput(
            base_url="http://example.test/v1",
            model="test",
            no_auth=True,
            structured_output=structured,
        )
    )
    runtime = LLMRuntime(settings.path)
    await runtime.client.aclose()
    context = guidance_context(
        "plan",
        planning_context(inputs["user_request"], []),
        load_guide("srs"),
    )
    schema = PlanProposal.model_json_schema()
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert "writing_guidance" in payload["messages"][1]["content"]
        assert "Software Requirements Specification" in payload["messages"][1]["content"]
        assert prompt_chars(runtime, "plan", context, schema) == sum(
            len(message["content"]) for message in payload["messages"]
        )
        event = {"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n",
        )

    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await runtime.complete("plan", context, schema, "test-guide")
        assert len(calls) == 1
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("document_type", [None, "urd", "srs"])
def test_cli_generate_saves_selected_snapshot_once(
    inputs, tmp_path, monkeypatch, capsys, document_type
):
    loaded = []

    def load(identifier):
        loaded.append(identifier)
        return load_guide(identifier)

    root = tmp_path / "jobs"
    args = [
        "folio",
        "--data-dir",
        str(root),
        "generate",
        "--request",
        inputs["user_request"],
        "--knowledge",
        inputs["knowledge_path"],
        "--enqueue",
    ]
    if document_type:
        args += ["--document-type", document_type]
    monkeypatch.setattr("folio.cli.load_guide", load)
    monkeypatch.setattr(sys, "argv", args)
    main()
    job_id = capsys.readouterr().out.splitlines()[0].removeprefix("job_id=")
    job = Store(root).get(job_id)
    assert loaded == ([document_type] if document_type else [])
    assert job["input"].get("writing_guide") == (
        load_guide(document_type).model_dump() if document_type else None
    )


def test_cli_prepare_passes_snapshot_once(tmp_path, monkeypatch, capsys):
    calls = []
    loaded = []
    sources = tmp_path / "sources"
    sources.mkdir()

    class Runtime:
        def __init__(self, config):
            pass

        async def aclose(self):
            calls.append("close")

    async def prepare(*args, **kwargs):
        calls.append(kwargs["writing_guide"])
        return dict.fromkeys(
            [
                "status",
                "artifact",
                "plan_nodes",
                "content_sections",
                "unresolved_comments",
                "missing_sections",
            ]
        )

    def load(identifier):
        loaded.append(identifier)
        return load_guide(identifier)

    monkeypatch.setattr("folio.cli.load_guide", load)
    monkeypatch.setattr("folio.runtime.llm.LLMRuntime", Runtime)
    monkeypatch.setattr("folio.service.resolve_model_config", lambda *args: None)
    monkeypatch.setattr("folio.document.prepare.prepare_with_model", prepare)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "folio",
            "--data-dir",
            str(tmp_path / "data"),
            "prepare",
            "--request",
            "生成业务需求说明书",
            "--knowledge",
            str(sources),
            "--output-dir",
            str(tmp_path / "preview"),
            "--document-type",
            "srs",
        ],
    )
    main()
    assert loaded == ["srs"]
    assert calls == [load_guide("srs"), "close"]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("guide_id, explicit_title, expected", [
    ("srs", None, "Software Requirements Specification"),
    ("urd", None, "User Requirements Document"),
    ("srs", "软件需求规格说明书", "软件需求规格说明书"),
])
async def test_builtin_guide_title_fallback_follows_document_language(
    tmp_path, monkeypatch, guide_id, explicit_title, expected
):
    class Runtime:
        async def complete(self, task, context, schema, session):
            return RuntimeResult(PlanProposal(sections=[PlanItem(
                node_id="scope", title="Scope", level=1, instruction="Describe the supplied requirements",
            )]).model_dump_json())

    rendered = []

    def render_preview(*args, plan, title, language, **kwargs):
        rendered.append((plan.document_title, title, language))
        return {"status": "layout_preview"}

    monkeypatch.setattr("folio.document.prepare.prepare", render_preview)
    await prepare_with_model(
        "Write a requirements document", [], tmp_path / "preview", runtime=Runtime(),
        writing_guide=load_guide(guide_id),
        context_extra={"ui_language": "zh-CN", "document_title_requirement": explicit_title},
    )
    assert rendered == [(expected, expected, "en")]
