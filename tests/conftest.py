import pytest

from folio.models import JobInput
from folio.store import Store


@pytest.fixture
def inputs(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "rules.md").write_text(
        "外汇交易由交易员录入。交易撤销需要主管审批。", encoding="utf-8"
    )
    return dict(
        title="测试需求说明书",
        user_request="# 外汇交易\n描述交易要求。\n## 交易撤销\n描述撤销审批规则。\n",
        knowledge_path=str(knowledge),
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def demo_job(store, inputs):
    return store.create(JobInput(**inputs, runtime="demo"))
