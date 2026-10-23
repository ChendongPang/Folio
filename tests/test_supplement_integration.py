"""Supplements produce bounded edits while untouched prose and provenance survive."""

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from folio.models import Block, JobInput, SectionContent, digest
from folio.orchestration.engine import Engine
from folio.orchestration.supplement_conflicts import SupplementCheckResult
from folio.orchestration.supplement_integration import (
    SupplementIntegration,
    apply_integration,
    integrate_supplements,
    retain_pending,
    validate_checked_patch,
)
from folio.runtime.base import RuntimeResult
from folio.runtime.business_transport import business_payload, translate_ids, unpack_rows
from folio.runtime.demo import DemoRuntime
from folio.runtime.errors import ModelCallFailed


def proposal(gap="alpha", *, disposition="body"):
    return {
        "gap_id": gap,
        "section_ids": ["s"],
        "content": gap + "补充业务方案说明适用范围，未经审批不得继续处理。",
        "disposition": disposition,
    }


def current_content():
    return SectionContent.model_validate(
        {
            "blocks": [
                {"block_id": "fact", "text": "既定业务事实。", "evidence_ids": ["source_alpha"]},
                {
                    "block_id": "table",
                    "kind": "table",
                    "rows": [["条件", "规则"], ["未审批", "不得处理"]],
                    "evidence_ids": ["source_beta"],
                },
                {"block_id": "old", "text": "已有补写方案。", "origin": "model_proposal"},
                {"block_id": "duplicate", "text": "重复补写方案。", "origin": "model_proposal"},
            ],
            "pending_items": ["待补充适用范围", "审核日期待确认"],
            "visuals": [
                {
                    "figure_id": "flow",
                    "kind": "flowchart",
                    "caption": "业务流程",
                    "description": "展示既定业务事实。",
                    "source_block_ids": ["fact"],
                    "after_block_id": "fact",
                }
            ],
        }
    )


def source_context():
    return {
        "section": {"section_id": "s", "title": "约束和假设"},
        "current": current_content().model_dump(),
        "model_proposals": [proposal("alpha"), proposal("beta")],
        "peer_sections": [{"section_id": "unrelated", "content": "其他章节完整正文"}],
        "task_sections": [{"section_id": "other", "title": "其他章节"}],
        "child_sections": [{"section_id": "child", "title": "其他子节"}],
        "known_gaps": ["其他章节缺口"],
        "evidence": [
            {
                "evidence_id": "source_" + topic,
                "source_id": "original",
                "text": topic + "原始业务规则：未经审批不得处理。末尾完整原文。",
            }
            for topic in ("alpha", "beta")
        ],
        "business_record": {
            "summary": "整个业务任务的宽泛摘要",
            "terminology": ["其他模块术语"],
            "facts": [
                {
                    "fact_id": topic,
                    "statement": topic + "业务事实",
                    "evidence_ids": ["source_" + topic],
                }
                for topic in ("alpha", "beta")
            ],
            "conflicts": [topic + "未决冲突" for topic in ("alpha", "beta")],
            "open_questions": [topic + "待核实业务条件" for topic in ("alpha", "beta")],
        },
        "model_completion_allowed": True,
    }


def test_apply_integration_preserves_untouched_blocks_tables_visuals_and_order():
    current = current_content()
    original = current.model_dump()
    patch = SupplementIntegration(
        updates=[
            Block(block_id="old", text="整合后的补写方案。", origin="model_proposal"),
            Block(block_id="new", text="新增业务方案。", origin="model_proposal"),
        ],
        remove_block_ids=["duplicate"],
        resolved_pending_items=["待补充适用范围"],
    )
    result = apply_integration(current, patch, {"source_alpha", "source_beta"})
    assert [block.block_id for block in result.blocks] == ["fact", "table", "old", "new"]
    assert result.blocks[:2] == current.blocks[:2]
    assert result.visuals == current.visuals
    assert result.pending_items == ["审核日期待确认"]
    assert all(
        block.origin == "model_proposal" and not block.evidence_ids for block in result.blocks[2:]
    )
    assert current.model_dump() == original


