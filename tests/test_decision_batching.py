"""Relevance batching never drops original text or changes the shared question."""

import json
from copy import deepcopy

import pytest

from folio.decisions.batching import (
    DECISION_CONTEXT_CHARS,
    DecisionBatchingError,
    context_chars,
    decision_pages,
)


def catalog_item(ref, value, **metadata):
    return {"context_ref": ref, "kind": "evidence", "value": value, **metadata}


def test_context_count_matches_original_unicode_json_and_exact_boundary():
    context = {"purpose": "审批", "catalog": [catalog_item("r", "")]}
    context["catalog"][0]["value"] = "中" * (DECISION_CONTEXT_CHARS - context_chars(context))
    assert context_chars(context) == len(json.dumps(context, ensure_ascii=False)) == DECISION_CONTEXT_CHARS
    assert decision_pages("select_context", context) == [context]
    context["catalog"][0]["value"] += "文"
    pages = decision_pages("select_context", context)
    assert len(pages) > 1 and all(context_chars(page) <= DECISION_CONTEXT_CHARS for page in pages)
    assert "".join(page["catalog"][0]["value"] for page in pages) == context["catalog"][0]["value"]


def test_complete_items_keep_order_and_all_shared_fields_without_mutation():
    context = {
        "purpose": {"section": "审批", "instruction": "保留否定和未决风险"},
        "user_request": "编制业务说明",
        "selection_reference_retry": "只返回外层引用",
        "catalog": [catalog_item(f"r{i}", {"text": "规则" * 250}) for i in range(19)],
    }
    original = deepcopy(context)
    pages = decision_pages("select_context", context, max_items=3, max_chars=3000)
    assert [item for page in pages for item in page["catalog"]] == context["catalog"]
    assert all(len(page["catalog"]) <= 3 and context_chars(page) <= 3000 for page in pages)
    assert all(
        {k: v for k, v in page.items() if k != "catalog"}
        == {k: v for k, v in context.items() if k != "catalog"}
        for page in pages
    )
    pages[0]["purpose"]["section"] = "changed"
    pages[0]["catalog"][0]["value"]["text"] = "changed"
    assert context == original and pages[1]["purpose"] == original["purpose"]


def test_pages_fill_item_capacity_in_source_order_instead_of_binary_halves():
    context = {
        "purpose": "审批条件",
        "catalog": [catalog_item(f"r{i}", "审批规则") for i in range(41)],
    }
    pages = decision_pages("select_context", context, max_items=8)
    assert [len(page["catalog"]) for page in pages] == [8, 8, 8, 8, 8, 1]
    assert [item for page in pages for item in page["catalog"]] == context["catalog"]


def test_packing_accounts_for_full_request_overhead_without_shrinking_other_pages():
    context = {
        "purpose": "审批条件",
        "catalog": [catalog_item("long", "长" * 1200)]
        + [catalog_item(f"r{i}", "规则" * 70) for i in range(12)],
    }

    def complete_input_chars(page):
        # Provider questions have per-item overhead as well as a shared core.
        return context_chars(page) + 250 + 100 * len(page["catalog"])

    pages = decision_pages(
        "select_context", context, max_items=8, max_chars=2000, measure_chars=complete_input_chars
    )
    assert all(complete_input_chars(page) <= 2000 for page in pages)
    assert [item for page in pages for item in page["catalog"]] == context["catalog"]
    assert len(pages[1]["catalog"]) > len(pages[0]["catalog"])
    for page, following in zip(pages, pages[1:], strict=False):
        assert (
            len(page["catalog"]) == 8
            or complete_input_chars(page | {"catalog": page["catalog"] + following["catalog"][:1]})
            > 2000
        )


def test_long_fragment_and_shared_core_count_provider_overhead_without_text_loss():
    value = {"evidence_id": "source", "text": "原文不得删节。" * 500}
    context = {"purpose": "审批", "catalog": [catalog_item("r", value)]}
    def measure(page):
        return context_chars(page) + 350

    pages = decision_pages("select_context", context, max_chars=1000, measure_chars=measure)
    assert all(measure(page) <= 1000 for page in pages)
    assert json.loads("".join(item["value"] for page in pages for item in page["catalog"])) == value
    with pytest.raises(DecisionBatchingError, match="shared_context_exceeds_capacity"):
        decision_pages("select_context", context, max_chars=1000, measure_chars=lambda _: 1001)


