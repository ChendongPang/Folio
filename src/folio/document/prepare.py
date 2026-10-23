"""Plan a directory from writing requirements and materials, then render its preview."""

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from folio.document.directory_revision import (
    DirectoryRevision,
    apply_directory_revision,
    directory_index,
)
from folio.document.guidance import WritingGuide, guidance_context
from folio.document.plan_repair import DirectoryValidationError, PlanRepair, apply_plan_repair
from folio.document.planner import build_plan, planning_context
from folio.document.research_context import (
    directory_confirmation_issues,
    planning_research_context,
)
from folio.document.samples import sample_profile
from folio.document.template_contract import TemplateContract
from folio.document.word import render
from folio.models import (
    Block,
    DocumentPlan,
    Extraction,
    Limits,
    PlanProposal,
    SectionContent,
    SectionState,
    digest,
)
from folio.runtime.errors import (
    ModelCallBudgetExceeded,
    ModelCallFailed,
    PlanningContextTooLarge,
)

# Bump when the generated directory's presentation changes. Saved plans remain authoritative.
DIRECTORY_PREVIEW_VERSION = 5


class DirectoryRuntimeBudget:
    """Share the preview's logical call allowance across research, plan and review."""

    def __init__(self, runtime, maximum):
        self.runtime, self.maximum, self.used = runtime, maximum, 0

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    async def complete(self, *args, **kwargs):
        if self.used >= self.maximum:
            raise ModelCallBudgetExceeded(self.used, self.maximum)
        self.used += 1
        return await self.runtime.complete(*args, **kwargs)


async def _planning_call(runtime, task, context, schema, session_key, timeout):
    """Enforce the configured deadline without losing transport cancellation diagnostics."""
    started = monotonic()
    try:
        return await asyncio.wait_for(
            runtime.complete(task, context, schema, session_key), timeout=timeout
        )
    except TimeoutError as exc:
        details = {}
        cause = exc
        # wait_for's TimeoutError chains the original cancellation. The runtime
        # keeps phase/counts on that exception, so simultaneous calls cannot mix them.
        for _ in range(4):
            if cause is None:
                break
            if isinstance(getattr(cause, "diagnostics", None), dict):
                details = dict(cause.diagnostics)
                break
            cause = cause.__cause__
        failure = ModelCallFailed("Directory planning call timed out", "timeout")
        failure.diagnostics = {
            "elapsed_seconds": round(monotonic() - started, 2),
            **details,
            "timeout_seconds": timeout,
            "deadline_source": "directory_call",
        }
        raise failure from exc