@pytest.mark.parametrize("anchor", ["source_block_ids", "after_block_id"])
def test_supplement_keeps_figure_anchor_identity_and_allows_in_place_update(anchor):
    current = current_content()
    visual = current.visuals[0]
    setattr(visual, anchor, ["old"] if anchor == "source_block_ids" else "old")
    before = current.model_dump()
    replacement = Block(block_id="si1", text="补全后的风控检查方案。", origin="model_proposal")
    patch = SupplementIntegration(updates=[replacement], remove_block_ids=["old"])
    with pytest.raises(ValueError, match="cannot remove figure source or insertion blocks: old"):
        apply_integration(current, patch, {"source_alpha", "source_beta"})
    assert current.model_dump() == before

    # The model can issue the same business edit using the existing identity;
    # the backend must not infer a new source mapping or discard the figure.
    in_place = SupplementIntegration(
        updates=[replacement.model_copy(update={"block_id": "old"})]
    )
    result = apply_integration(current, in_place, {"source_alpha", "source_beta"})
    assert [block.block_id for block in result.blocks] == [block.block_id for block in current.blocks]
    assert result.blocks[2].text == replacement.text
    assert result.visuals == current.visuals
    assert current.model_dump() == before


def test_supplement_cannot_update_and_remove_same_target_or_remove_unknown_target():
    current = current_content()
    before = current.model_dump()
    update = current.blocks[2].model_copy(update={"text": "重新整理的补写。"})
    for target in ("old", "unknown_block"):
        with pytest.raises(ValueError, match="Unknown or overlapping supplement edit target"):
            apply_integration(
                current,
                SupplementIntegration(updates=[update], remove_block_ids=[target]),
                {"source_alpha", "source_beta"},
            )
        assert current.model_dump() == before


@pytest.mark.parametrize("anchor", ["source_block_ids", "after_block_id"])
@pytest.mark.parametrize("long_id", [False, True])
def test_wire_schema_forbids_figure_anchor_removal_but_keeps_it_editable(anchor, long_id):
    from jsonschema import Draft202012Validator

    block_id = "figure_source_supplement_" + "a" * 40 if long_id else "old"
    source = source_context() | {"integration_item": proposal()}
    source["current"]["blocks"][2]["block_id"] = block_id
    source["current"]["visuals"][0][anchor] = (
        [block_id] if anchor == "source_block_ids" else block_id
    )
    engine = object.__new__(Engine)
    schema = engine.request_schema("integrate_supplement", source, SupplementIntegration)
    wire, actual, refs = business_payload(source, schema)
    assert translate_ids(unpack_rows(wire), refs) == source
    alias = {original: short for short, original in refs.items()}.get(block_id, block_id)
    assert alias not in actual["properties"]["remove_block_ids"]["items"]["enum"]
    assert actual["properties"]["remove_block_ids"]["items"]["enum"] == ["duplicate"]
    bad_patch = {"updates": [], "remove_block_ids": [alias], "resolved_pending_items": []}
    assert list(Draft202012Validator(actual).iter_errors(bad_patch))
    valid_patch = {
        "updates": [
            {
                "block_id": alias,
                "text": "补全后的业务方案。",
                "origin": "model_proposal",
                "evidence_ids": [],
            }
        ],
        "remove_block_ids": [],
        "resolved_pending_items": [],
    }
    assert not list(Draft202012Validator(actual).iter_errors(valid_patch))


@pytest.mark.parametrize("kind", ["paragraph", "table"])
def test_replayed_identical_model_supplement_with_new_id_is_not_appended(kind):
    current = current_content()
    if kind == "table":
        current.blocks[2] = Block(
            block_id="old",
            kind="table",
            rows=[["条件", "建议"], ["异常", "通知复核"]],
            origin="model_proposal",
        )
    before = current.model_dump()
    duplicate = current.blocks[2].model_copy(update={"block_id": "retry_new_id"})
    result = apply_integration(
        current, SupplementIntegration(updates=[duplicate]), {"source_alpha", "source_beta"}
    )
    assert result == current
    assert all(block.block_id != "retry_new_id" for block in result.blocks)
    retried = apply_integration(
        result,
        SupplementIntegration(updates=[duplicate.model_copy(update={"block_id": "another_id"})]),
        {"source_alpha", "source_beta"},
    )
    assert retried == current and current.model_dump() == before


@pytest.mark.parametrize("cited", [True, False])
def test_only_uncited_existing_text_can_be_reclassified_as_model_supplement(cited):
    current = current_content()
    if not cited:
        current.blocks[0].evidence_ids = []
    before = current.model_dump()
    reclassified = current.blocks[0].model_copy(
        update={"origin": "model_proposal", "evidence_ids": []}
    )
    patch = SupplementIntegration(updates=[reclassified])
    if cited:
        with pytest.raises(ValueError, match="reclassify cited facts"):
            apply_integration(current, patch, {"source_alpha", "source_beta"})
    else:
        result = apply_integration(current, patch, {"source_beta"})
        assert result.blocks[0] == reclassified
        assert result.blocks[0].text == current.blocks[0].text
        assert result.blocks[1:] == current.blocks[1:]
        assert result.visuals == current.visuals
    assert current.model_dump() == before


