"""Freeze example style before planning; keep its calls in the job's durable budget."""

import json

from folio.runtime.base import RuntimeResult, prompt_chars
from folio.runtime.call_budget import CallBudgetPolicy, call_budget_scope


class JobStyleRuntime:
    def __init__(self, engine, maximum):
        self.engine = engine
        self.maximum = maximum
        self.config = getattr(engine.runtime, "config", {})

    def prompt_chars(self, task, context, schema):
        return prompt_chars(self.engine.runtime, task, context, schema)

    def request_parts(self, task, context, schema):
        builder = getattr(self.engine.runtime, "request_parts", None)
        if builder is not None:
            return builder(task, context, schema)
        from folio.document.style_profile import style_messages

        return style_messages(context, schema), schema, {}, None, {}

    async def measure(self, task, context, schema, char_limit):
        measure = getattr(self.engine.runtime, "measure", None)
        if measure is not None:
            return await measure(task, context, schema, char_limit)
        size = self.prompt_chars(task, context, schema)
        return {"characters": size, "fits": size <= char_limit}

    async def complete(self, task, context, schema, session_key):
        from folio.document.style_contracts import StyleAnalysisStep, StyleConsolidationStep

        if task != "style_analysis":
            raise ValueError("Style analysis cannot run another model task")
        policy = CallBudgetPolicy(
            bucket="style_analysis", max_calls=self.maximum,
            reserve_calls=min(20, self.engine.limits.max_calls // 4), purpose="research",
        )
        with call_budget_scope(policy):
            result = await self.engine.call(
                task, context,
                StyleConsolidationStep if context.get("style_consolidation") else StyleAnalysisStep,
            )
        return RuntimeResult(result.model_dump_json())


async def prepare_job_style(engine):
    from folio.document.samples import sample_profile
    from folio.document.style_layout import resolve_style_policy
    from folio.document.style_profile import prepare_style_profile, validate_profile_sources
    from folio.research.workflow import local

    paths = engine.request.sample_paths
    if not paths:
        return sample_profile([])
    output = engine.store.root / "jobs" / engine.job_id / "inputs" / "style"
    frozen = output / "profile.json"
    if frozen.is_file():
        profile = json.loads(await local(frozen.read_text, encoding="utf-8"))
        await local(validate_profile_sources, profile, paths)
        if "user_format_policy" not in profile:
            # A cancelled run may have persisted extraction immediately before
            # adopting this job's explicit formatting requirements.
            profile = resolve_style_policy(profile, engine.request.user_request)
            await local(save_style_profile, output, profile)
        return profile
    config = engine.request.style_analysis.model_copy(update={
        "timeout_seconds": min(engine.request.style_analysis.timeout_seconds, engine.limits.call_timeout_seconds),
    })
    profile = await prepare_style_profile(
        paths, output,
        runtime=JobStyleRuntime(engine, config.max_calls) if engine.request.runtime == "llm" else None,
        cache_root=engine.store.root / "source-cache", config=config,
        on_progress=lambda text: engine.store.work_event(engine.job_id, text),
        revision_view=engine.request.reference_revision_view,
        user_request=engine.request.user_request,
    )
    profile = resolve_style_policy(profile, engine.request.user_request)
    await local(save_style_profile, output, profile)
    return profile


def save_style_profile(output, profile):
    """The adopted Markdown and parameters derive from the same frozen profile."""
    output.mkdir(parents=True, exist_ok=True)
    for name, text in (
        ("profile.json", json.dumps(profile, ensure_ascii=False, indent=2) + "\n"),
        ("style.md", profile.get("markdown", profile.get("style_markdown", ""))),
    ):
        path = output / name
        pending = path.with_suffix(path.suffix + ".tmp")
        pending.write_text(text, encoding="utf-8")
        pending.replace(path)