@pytest.mark.parametrize("value_kind", ["text", "object", "array"])
def test_long_value_fragments_restore_every_character_and_source_identity(value_kind):
    text = '未获批准，不得付款。\\路径\n"例外"：🧾e\u0301' * 600
    value = {
        "text": text,
        "source_id": "kb/审批办法",
        "evidence_id": "evidence_1",
        "metadata": {"confidential": True},
    }
    if value_kind == "text":
        value = text
    elif value_kind == "array":
        value = [value, {"exception": "总行审批后才可付款"}]
    item = catalog_item("outer_ref", value, source_id="source_outer", source_page=7)
    context = {"purpose": "判断是否与付款流程相关", "catalog": [item]}
    original = deepcopy(context)
    pages = decision_pages("select_context", context, max_items=8, max_chars=1200)
    assert len(pages) > 1
    fragments = [page["catalog"][0] for page in pages]
    assert [part["fragment"]["index"] for part in fragments] == list(range(len(pages)))
    assert all(part["fragment"]["partial"] for part in fragments)
    assert all(part["fragment"]["count"] == len(pages) for part in fragments)
    for part, page in zip(fragments, pages, strict=True):
        assert context_chars(page) <= 1200
        assert part["context_ref"] == "outer_ref" and part["kind"] == "evidence"
        assert part["source_id"] == "source_outer" and part["source_page"] == 7
        if value_kind == "object":
            assert part["fragment"]["source"] == {
                "source_id": "kb/审批办法",
                "evidence_id": "evidence_1",
            }
    restored = "".join(part["value"] for part in fragments)
    assert len(restored) == fragments[0]["fragment"]["original_chars"]
    assert (restored if value_kind == "text" else json.loads(restored)) == value
    assert context == original


def test_mixed_large_and_small_sources_do_not_merge_or_renumber_refs():
    context = {
        "purpose": "相关性",
        "catalog": [
            catalog_item("first", "小来源"),
            catalog_item("long", "很长的来源" * 1000),
            catalog_item("last", {"rule": "最后一条完整规则"}),
        ],
    }
    pages = decision_pages("select_context", context, max_chars=900)
    items = [item for page in pages for item in page["catalog"]]
    assert items[0] == context["catalog"][0] and items[-1] == context["catalog"][-1]
    assert {item["context_ref"] for item in items[1:-1]} == {"long"}
    assert "".join(item["value"] for item in items[1:-1]) == context["catalog"][1]["value"]


def test_shared_purpose_and_source_metadata_must_fit_without_truncation():
    context = {"purpose": "全局要求" * 1000, "catalog": [catalog_item("r", "内容")]}
    with pytest.raises(DecisionBatchingError, match="shared_context_exceeds_capacity"):
        decision_pages("select_context", context, max_chars=500)
    context = {"purpose": "相关性", "catalog": [catalog_item("r", "内容", source="来源" * 1000)]}
    with pytest.raises(DecisionBatchingError, match="item_metadata_exceeds_capacity") as error:
        decision_pages("select_context", context, max_chars=500)
    assert error.value.context_ref == "r"


def test_empty_catalog_and_unfragmented_result_are_independent_copies():
    context = {"purpose": {"instruction": "相关性"}, "catalog": []}
    assert decision_pages("select_context", context) == [context]
    page = decision_pages("select_context", context)[0]
    page["purpose"]["instruction"] = "changed"
    assert context["purpose"]["instruction"] == "相关性"


@pytest.mark.parametrize("max_chars", [0, -1, True, 1.5])
def test_character_limit_requires_positive_integer(max_chars):
    with pytest.raises(ValueError, match="max_chars"):
        decision_pages("select_context", {"catalog": []}, max_chars=max_chars)


def test_other_decisions_are_out_of_scope():
    with pytest.raises(DecisionBatchingError, match="unsupported_task"):
        decision_pages("next_action", {"business_record": {"facts": []}})