def checked_case(case):
    current = current_content()
    approved = proposal()
    if case == "noop":
        current.blocks[2].text = approved["content"]
    patch = SupplementIntegration()
    if case in {"exact", "changed"}:
        patch.updates = [
            Block(
                block_id="checked_addition",
                text=approved["content"]
                if case == "exact"
                else "未经审批允许直接处理，与已核对方案相反。",
                origin="model_proposal",
            )
        ]
    return current, approved, patch


@pytest.mark.parametrize("case", ["exact", "noop", "changed", "missing"])
def test_checked_patch_requires_exact_adoption_or_existing_approved_text(case):
    current, approved, patch = checked_case(case)
    before = current.model_dump()
    if case in {"changed", "missing"}:
        with pytest.raises(ValueError, match="source-checked|not integrated"):
            validate_checked_patch(current, patch, approved)
    else:
        assert validate_checked_patch(current, patch, approved) is None
        adopted = apply_integration(current, patch, {"source_alpha", "source_beta"})
        assert sum(block.text == approved["content"] for block in adopted.blocks) == 1
    assert current.model_dump() == before


@pytest.mark.parametrize("case", ["exact", "noop", "changed", "missing"])
async def test_engine_rejects_changed_or_missing_checked_placement_before_caching(
    store, inputs, case
):
    current, approved, patch = checked_case(case)
    context = source_context() | {
        "current": current.model_dump(),
        "integration_item": approved,
        "model_proposals": [approved],
        "supplement_conflicts_checked": True,
        "existing_evidence_ids": ["source_alpha", "source_beta"],
    }

    class PlacementRuntime(IntegrationRuntime):
        async def complete(self, task, received, schema, session):
            assert task == "integrate_supplement"
            self.integrations.append(deepcopy(received))
            return RuntimeResult(patch.model_dump_json())

    runtime = PlacementRuntime()
    engine = engine_for(store, inputs, runtime)
    if case in {"changed", "missing"}:
        with pytest.raises(ValueError, match="source-checked|not integrated"):
            await engine._call("integrate_supplement", context, SupplementIntegration)
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT result, error FROM tasks WHERE job_id=?", (engine.job_id,)
            ).fetchall()
        assert rows and all(row["result"] is None and row["error"] for row in rows)
    else:
        assert await engine._call("integrate_supplement", context, SupplementIntegration) == patch
        count = len(runtime.integrations)
        assert await engine._call("integrate_supplement", context, SupplementIntegration) == patch
        assert len(runtime.integrations) == count


@pytest.mark.parametrize(
    "patch",
    [
        {"updates": [{"block_id": "new", "text": "重复标识", "origin": "model_proposal"}] * 2},
        {"remove_block_ids": ["old", "old"]},
        {"remove_block_ids": ["unknown"]},
        {"remove_block_ids": ["fact"]},
        {"remove_block_ids": ["old"], "updates": [{"block_id": "old", "text": "重叠修改"}]},
        {"resolved_pending_items": ["不存在的待确认项"]},
        {
            "updates": [
                {
                    "block_id": "new",
                    "text": "补写不得冒充已证实原文。",
                    "evidence_ids": ["source_alpha"],
                }
            ]
        },
        {
            "updates": [
                {"block_id": "fact", "text": "既定业务事实。", "evidence_ids": ["unknown_source"]}
            ]
        },
        {
            "updates": [
                {
                    "block_id": "fact",
                    "text": "未经审批也允许处理。",
                    "evidence_ids": ["source_alpha"],
                }
            ]
        },
        {
            "updates": [
                {
                    "block_id": "fact",
                    "text": "改成红字仍然不能覆盖既定业务事实。",
                    "origin": "model_proposal",
                    "evidence_ids": [],
                }
            ]
        },
        {
            "updates": [
                {
                    "block_id": "table",
                    "kind": "table",
                    "rows": [["条件", "规则"], ["未审批", "允许处理"]],
                    "origin": "model_proposal",
                    "evidence_ids": [],
                }
            ]
        },
    ],
)
def test_apply_rejects_invalid_or_unattributed_edits_without_mutating_current(patch):
    current = current_content()
    before = current.model_dump()
    with pytest.raises(ValueError):
        apply_integration(
            current, SupplementIntegration.model_validate(patch), {"source_alpha", "source_beta"}
        )
    assert current.model_dump() == before


def test_supplement_cannot_delete_even_a_model_origin_table():
    current = current_content()
    current.blocks[1].origin = "model_proposal"
    current.blocks[1].evidence_ids = []
    with pytest.raises(ValueError):
        apply_integration(
            current, SupplementIntegration(remove_block_ids=["table"]), {"source_alpha"}
        )


