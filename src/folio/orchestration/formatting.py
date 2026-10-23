"""Optional, bounded formatting reviews scoped to existing business task groups."""

import asyncio
import json
from pathlib import Path

from docx import Document

from folio.document.formatting import (
    FORMAT_POLICY,
    FormattingResult,
    apply_patches,
    baseline_snapshot,
    collect_targets,
    protected_signature,
    replay_formatting,
    save_document,
    target_view,
)

MAX_GROUP_CALLS = 3
MAX_PARALLEL_GROUPS = 4
GROUP_SECONDS = 25
STAGE_SECONDS = 120
PAGE_CHARACTERS = 10000
PAGE_TARGETS = 24


def _write_report(path, report):
    temporary = path.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _pages(document, targets):
    pages, current, size, oversized = [], [], 0, []
    for target in targets:
        length = len(json.dumps(target_view(document, target), ensure_ascii=False))
        if length > PAGE_CHARACTERS:
            oversized.append(target.target_id)
            continue
        if current and (len(current) >= PAGE_TARGETS or size + length > PAGE_CHARACTERS):
            pages.append(current)
            current, size = [], 0
        current.append(target)
        size += length
    if current:
        pages.append(current)
    return pages, oversized


async def format_document(
    path,
    plan,
    states,
    work_plan,
    folder,
    call,
    progress,
    measure=None,
    *,
    enabled=True,
    disabled_reason="offline_demo",
    group_seconds=GROUP_SECONDS,
    stage_seconds=STAGE_SECONDS,
):
    """Never prevent document export; durable requests use the engine's ordinary call cache."""
    path, folder = Path(path), Path(folder)
    manifest = folder / "formatting.json"
    report = {"policy": FORMAT_POLICY, "status": "skipped", "changed": False, "groups": []}
    if not enabled:
        report["reason"] = disabled_reason
        _write_report(manifest, report)
        return report
    document = None
    targets = []
    invariant_failed = False
    try:
        document = Document(path)
        targets = collect_targets(document, plan, states)
        signature = protected_signature(document, targets)
        report["source_signature"] = signature
        report["target_baselines"] = baseline_snapshot(targets)
        if manifest.is_file():
            previous = json.loads(manifest.read_text(encoding="utf-8"))
            if previous.get("policy") == FORMAT_POLICY:
                replay = replay_formatting(path, plan, states, manifest)
                if replay["status"] == "replayed":
                    return previous | {"status": "replayed", "changed": replay["changed"]}
        report["status"] = "complete"
        definitions = {section.section_id: section for section in plan.sections}
        report["groups"] = [
            {"task_id": task.task_id, "status": "not_started", "iterations": [], "calls": 0}
            for task in work_plan.tasks
        ]
        slots = asyncio.Semaphore(MAX_PARALLEL_GROUPS)
        workers = []
        _write_report(manifest, report)

        async def group(task, result):
            nonlocal invariant_failed
            owned = [target for target in targets if target.section_id in task.section_ids]
            result["status"] = "complete"
            if len(owned) < 2:
                result["status"] = "unchanged"
                return
            pages, oversized = _pages(document, owned)
            result["oversized_targets"] = oversized
            calls = 0
            try:
                async with asyncio.timeout(group_seconds):
                    while pages and calls < MAX_GROUP_CALLS:
                        page = pages[0]
                        context = {
                            "formatting_policy": FORMAT_POLICY,
                            "work_task": {"task_id": task.task_id, "title": task.title},
                            "sections": [
                                {"section_id": sid, "title": definitions[sid].title}
                                for sid in task.section_ids
                                if sid in definitions
                            ],
                            "iteration": calls + 1,
                            "max_iterations": MAX_GROUP_CALLS,
                            "remaining_pages": len(pages),
                            "targets": [target_view(document, target) for target in page],
                            "evaluation": 'Actual DOCX paragraph and table properties; no page screenshots or layout-engine coordinates are available.',
                        }
                        if (
                            measure
                            and not (await measure("format_document", context, FormattingResult))[
                                "fits"
                            ]
                        ):
                            if len(page) > 1:
                                middle = len(page) // 2
                                pages[:1] = [page[:middle], page[middle:]]
                                continue
                            oversized.append(page[0].target_id)
                            pages.pop(0)
                            continue
                        if invariant_failed:
                            result["status"] = "cancelled"
                            break
                        calls += 1
                        progress(
                            f"正在优化“{task.title}”的排版（第 {calls}/{MAX_GROUP_CALLS} 次），正文内容保持不变。"
                        )
                        proposal = await call("format_document", context, FormattingResult)
                        if invariant_failed:
                            result["status"] = "cancelled"
                            break
                        if not isinstance(proposal, FormattingResult):
                            proposal = FormattingResult.model_validate(proposal)
                        if not proposal.patches:
                            result["status"] = (
                                "unchanged" if not result["iterations"] else "complete"
                            )
                            pages.pop(0)
                            continue
                        try:
                            changed = apply_patches(document, page, proposal.patches)
                        except ValueError as exc:
                            if "protected" in str(exc):
                                invariant_failed = True
                                for worker in workers:
                                    if (
                                        worker is not asyncio.current_task()
                                        and not worker.done()
                                        and not worker.cancelling()
                                    ):
                                        worker.cancel()
                            raise
                        if not changed:
                            result["status"] = (
                                "unchanged" if not result["iterations"] else "complete"
                            )
                            pages.pop(0)
                            continue
                        result["status"] = "complete"
                        result["iterations"].append(
                            {
                                "iteration": calls,
                                "target_ids": [target.target_id for target in page],
                                "applied_patches": [
                                    patch.model_dump(exclude_none=True)
                                    for patch in proposal.patches
                                ],
                                "summary": proposal.summary,
                                "done": proposal.done,
                            }
                        )
                        report["changed"] = True
                        _write_report(manifest, report)
                        if proposal.done:
                            pages.pop(0)
                    if calls >= MAX_GROUP_CALLS and pages:
                        result["status"] = "iteration_limit"
            except TimeoutError:
                result["status"] = "timeout"
                progress(f"“{task.title}”的排版优化已到时间上限，保留已完成的安全调整。")
            except Exception as exc:
                result.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:400])
                progress(
                    f"“{task.title}”的排版优化未完成，继续输出文档；原因：{type(exc).__name__}。"
                )
            finally:
                result["calls"] = calls
                result["remaining_targets"] = sum(len(page) for page in pages)
                if (result["remaining_targets"] or oversized) and result["status"] in {
                    "complete",
                    "unchanged",
                }:
                    result["status"] = "partial"
                _write_report(manifest, report)

        async def bounded_group(task, result):
            try:
                # Waiting for another group must not consume this group's deadline.
                async with slots:
                    if invariant_failed:
                        result["status"] = "cancelled"
                        return
                    await group(task, result)
            except asyncio.CancelledError:
                if result["status"] != "not_started":
                    result["status"] = "cancelled"
                raise
            except Exception as exc:
                result.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:400])
            finally:
                # Document edits and report writes stay synchronous on this event loop.
                _write_report(manifest, report)

        workers = [
            asyncio.create_task(bounded_group(task, result))
            for task, result in zip(work_plan.tasks, report["groups"], strict=True)
        ]
        try:
            async with asyncio.timeout(stage_seconds):
                await asyncio.gather(*workers, return_exceptions=True)
        except TimeoutError:
            report["status"] = "timeout"
            progress("排版优化已到总时间上限，保留已完成的安全调整并继续输出文档。")
        except asyncio.CancelledError:
            report["status"] = "cancelled"
            raise
        finally:
            for worker in workers:
                if not worker.done() and not worker.cancelling():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            _write_report(manifest, report)
        if invariant_failed or protected_signature(document, targets) != signature:
            report.update(
                status="failed",
                changed=False,
                error="Protected content check failed; formatting discarded",
            )
        elif report["changed"]:
            save_document(document, path)
        if report["status"] == "complete" and any(
            g["status"]
            in {"failed", "timeout", "iteration_limit", "partial", "cancelled", "not_started"}
            for g in report["groups"]
        ):
            report["status"] = "partial"
    except Exception as exc:
        report.update(
            status="failed", changed=False, error_type=type(exc).__name__, error=str(exc)[:400]
        )
        progress(f"排版优化不可用，继续输出已生成文档；原因：{type(exc).__name__}。")
    _write_report(manifest, report)
    return report
