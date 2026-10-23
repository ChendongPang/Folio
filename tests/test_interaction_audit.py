import asyncio
import json

import pytest

from folio.runtime.interaction_audit import interaction_scope, provider_attempt
from folio.store import Store


async def test_concurrent_provider_retries_keep_parent_and_do_not_leak_payload(tmp_path):
    store = Store(tmp_path)

    async def operation(key):
        with interaction_scope(store, "job", "select_context", key, 2):
            try:
                with provider_attempt("decision", "evaluate", model="jev") as data:
                    data.update(status_code=503, payload="secret", headers="Bearer secret")
                    await asyncio.sleep(0)
                    raise ValueError("provider body secret")
            except ValueError:
                pass
            with provider_attempt("decision", "evaluate", model="jev") as data:
                data["status_code"] = 200

    await asyncio.gather(operation("a"), operation("b"))
    events = store.events("job")
    completed = [e["data"] for e in events if e["kind"] == "provider_completed"]
    assert len(completed) == 4
    assert len({e["interaction_id"] for e in completed}) == 4
    for key in ("a", "b"):
        pair = [e for e in completed if e["task_key"] == key]
        assert [e["outcome"] for e in pair] == ["failed", "success"]
        assert all(e["attempt"] == 2 for e in pair)
    assert "secret" not in json.dumps(events)


async def test_cancelled_provider_is_not_success_and_no_scope_is_noop(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        with interaction_scope(store, "job", "generate", "generate:a"):
            with provider_attempt("llm", "chat.completions"):
                raise asyncio.CancelledError()
    with provider_attempt("llm", "chat.completions"):
        pass
    events = store.events("job")
    assert len(events) == 2
    assert events[-1]["data"]["outcome"] == "cancelled"