async def prepare_with_model(
    requirements: str,
    source_catalog: list[dict],
    output: Path,
    *,
    runtime,
    title: str | None = None,
    revision_view="final",
    samples=None,
    limits=None,
    context_extra=None,
    document_id=None,
    on_progress=None,
    writing_guide: WritingGuide | None = None,
    cover: dict | None = None,
    template: TemplateContract | None = None,
    source_dir: Path | None = None,
    research_cache_root: Path | None = None,
    style_config=None,
    research_replay: Path | None = None,
):
    """Research sources, plan the whole directory, and check coverage with bounded retries.

    ``research_replay`` continues an interrupted directory research run from its folder.
    """
    from pydantic import ValidationError

    from folio.runtime.base import parse_json, prompt_chars

    operation_started = monotonic()
    operation_started_at = datetime.now(UTC).isoformat()
    limits = limits or Limits()
    runtime = DirectoryRuntimeBudget(runtime, limits.max_calls)
    from folio.runtime.language import localize_text, normalize_language, output_language

    ui_language = normalize_language((context_extra or {}).get("ui_language"))
    language = output_language(requirements, default=ui_language)

    def progress(text):
        if on_progress:
            on_progress(text)

    writing_profile = None
    if samples:
        from folio.document.style_layout import resolve_style_policy
        from folio.document.style_profile import (
            StyleAnalysisConfig,
            prepare_style_profile,
            profile_for_stage,
        )
        from folio.orchestration.style_processing import save_style_profile
        from folio.research.workflow import local

        style_config = StyleAnalysisConfig.model_validate(style_config or {})
        style_config = style_config.model_copy(update={
            "max_calls": min(style_config.max_calls, max(0, limits.max_calls - 1)),
            "timeout_seconds": min(style_config.timeout_seconds, limits.call_timeout_seconds),
        })
        if not getattr(runtime, "measure", None):
            style_config = style_config.model_copy(update={
                "max_prompt_chars": min(style_config.max_prompt_chars, limits.max_prompt_chars),
            })
        writing_profile = await prepare_style_profile(
            samples, output / "style", runtime=runtime, cache_root=research_cache_root,
            config=style_config, on_progress=on_progress, revision_view=revision_view,
            user_request=requirements,
        )
        writing_profile = resolve_style_policy(writing_profile, requirements)
        await local(save_style_profile, output / "style", writing_profile)
    progress("正在准备资料目录与写作要求。")
    research = None
    research_review = None
    if source_dir is not None:
        from folio.research.planning import research_for_directory

        research = await research_for_directory(
            runtime, Path(source_dir), output / "research", requirements,
            cache_root=research_cache_root,
            template=template.planning_context() if template else None,
            on_progress=on_progress, limits=limits, replay=research_replay,
        )
    context = planning_context(requirements, source_catalog)
    context.update(context_extra or {})
    context["document_language"] = language
    if writing_profile:
        context["writing_profile"] = profile_for_stage(writing_profile, "planning")
    if research is not None:
        context["directory_research"] = planning_research_context(research)
    if template is not None:
        context["template"] = template.planning_context()
    previous = (
        PlanProposal.model_validate(context["previous_proposal"])
        if context.get("previous_proposal")
        else None
    )
    if previous:
        context["previous_directory"] = directory_index(previous)
    context = guidance_context("revise_plan" if previous else "plan", context, writing_guide)
    original = context
    schema = PlanProposal.model_json_schema()
    document_id = document_id or "preview_" + digest(str(output.resolve()))
    output.mkdir(parents=True, exist_ok=True)
    (output / "writing-guide.json").write_text(
        writing_guide.model_dump_json(indent=2) if writing_guide else "null",
        encoding="utf-8",
    )
    (output / "planning-input.json").write_text(
        json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    attempts = []
    base_attempts = min(limits.call_attempts, limits.max_calls)
    # One extra sparse repair is allowed ONLY when the preceding repair measurably
    # reduced validation errors. Timeouts/repeated invalid proposals get no extra try.
    max_attempts = min(base_attempts + 1, limits.max_calls)
    for attempt in range(max_attempts):
        # Sparse edits need a schema-valid proposal and concrete structural paths.
        # Malformed/extra fields cannot be repaired by the typed field-edit contract.
        repair = (
            not previous
            and attempt > 0
            and context.get("rejected_proposal_schema_valid") is True
            and bool(context.get("repair_paths"))
        )
        task = "revise_plan" if previous else "repair_plan" if repair else "plan"
        # Measure and send exactly the same action-specific guidance. It remains
        # advisory context and never becomes a directory validation requirement.
        context = guidance_context(task, context, writing_guide)
        call_schema = (
            DirectoryRevision.model_json_schema()
            if previous
            else PlanRepair.model_json_schema()
            if repair
            else schema
        )
        if repair and context.get("repair_paths"):
            call_schema["$defs"]["PlanEdit"]["properties"]["path"]["enum"] = context["repair_paths"]
        record = {"attempt": attempt + 1, "task": task}
        started = monotonic()
        progress(
            f"正在按目录编号理解修改建议（第 {attempt + 1} 轮），仅返回需要执行的编辑。"
            if previous
            else f"正在等待模型修正出错字段（第 {attempt + 1} 轮），保留其余目录。"
            if repair
            else f"正在等待模型返回完整目录（第 {attempt + 1} 轮），尚未生成正文。"
        )
        try:
            record["prompt_chars"] = prompt_chars(runtime, task, context, call_schema)
            measure = getattr(runtime, "measure", None)
            if measure is not None:
                record["capacity"] = await asyncio.wait_for(
                    measure(task, context, call_schema, limits.max_planning_prompt_chars),
                    timeout=limits.call_timeout_seconds,
                )
                fits = record["capacity"]["fits"]
            else:
                fits = record["prompt_chars"] <= limits.max_planning_prompt_chars
            if not fits:
                raise PlanningContextTooLarge(
                    record["prompt_chars"], limits.max_planning_prompt_chars,
                    capacity=record.get("capacity"),
                )
            result = await _planning_call(
                runtime,
                task,
                context,
                call_schema,
                f"{document_id}:plan:{attempt + 1}",
                limits.call_timeout_seconds,
            )
            if result.stop_reason != "completed":
                raise ModelCallFailed(
                    "Directory planning response incomplete",
                    "length_limit"
                    if result.stop_reason in {"length", "length_limit", "max_tokens"}
                    else "protocol",
                )
            record["response"] = result.content
            record["usage"] = result.usage
            record["model_seconds"] = round(monotonic() - started, 2)
            progress("模型已返回目录，正在核对标题层级、节点用途和重复章节。")
            candidate = parse_json(result.content)
            if previous:
                record["revision"] = candidate
                candidate = apply_directory_revision(
                    previous, DirectoryRevision.model_validate(candidate)
                ).model_dump()
            elif repair:
                candidate = apply_plan_repair(
                    context["rejected_proposal"],
                    PlanRepair.model_validate(candidate),
                    context.get("repair_paths"),
                )
            record["proposal"] = candidate
            proposal = PlanProposal.model_validate(candidate)
            record["proposal_schema_valid"] = True
            # Materialize schema defaults so subsequent sparse repairs have real paths.
            record["proposal"] = proposal.model_dump()
            plan = build_plan(document_id, proposal, template=template)
            explicit_title = (
                (context_extra or {}).get("document_title_requirement")
                or (cover or {}).get("document_title")
            )
            effective_title = (
                explicit_title
                or plan.document_title
                or title
                or (localize_text(writing_guide.name, language) if writing_guide else None)
                or ("Untitled document" if language == "en" else "文档")
            )
            plan.document_title = effective_title
            title = effective_title
            record["validated"] = True
            if research is not None:
                from folio.document.research_review import review_directory_research

                progress("正在对照已读取的资料要求，检查目录是否有明确的写作安排。")
                research_review = await review_directory_research(
                    runtime, proposal, research, requirements=requirements,
                    limits=limits, session_key=f"{document_id}:directory-review:{attempt + 1}",
                    output=output / f"research-review-attempt-{attempt + 1}.json",
                )
                record["research_review"] = research_review
                (output / "research-review.json").write_text(
                    json.dumps(research_review, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                if research_review.get("retryable") and attempt + 1 < base_attempts:
                    raise DirectoryValidationError(
                        "These researched requirements are not yet assigned in the directory. "
                        "Add the relevant sections or writing instructions based on the originals:\n"
                        + "\n".join(research_review["issues"]),
                        [],
                    )
            break
        except Exception as exc:
            received = getattr(exc, "received_planning_response", None)
            if isinstance(received, dict) and isinstance(received.get("content"), str):
                record["response"] = received["content"]
                record["usage"] = received.get("usage", {})
            record["error"] = type(exc).__name__ + ": " + str(exc)[:3000]
            record["error_code"] = getattr(exc, "code", None)
            if getattr(exc, "diagnostics", None):
                record["diagnostics"] = exc.diagnostics
            issues = len(str(exc).splitlines()) if isinstance(exc, DirectoryValidationError) else None
            progress_repair = (
                task == "repair_plan"
                and issues is not None
                and issues < context.get("validation_issue_count", 0)
                and record.get("proposal") != context.get("rejected_proposal")
            )
            if (
                isinstance(exc, PlanningContextTooLarge)
                or getattr(exc, "code", "") == "context_limit"
                or attempt + 1 >= max_attempts
                or (attempt + 1 >= base_attempts and not progress_repair)
            ):
                raise
            if getattr(exc, "code", "") == "length_limit":
                # A partial JSON response is never accepted. Keep full source material,
                # but change the next request instead of repeating the exhausted one.
                context = context | {
                    "concise_plan_retry": True,
                    "format_feedback": "The previous output budget was exhausted before completing JSON. Return a complete result. "
                    "Use short phrases in explanatory fields, omit empty values and unnecessary notes, and avoid repeated explanations. "
                    "Retain all necessary business nodes and writing scope; do not write body text.",
                }
                adjust = getattr(runtime, "planning_retry_budget", None)
                if adjust:
                    record["retry_budget"] = await adjust(
                        task, context, call_schema, limits.max_planning_prompt_chars
                    )
                progress(
                    f"本轮目录输出达到长度上限，准备第 {attempt + 2} 轮："
                    "缩短写作说明、保留完整材料和目录，并检查可用输出空间。"
                )
                continue
            # Only a received model result can have correctable JSON/schema/structure
            # errors. Network, timeout, auth and unexpected runtime failures must not
            # become an identical full-plan request with empty validation feedback.
            if "response" not in record or not isinstance(exc, ValueError):
                raise
            feedback = (
                json.dumps(
                    exc.errors(include_input=False, include_context=False), ensure_ascii=False
                )
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            if not isinstance(exc, DirectoryValidationError):
                feedback = feedback[:3000]
            rejected_context = context
            context = original | {"format_feedback": "Correct these validation errors: " + feedback}
            if issues is not None:
                context["validation_issue_count"] = issues
            if previous:
                context["rejected_revision"] = record.get("revision", {})
            elif getattr(exc, "repair_paths", None):
                context["repair_paths"] = exc.repair_paths
            if not previous and repair and not record.get("proposal_schema_valid"):
                # A bad patch must not replace the last schema-valid directory.
                context["rejected_proposal"] = rejected_context["rejected_proposal"]
                context["rejected_proposal_schema_valid"] = True
                context["repair_paths"] = rejected_context["repair_paths"]
                context["validation_issue_count"] = rejected_context.get("validation_issue_count", 0)
                context["rejected_repair"] = record["response"]
            elif not previous and record.get("proposal"):
                context["rejected_proposal"] = record["proposal"]
                context["rejected_proposal_schema_valid"] = bool(record.get("proposal_schema_valid"))
            elif not previous and record.get("response"):
                # Keep malformed output as feedback, without treating it as an editable tree.
                try:
                    context["rejected_proposal"] = parse_json(record["response"])
                except ValueError:
                    context["rejected_proposal"] = record["response"]
            progress(f"本轮目录未通过检查，准备第 {attempt + 2} 轮修正。")
        finally:
            record["elapsed_seconds"] = round(monotonic() - started, 2)
            attempts.append(record)
            (output / "planning-attempts.json").write_text(
                json.dumps(attempts, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    output.mkdir(parents=True, exist_ok=True)
    (output / "planning-input.json").write_text(
        json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "planning-proposal.json").write_text(
        proposal.model_dump_json(indent=2), encoding="utf-8"
    )
    confirmable = research is None or (
        research["status"] in {"ready", "no_sources"}
        and research_review is not None and research_review["status"] == "passed"
    )
    confirmation_issues = directory_confirmation_issues(research, research_review)
    if not confirmable:
        plan.notes = list(dict.fromkeys([
            *plan.notes,
            *(item["message"] for item in confirmation_issues),
            *(research or {}).get("blocking_gaps", []),
            *(research_review or {}).get("issues", []),
        ]))[:30]
    progress(
        f"目录校验通过，共 {len(plan.sections)} 个节点，正在排版 Word 预览。"
        if confirmable else f"已整理 {len(plan.sections)} 个目录节点，仍有待解决事项，正在排版初稿预览。"
    )
    result = await asyncio.to_thread(
        prepare,
        output,
        plan=plan,
        title=title or plan.document_title or ("Untitled document" if language == "en" else "文档"),
        samples=samples,
        writing_profile=writing_profile,
        revision_view=revision_view,
        cover=cover,
        language=language,
    )
    result["task_usage"] = [_directory_usage(
        attempts=attempts,
        research=research,
        research_review=research_review,
        started_at=operation_started_at,
        started=operation_started,
        language=ui_language,
    )]
    if writing_profile:
        result["style"] = {
            "status": writing_profile.get("status"),
            "source_count": len(writing_profile.get("documents", [])),
            "policy": writing_profile.get("user_format_policy", {}),
        }
    if research is not None:
        visible_research = {key: value for key, value in research.items() if key in {
            "status", "summary", "unresolved", "blocking_gaps", "files", "calls", "rounds",
            "source_snapshot_id", "validation_issues", "termination_reason", "failure",
            "conclusion_submitted", "conclusion_validated",
        }}
        if isinstance(research.get("web_research"), dict):
            from folio.research.planning_progress import web_research_view

            visible_research["web_research"] = web_research_view(research["web_research"])
        visible_research["unresolved"] = list(dict.fromkeys([
            *research.get("blocking_gaps", []),
            *(research_review or {}).get("issues", []),
            *research.get("unresolved", []),
        ]))
        visible_research["files"] = [
            {key: value for key, value in file.items() if key in {
                "path", "name", "source_version", "inspected", "structure_complete",
                "read", "characters", "readings", "warnings", "source_complete",
            }} for file in research.get("files", [])
        ]
        for file in visible_research["files"]:
            name = re.sub(r"^[0-9a-f]{32}__", "", Path(file["path"]).name)
            file["name"] = name
            visible_research["unresolved"] = [
                issue.replace(file["path"], name) for issue in visible_research["unresolved"]
            ]
        if not confirmable:
            visible_research["status"] = "partial"
        result.update(
            confirmable=confirmable,
            confirmation_issues=confirmation_issues,
            research=visible_research,
            research_review={key: value for key, value in (research_review or {}).items() if key != "calls"},
        )
    return result


def _directory_usage(*, attempts, research, research_review, started_at, started, language):
    """Aggregate every model call in one directory planning operation."""
    records = list(attempts or [])
    records.extend(
        item for item in (research or {}).get("usage", []) if isinstance(item, dict)
    )
    records.extend(
        item for item in (research_review or {}).get("calls", []) if isinstance(item, dict)
    )

    def count(usage, *keys):
        for key in keys:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                return value
        return 0

    input_tokens = output_tokens = total_tokens = 0
    model_calls = failed_calls = cache_hits = 0
    for record in records:
        usage = record.get("usage")
        if not isinstance(usage, dict):
            continue
        prompt = count(usage, "prompt_tokens", "input_tokens")
        completion = count(usage, "completion_tokens", "output_tokens")
        input_tokens += prompt
        output_tokens += completion
        total_tokens += count(usage, "total_tokens") or prompt + completion
        model_calls += 1
        failed_calls += record.get("status") == "failed"
        cache_hits += record.get("cached") is True
    return {
        "task_id": "directory_planning",
        "title": "Outline planning" if language == "en" else "目录规划",
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "elapsed_ms": max(0, round((monotonic() - started) * 1000)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "model_calls": model_calls,
        "cache_hits": cache_hits,
        "failed_calls": failed_calls,
    }


def prepare(
    output: Path,
    *,
    plan: DocumentPlan,
    title: str | None = None,
    samples: list[Path] | None = None,
    revision_view="final",
    cover=None,
    defer_pagination: bool = False,
    writing_profile: dict | None = None,
    language: str = "zh-CN",
) -> dict:
    """Render a validated directory using the built-in document presentation."""
    from folio.runtime.language import normalize_language

    language = normalize_language(language)
    english = language == "en"
    title = plan.document_title or title or ("Requirements document" if english else "需求说明书")
    writing_profile = writing_profile if writing_profile is not None else sample_profile(samples or [], revision_view)
    states = []
    for section in plan.sections:
        content = SectionContent()
        if section.content_mode != "container":
            content.blocks.append(
                Block(
                    block_id="layout_text",
                    text=(
                        "Body not generated yet. After you confirm the outline, Folio will "
                        "research the uploaded sources and generate this section."
                        if english else
                        "尚未生成正文。确认目录后，将研究上传资料并生成本节内容。"
                    ),
                )
            )
        states.append(
            SectionState(
                section_id=section.section_id,
                content=content,
                evidence=[],
                extraction=Extraction(
                    summary="Outline layout only; no business body yet"
                    if english else "仅预览章节结构，尚无业务正文"
                ),
            )
        )
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / ("Document-outline-preview.docx" if english else "文档-目录预览.docx")
    values = {"document_title": title} | (cover or {})
    layout = render(
        artifact,
        plan,
        states,
        title=title,
        draft=True,
        preview=True,
        cover=cover,
        comments=[],
        defer_pagination=defer_pagination,
        writing_profile=writing_profile,
        language=language,
    )
    directory = [
        {
            "number": s.display_number,
            "section_id": s.section_id,
            "path": s.heading_path,
            "mode": s.content_mode,
            "evidence_count": 0,
            "pending_items": state.content.pending_items,
        }
        for s, state in zip(plan.sections, states, strict=True)
    ]
    report = {
        "status": "layout_preview",
        "title": title,
        "layout": layout,
        "artifact": str(artifact.resolve()),
        "plan_nodes": len(plan.sections),
        "content_sections": sum(s.content_mode == "generate" for s in plan.sections),
        "cover": values,
        "writing_profile": writing_profile,
        "directory": directory,
        "unresolved_comments": 0,
        "missing_sections": [node["path"] for node in directory if node["pending_items"]],
        "notes": (
            [
                "The outline is planned from the writing requirements and sources; body content will be based on later source research.",
                "This file previews the outline only; structural checks do not establish business-content completeness.",
                "Writing examples guide expression only and are not business evidence.",
                "Outline bookmarks and page numbers were calculated."
                if layout["status"] == "paginated"
                else "Outline bookmarks were created; update fields in Word/WPS to calculate page numbers.",
            ]
            if english else [
                "目录由模型根据写作要求与资料规划，正文依据来自后续资料研究",
                "本次仅预览目录，尚未生成业务正文；结构校验不代表业务内容完整性审查",
                "写作示例只指导表达，不作为业务事实依据",
                "目录书签和页码已计算"
                if layout["status"] == "paginated"
                else "目录书签已建立；页码需要 Word/WPS 更新域后计算",
            ]
        ),
    }
    (output / "document-plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    (output / "inspection.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "trace.json").write_text(
        json.dumps([s.model_dump() for s in states], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = (
        [
            "# Outline structure check",
            "",
            "This preview shows the outline only; body content has not been generated.",
            "",
            f"- Outline nodes: {len(plan.sections)}",
            f"- Content sections to generate: {report['content_sections']}; the rest are outline containers",
            "",
            "| Number | Section path | Mode |",
            "|---|---|---|",
        ]
        if english else [
            "# 目录结构检查",
            "",
            "此预览仅展示目录，正文尚未生成。",
            "",
            f"- 目录节点：{len(plan.sections)} 个",
            f"- 待生成内容章节：{report['content_sections']} 个；其余为目录容器",
            "",
            "| 编号 | 章节路径 | 用途 |",
            "|---|---|---|",
        ]
    )
    for node in directory:
        lines.append(f"| {node['number']} | {' / '.join(node['path'])} | {node['mode']} |")
    lines.extend(["", *report["notes"]])
    report_name = "inspection-report.md" if english else "检查报告.md"
    (output / report_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
