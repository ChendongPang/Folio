"""The CLI requests an explicit durable retry before starting saved work."""

import json
import sys

import pytest

from folio.cli import main


def test_cli_resume_authorizes_failed_task_before_execution(store, demo_job, monkeypatch, capsys):
    store.begin_task(demo_job, "research_step:failed", 100)
    store.finish_task(demo_job, "research_step:failed", error="Model HTTP 502")
    store.update(demo_job, status="failed", stage="generation", error="Model HTTP 502")

    async def execute(actual_store, actual_job_id, config):
        assert actual_job_id == demo_job and config is None
        assert actual_store.job_status(demo_job) == "queued"
        assert actual_store.task_retry_baseline(demo_job, "research_step:failed") == 1
        return actual_store.get(demo_job)

    monkeypatch.setattr("folio.cli.execute", execute)
    monkeypatch.setattr(
        sys, "argv", ["folio", "--data-dir", str(store.root), "resume", demo_job],
    )
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "queued" and result["calls"] == 1


@pytest.mark.parametrize("status", ["published", "draft", "needs_confirmation", "pausing"])
def test_cli_resume_does_not_execute_nonresumable_job(
    store, demo_job, monkeypatch, capsys, status,
):
    store.update(demo_job, status=status)

    async def unexpected(*args):
        pytest.fail("Non-resumable jobs must not start execution")

    monkeypatch.setattr("folio.cli.execute", unexpected)
    monkeypatch.setattr(
        sys, "argv", ["folio", "--data-dir", str(store.root), "resume", demo_job],
    )
    with pytest.raises(SystemExit) as failed:
        main()
    assert failed.value.code == 1
    assert capsys.readouterr().out == ""
    assert store.job_status(demo_job) == status