@pytest.mark.parametrize("count", [2, 50])
def test_retain_pending_keeps_every_old_message_and_deduplicates_new_reason(count):
    current = current_content()
    current.pending_items = [f"既有待确认项{i}" for i in range(count)]
    before = current.model_dump()
    result = retain_pending(current, proposal(), "原文约束尚未完成核对")
    assert result.blocks == current.blocks and result.visuals == current.visuals
    assert len(result.pending_items) <= 50
    assert all(item in "；".join(result.pending_items) for item in current.pending_items)
    assert "alpha" in result.pending_items[-1]
    assert "原文约束尚未完成核对" in result.pending_items[-1]
    assert retain_pending(result, proposal(), "原文约束尚未完成核对") == result
    assert current.model_dump() == before


class IntegrationRuntime(DemoRuntime):
    def __init__(self, *, overflow=False, provider_rejection=False, malicious_record_only=False):
        self.overflow = overflow
        self.provider_rejection = provider_rejection
        self.malicious_record_only = malicious_record_only
        self.selections = []
        self.integrations = []

    async def measure(self, task, context, schema, char_limit):
        size = len(json.dumps(context, ensure_ascii=False))
        fits = not (
            task == "integrate_supplement"
            and self.overflow
            and not context.get("supplement_conflicts_checked")
        )
        return {"characters": size, "fits": fits}

    async def complete(self, task, context, schema, session):
        if task == "select_context":
            self.selections.append(deepcopy(context))
            topic = context["purpose"]["supplement"]["gap_id"]
            chosen = [
                item
                for item in context["catalog"]
                if topic in json.dumps(item["value"], ensure_ascii=False)
            ]
            risks = [item["context_ref"] for item in chosen if item["kind"] == "risk"]
            return RuntimeResult(
                json.dumps(
                    {
                        "selected_refs": [
                            item["context_ref"] for item in chosen if item["kind"] != "risk"
                        ],
                        "risk_refs": risks,
                        "risk_summary": "本项尚有待核实约束" if risks else "",
                    }
                )
            )
        assert task == "integrate_supplement"
        self.integrations.append(deepcopy(context))
        if self.provider_rejection and not context.get("supplement_conflicts_checked"):
            raise ModelCallFailed("request rejected", "context_limit")
        item = context["integration_item"]
        if item["disposition"] == "record_only" and not self.malicious_record_only:
            return RuntimeResult('{"updates":[],"remove_block_ids":[],"resolved_pending_items":[]}')
        return RuntimeResult(
            json.dumps(
                {
                    "updates": [
                        {
                            "block_id": "added_" + item["gap_id"],
                            "text": item["content"],
                            "origin": "model_proposal",
                            "evidence_ids": [],
                        }
                    ],
                    "remove_block_ids": [],
                    "resolved_pending_items": [],
                },
                ensure_ascii=False,
            )
        )


def engine_for(store, inputs, runtime):
    engine = Engine(store, runtime)
    engine.request = JobInput(**inputs, runtime="demo")
    engine.limits = engine.request.limits
    engine.job_id = store.create(engine.request)
    return engine


async def test_each_supplement_selects_its_own_materials_and_sees_prior_accepted_edit(
    store, inputs
):
    source = source_context()
    before = deepcopy(source)
    runtime = IntegrationRuntime()
    engine = engine_for(store, inputs, runtime)
    result = await integrate_supplements(engine, source, source["model_proposals"])
    assert len(runtime.integrations) == 2
    for index, item in enumerate(runtime.integrations):
        topic = ("alpha", "beta")[index]
        assert item["model_proposals"] == [proposal(topic)]
        assert item["integration_item"] == proposal(topic)
        assert {e["evidence_id"] for e in item["evidence"]} == {"source_" + topic}
        assert item["business_record"] == {
            "facts": [before["business_record"]["facts"][index]],
            "conflicts": [topic + "未决冲突"],
            "open_questions": [topic + "待核实业务条件"],
        }
        assert not {"peer_sections", "task_sections", "child_sections", "known_gaps"} & item.keys()
        assert [block["block_id"] for block in item["current"]["blocks"]] == [
            "fact",
            "table",
            "old",
            "duplicate",
            *(["added_alpha"] if index else []),
        ]
    assert {page["purpose"]["action"] for page in runtime.selections} == {
        "integrate_one_supplement"
    }
    assert result.blocks[:4] == current_content().blocks
    assert [block.block_id for block in result.blocks[-2:]] == ["added_alpha", "added_beta"]
    assert result.visuals == current_content().visuals
    assert source == before


