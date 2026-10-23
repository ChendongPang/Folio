"""Public directory search progress comes from actual actions and safe records."""

import json

import pytest

from folio.research.planning_progress import (
    action_message,
    error_message,
    result_message,
    web_research_view,
)


def test_directory_web_progress_explains_query_and_outline_purpose():
    message = action_message("search.web", {
        "query": "ISO 27001 certification scope",
        "purpose": "核对信息安全章节需要解释的认证范围",
    }, {}, {}, "private reasoning should not be surfaced")
    assert "正在联网查找“ISO 27001 certification scope”" in message
    assert "目录用途：核对信息安全章节需要解释的认证范围" in message
    assert "private reasoning" not in message


@pytest.mark.parametrize("cached", [False, True])
def test_directory_web_result_counts_unique_sources_and_labels_cache(cached):
    message = result_message("search.web", {"query": "认证范围"}, {
        "status": "completed", "cached": cached,
        "evidence": [
            {"evidence_id": "W1", "source_id": "https://example.org/standard", "text": "摘录一"},
            {"evidence_id": "W2", "source_id": "https://example.org/standard", "text": "摘录二"},
            {"evidence_id": "W3", "source_id": "https://example.org/faq", "text": "摘录三"},
        ],
    }, {}, {})
    assert "2 个公开来源" in message
    assert "认证范围" in message
    assert ("已复用缓存" in message) is cached
    assert "摘录" not in message


def test_paged_web_progress_keeps_retrieved_source_count_and_explains_unread_tail():
    message = result_message("search.web", {"query": "公开标准"}, {
        "status": "completed", "source_count": 5, "web_result_page": True, "complete": False,
        "text": "Only a prefix is shown", "next_offset": 2000,
    }, {}, {})
    assert "5 个公开来源" in message
    assert "部分摘录已返回，其余可继续读取" in message
    assert "0 个公开来源" not in message


@pytest.mark.parametrize("status,expected", [
    ("failed", "未成功"), ("unavailable", "暂不可用"), ("interrupted", "已中断"),
])
def test_directory_web_failure_progress_never_echoes_provider_error(status, expected):
    raw = "Authorization: Bearer private-provider-token"
    message = result_message("search.web", {"query": "公开标准"}, {
        "status": status, "error": raw, "error_code": "search_failed",
    }, {}, {})
    assert expected in message and "公开标准" in message
    assert raw not in message
    assert raw not in error_message("search.web", {"query": "公开标准"}, {}, ValueError(raw))
    assert "private-provider-token" not in action_message("search.web", {"query": raw, "purpose": raw}, {}, {})


def test_directory_web_public_record_has_only_navigation_and_supported_findings():
    record = web_research_view({
        "available": True, "config": {"api_key": "secret-provider-key"},
        "searches": [
            {"query": "公开认证要求", "purpose": "确定目录的合规范围", "status": "completed", "cached": True,
             "evidence_ids": ["canonical_1", "bad_url"], "raw_response": "secret-provider-key"},
            {"query": "其他公开标准", "purpose": "查找补充说明", "status": "failed", "evidence_ids": [],
             "error_code": "search_failed", "error": "Authorization: Bearer secret-provider-key"},
        ],
        "evidence": [
            {"evidence_id": "canonical_1", "source_id": "https://example.org/standard", "text": "不发送浏览器的原文",
             "retrieved_at": "2026-10-21T09:00:00+00:00", "metadata": {"title": "认证说明", "api_key": "secret-provider-key"}},
            {"evidence_id": "bad_url", "source_id": "javascript:alert(1)", "text": "不可信链接"},
        ],
        "findings": [{"text": "目录中补充认证适用范围的说明。", "evidence_ids": ["canonical_1"]}],
    })
    assert record["searches"][0]["source_count"] == 1
    assert record["searches"][0]["evidence_ids"] == ["canonical_1"]
    assert record["searches"][1]["error_code"] == "search_failed"
    assert record["evidence"] == [{
        "evidence_id": "canonical_1", "url": "https://example.org/standard", "title": "认证说明",
        "retrieved_at": "2026-10-21T09:00:00+00:00",
    }]
    text = json.dumps(record, ensure_ascii=False)
    assert all(value not in text for value in ["secret-provider-key", "不发送浏览器的原文", "javascript", "raw_response"])
    assert record["findings"][0]["evidence_ids"] == ["canonical_1"]