@pytest.mark.parametrize("provider_rejection", [False, True])
async def test_capacity_fallback_integrates_only_checked_proposal_and_exact_quotes(
    store, inputs, monkeypatch, provider_rejection
):
    source = source_context()
    before = deepcopy(source)
    runtime = IntegrationRuntime(
        overflow=not provider_rejection, provider_rejection=provider_rejection
    )
    engine = engine_for(store, inputs, runtime)
    checked = proposal() | {"content": "alpha经过原始约束核对的方案，未经主管审批不得处理。"}
    seen = []

    async def check(_engine, section, proposals, materials, **kwargs):
        assert kwargs["budget"] is not None
        seen.append(deepcopy(materials))
        original = next(item for item in materials if item["kind"] == "evidence")
        assert original["value"]["text"] == source["evidence"][0]["text"]
        return SupplementCheckResult(
            "checked",
            [checked],
            [
                {
                    "gap_ids": ["alpha"],
                    "source_ref": original["context_ref"],
                    "source_kind": "evidence",
                    "quote": "未经审批不得处理。",
                    "reason": "保留已核对原文约束",
                }
            ],
        )

    monkeypatch.setattr(
        "folio.orchestration.supplement_conflicts.check_supplement_conflicts", check
    )
    result = await integrate_supplements(engine, source, [proposal()])
    assert len(seen) == 1
    assert len(runtime.integrations) == 1 + int(provider_rejection)
    compact = runtime.integrations[-1]
    assert compact["supplement_conflicts_checked"]
    assert compact["integration_item"] == checked and compact["model_proposals"] == [checked]
    assert compact["evidence"][0]["text"] == "未经审批不得处理。"
    assert compact["evidence"][0]["excerpt_only"] is True
    assert "later validation will read the full originals" in compact["repair_instruction"]
    assert result.blocks[-1].text == checked["content"]
    assert result.blocks[-1].origin == "model_proposal" and result.blocks[-1].evidence_ids == []
    assert result.blocks[:-1] == current_content().blocks
    assert source == before


async def test_pending_conflict_check_never_adopts_partial_correction(store, inputs, monkeypatch):
    source = source_context()
    runtime = IntegrationRuntime(overflow=True)
    engine = engine_for(store, inputs, runtime)
    unfinished = proposal() | {"content": "不可采用的中间修订，尚未完成全部原文冲突核对。"}
    check = AsyncMock(
        return_value=SupplementCheckResult("pending", [unfinished], reason="部分原文尚未核实")
    )
    monkeypatch.setattr(
        "folio.orchestration.supplement_conflicts.check_supplement_conflicts", check
    )
    result = await integrate_supplements(engine, source, [proposal()])
    check.assert_awaited_once()
    assert runtime.integrations == []
    assert result.blocks == current_content().blocks and result.visuals == current_content().visuals
    assert result.pending_items[:-1] == current_content().pending_items
    assert "部分原文尚未核实" in result.pending_items[-1]
    assert "不可采用的中间修订" not in result.model_dump_json()


@pytest.mark.parametrize("malicious", [False, True])
async def test_record_only_supplement_never_changes_document_body(store, inputs, malicious):
    source = source_context()
    runtime = IntegrationRuntime(malicious_record_only=malicious)
    engine = engine_for(store, inputs, runtime)
    result = await integrate_supplements(engine, source, [proposal(disposition="record_only")])
    assert result == current_content()
    assert runtime.selections == runtime.integrations == []
    assert store.get(engine.job_id)["calls"] == 0
    records = list(
        (store.root / "jobs" / engine.job_id / "contexts" / "supplement_integrations").glob(
            "*.json"
        )
    )
    assert len(records) == 1
    archived = json.loads(records[0].read_text())
    assert archived["mode"] == "record_only"
    assert archived["proposal"] == proposal(disposition="record_only")
    assert source == source_context()


async def test_already_present_supplement_checks_changed_sources_without_duplicating_body(
    store, inputs
):
    source = source_context()
    source["current"]["blocks"][2]["text"] = proposal()["content"]
    runtime = IntegrationRuntime()
    engine = engine_for(store, inputs, runtime)
    for version in (1, 2):
        source["evidence"][0]["text"] = f"alpha 第{version}版否定约束：未经审批不得继续处理。"
        before = deepcopy(source)
        result = await integrate_supplements(engine, source, [proposal()])
        assert result.model_dump() == source["current"]
        assert source == before
        assert len(runtime.integrations) == version
        assert runtime.integrations[-1]["evidence"][0]["text"] == source["evidence"][0]["text"]
    assert len(runtime.selections) == 2
    records = list(
        (store.root / "jobs" / engine.job_id / "contexts" / "supplement_integrations").glob(
            "*.json"
        )
    )
    assert len(records) == 2
    assert {json.loads(record.read_text())["mode"] for record in records} == {"direct"}


async def test_existing_exact_supplement_does_not_clear_pending_without_current_source_check(
    store, inputs, monkeypatch
):
    source = source_context()
    source["current"]["blocks"][2]["text"] = proposal()["content"]
    source["evidence"][0]["text"] = "alpha新版本明确禁止当前方案，尚待核对适用条件。"
    runtime = IntegrationRuntime(overflow=True)
    engine = engine_for(store, inputs, runtime)
    check = AsyncMock(return_value=SupplementCheckResult("pending", [], reason="否定条件尚待核实"))
    monkeypatch.setattr(
        "folio.orchestration.supplement_conflicts.check_supplement_conflicts", check
    )
    result = await integrate_supplements(engine, source, [proposal()])
    check.assert_awaited_once()
    assert runtime.integrations == []
    assert result.blocks == SectionContent.model_validate(source["current"]).blocks
    assert result.pending_items[:-1] == source["current"]["pending_items"]
    assert "否定条件尚待核实" in result.pending_items[-1]


async def test_fallback_items_share_one_conflict_check_budget(store, inputs, monkeypatch):
    source = source_context()
    runtime = IntegrationRuntime(overflow=True)
    engine = engine_for(store, inputs, runtime)
    budgets = []

    async def check(_engine, section, proposals, materials, *, budget):
        budgets.append(budget)
        return SupplementCheckResult("checked", deepcopy(proposals))

    monkeypatch.setattr(
        "folio.orchestration.supplement_conflicts.check_supplement_conflicts", check
    )
    result = await integrate_supplements(engine, source, source["model_proposals"])
    assert len(budgets) == 2 and budgets[0] is budgets[1]
    assert [block.block_id for block in result.blocks[-2:]] == ["added_alpha", "added_beta"]
    assert all(context["supplement_conflicts_checked"] for context in runtime.integrations)


async def test_each_item_receives_only_its_original_gap_description(store, inputs):
    source = source_context()
    descriptions = ["alpha缺口：明确应补哪一项审批规则", "beta缺口：明确应补哪一项异常规则"]
    proposals = [
        proposal(topic) | {"gap_id": digest(description)}
        for topic, description in zip(("alpha", "beta"), descriptions, strict=True)
    ]
    source["integration_gaps"] = descriptions
    source["model_proposals"] = proposals
    before = deepcopy(source)
    runtime = IntegrationRuntime()
    engine = engine_for(store, inputs, runtime)
    result = await integrate_supplements(engine, source, proposals)
    assert len(runtime.integrations) == 2
    for index, context in enumerate(runtime.integrations):
        assert "integration_gaps" not in context
        assert context["integration_item"]["gap_description"] == descriptions[index]
        assert context["model_proposals"] == [proposals[index]]
        assert descriptions[1 - index] not in json.dumps(context, ensure_ascii=False)
    assert len(runtime.selections) == 2
    for index, context in enumerate(runtime.selections):
        assert context["purpose"]["supplement"]["gap_description"] == descriptions[index]
        assert descriptions[1 - index] not in json.dumps(context, ensure_ascii=False)
    assert len(result.blocks) == len(current_content().blocks) + 2
    assert source == before


async def test_engine_patch_namespace_keeps_old_citations_and_roundtrips_removed_block_ids(
    store, inputs
):
    old_ref = "historical_evidence_" + "a" * 40
    selected_ref = "selected_evidence_" + "b" * 40
    old_block = "existing_fact_block_" + "c" * 40
    redundant_block = "redundant_proposal_" + "d" * 40
    current = current_content()
    current.blocks[0].block_id = old_block
    current.blocks[0].evidence_ids = [old_ref]
    current.blocks[3].block_id = redundant_block
    current.visuals[0].source_block_ids = [old_block]
    current.visuals[0].after_block_id = old_block
    patch = SupplementIntegration(updates=[current.blocks[0]], remove_block_ids=[redundant_block])
    context = {
        "section": {"section_id": "s", "title": "业务规则"},
        "current": current.model_dump(),
        "integration_item": proposal(),
        "model_proposals": [proposal()],
        "evidence": [{"evidence_id": selected_ref, "source_id": "new-doc", "text": "相关新原文"}],
        "existing_evidence_ids": [old_ref, "source_beta"],
    }

    class ExistingCitationRuntime(IntegrationRuntime):
        async def complete(self, task, received, schema, session):
            assert task == "integrate_supplement"
            self.integrations.append(deepcopy(received))
            for branch in schema["$defs"]["Block"]["anyOf"]:
                fields = branch["properties"]
                if old_block not in fields["block_id"]["enum"]:
                    assert fields["evidence_ids"]["maxItems"] == 0
                    continue
                assert set(fields["evidence_ids"]["items"]["enum"]) == {
                    old_ref,
                    selected_ref,
                    "source_beta",
                }
            return RuntimeResult(patch.model_dump_json())

    runtime = ExistingCitationRuntime()
    engine = engine_for(store, inputs, runtime)
    result = await engine._call("integrate_supplement", context, SupplementIntegration)
    assert result == patch and len(runtime.integrations) == 1
    schema = engine.request_schema("integrate_supplement", context, SupplementIntegration)
    wire, output_schema, references = business_payload(context, schema)
    assert translate_ids(unpack_rows(wire), references) == context
    aliases = {canonical: compact for compact, canonical in references.items()}
    encoded_patch = translate_ids(patch.model_dump(), aliases)
    assert encoded_patch["remove_block_ids"] == [aliases[redundant_block]]
    assert encoded_patch["updates"][0]["block_id"] == aliases[old_block]
    assert encoded_patch["updates"][0]["evidence_ids"] == [aliases[old_ref]]
    assert SupplementIntegration.model_validate(translate_ids(encoded_patch, references)) == patch
    for branch in output_schema["$defs"]["Block"]["anyOf"]:
        fields = branch["properties"]
        if aliases[old_block] in fields["block_id"]["enum"]:
            assert aliases[old_ref] in fields["evidence_ids"]["items"]["enum"]
        else:
            assert "evidence_ids" not in fields
    assert (
        apply_integration(current, result, {old_ref, selected_ref, "source_beta"}).blocks[0]
        == current.blocks[0]
    )


@pytest.mark.parametrize("change", ["remove", "text", "origin"])
def test_protected_supplement_blocks_reject_changes_but_allow_identical_noop(change):
    current = current_content()
    before = current.model_dump()
    patch = SupplementIntegration()
    if change == "remove":
        patch.remove_block_ids = ["old"]
    else:
        patch.updates = [
            current.blocks[2].model_copy(
                update={"text": "另一个方案不能抹掉此前独有流程。"}
                if change == "text"
                else {"origin": "evidence", "evidence_ids": ["source_alpha"]}
            )
        ]
    with pytest.raises(ValueError, match="protected block"):
        apply_integration(
            current, patch, {"source_alpha", "source_beta"}, protected_block_ids=["old"]
        )
    noop = SupplementIntegration(updates=[current.blocks[2].model_copy(deep=True)])
    assert (
        apply_integration(
            current, noop, {"source_alpha", "source_beta"}, protected_block_ids=["old"]
        )
        == current
    )
    assert current.model_dump() == before


class LaterItemOverwriteRuntime(IntegrationRuntime):
    def __init__(
        self, *, mutation="remove", target="added_alpha", overflow=False, cancel_once=False
    ):
        super().__init__(overflow=overflow)
        self.mutation, self.target, self.cancel_once = mutation, target, cancel_once
        self.cancelled_once = False

    async def complete(self, task, context, schema, session):
        result = await super().complete(task, context, schema, session)
        if task != "integrate_supplement" or context["integration_item"]["gap_id"] != "beta":
            return result
        assert self.target in context["protected_block_ids"]
        assert context["integration_policy"] == 2
        if self.cancel_once and not self.cancelled_once:
            import asyncio

            self.cancelled_once = True
            raise asyncio.CancelledError()
        patch = json.loads(result.content)
        if self.mutation == "remove":
            patch["remove_block_ids"] = [self.target]
        else:
            previous = next(b for b in context["current"]["blocks"] if b["block_id"] == self.target)
            patch["updates"].append(previous | {"text": "后项方案覆盖了前项独有的业务流程。"})
        return RuntimeResult(json.dumps(patch, ensure_ascii=False))


@pytest.mark.parametrize("overflow", [False, True])
@pytest.mark.parametrize("mutation", ["remove", "rewrite"])
async def test_later_item_cannot_erase_prior_accepted_supplement(
    store, inputs, monkeypatch, overflow, mutation
):
    source = source_context()
    before = deepcopy(source)
    runtime = LaterItemOverwriteRuntime(overflow=overflow, mutation=mutation)
    engine = engine_for(store, inputs, runtime)
    if overflow:

        async def check(_engine, section, proposals, materials, **kwargs):
            return SupplementCheckResult("checked", deepcopy(proposals))

        monkeypatch.setattr(
            "folio.orchestration.supplement_conflicts.check_supplement_conflicts", check
        )
    result = await integrate_supplements(engine, source, source["model_proposals"])
    assert [b.block_id for b in result.blocks] == [
        "fact",
        "table",
        "old",
        "duplicate",
        "added_alpha",
    ]
    assert result.blocks[-1].text == proposal("alpha")["content"]
    assert result.blocks[:-1] == current_content().blocks
    assert any(proposal("beta")["content"][:80] in item for item in result.pending_items)
    assert result.visuals == current_content().visuals and source == before
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT result,error FROM tasks WHERE job_id=? AND task_key LIKE 'integrate_supplement:%'",
            (engine.job_id,),
        ).fetchall()
    assert sum(row["result"] is not None for row in rows) == 1
    assert any(row["error"] for row in rows)


@pytest.mark.parametrize("exact", [False, True])
async def test_initial_block_belonging_to_other_proposal_uses_only_exact_text_protection(
    store, inputs, exact
):
    source = source_context()
    source["current"]["blocks"][2]["text"] = proposal("beta")["content"] + ("" if exact else " ")
    original = deepcopy(source)

    class ExistingOtherRuntime(IntegrationRuntime):
        async def complete(self, task, context, schema, session):
            result = await super().complete(task, context, schema, session)
            if task == "integrate_supplement":
                assert ("old" in context["protected_block_ids"]) is exact
                patch = json.loads(result.content)
                patch["remove_block_ids"] = ["old"]
                return RuntimeResult(json.dumps(patch))
            return result

    engine = engine_for(store, inputs, ExistingOtherRuntime())
    # beta is not being integrated in this invocation; its saved exact prose is
    # still owned by another known proposal and must remain protected.
    result = await integrate_supplements(engine, source, [proposal("alpha")])
    if exact:
        assert result.blocks == SectionContent.model_validate(source["current"]).blocks
        assert len(result.pending_items) == len(source["current"]["pending_items"]) + 1
    else:
        assert "old" not in {b.block_id for b in result.blocks}
        assert result.blocks[-1].text == proposal("alpha")["content"]
    assert source == original


async def test_deduplicated_prior_addition_protects_the_retained_original_id(store, inputs):
    source = source_context()
    source["current"]["blocks"][2]["text"] = proposal("alpha")["content"]
    runtime = LaterItemOverwriteRuntime(target="old")
    result = await integrate_supplements(
        engine_for(store, inputs, runtime), source, source["model_proposals"]
    )
    assert result.blocks == SectionContent.model_validate(source["current"]).blocks
    assert sum(b.text == proposal("alpha")["content"] for b in result.blocks) == 1
    assert any(proposal("beta")["content"][:80] in p for p in result.pending_items)


async def test_cancellation_and_cache_resume_rebuild_prior_item_protection(store, inputs):
    import asyncio

    source = source_context()
    before = deepcopy(source)
    runtime = LaterItemOverwriteRuntime(cancel_once=True)
    engine = engine_for(store, inputs, runtime)
    with pytest.raises(asyncio.CancelledError):
        await integrate_supplements(engine, source, source["model_proposals"])
    archives = list(
        (store.root / "jobs" / engine.job_id / "contexts/supplement_integrations").glob("*.json")
    )
    accepted = [
        json.loads(p.read_text()) for p in archives if json.loads(p.read_text())["mode"] == "direct"
    ]
    assert (
        len(accepted) == 1
        and accepted[0]["result"]["blocks"][-1]["text"] == proposal("alpha")["content"]
    )
    result = await integrate_supplements(engine, source, source["model_proposals"])
    assert sum(c["integration_item"]["gap_id"] == "alpha" for c in runtime.integrations) == 1
    assert result.blocks[-1].block_id == "added_alpha"
    assert result.blocks[-1].text == proposal("alpha")["content"]
    assert any(proposal("beta")["content"][:80] in p for p in result.pending_items)
    assert source == before


def test_protected_block_ids_round_trip_and_cannot_be_wire_edit_targets():
    from jsonschema import Draft202012Validator

    original_id = "accepted_supplement_" + "a" * 50
    source = source_context() | {"integration_item": proposal(), "protected_block_ids": [original_id]}
    source["current"]["blocks"][2]["block_id"] = original_id
    engine = object.__new__(Engine)
    schema = engine.request_schema("integrate_supplement", source, SupplementIntegration)
    wire, actual, refs = business_payload(source, schema)
    restored = translate_ids(unpack_rows(wire), refs)
    assert restored == source
    aliases = {canonical: short for short, canonical in refs.items()}
    protected = aliases[original_id]
    assert unpack_rows(wire)["protected_block_ids"] == [protected]
    for branch in actual["$defs"]["Block"]["anyOf"]:
        assert protected not in branch["properties"]["block_id"]["enum"]
    assert protected not in actual["properties"]["remove_block_ids"]["items"]["enum"]
    patch = {"updates": [], "remove_block_ids": [protected], "resolved_pending_items": []}
    assert list(Draft202012Validator(actual).iter_errors(patch))
    assert "uniqueItems" not in json.dumps(actual)
