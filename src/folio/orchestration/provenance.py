"""Resolve unambiguous owner-local fact aliases back to original evidence IDs."""

from collections import defaultdict

from folio.models import SectionContent


def normalize_fact_references(content: SectionContent, evidence, record: dict) -> SectionContent:
    available = {item.evidence_id for item in evidence}
    facts = defaultdict(list)
    for fact in record.get("facts", []):
        facts[fact["fact_id"]].append(fact)
    aliases = {}
    for fact_id, matches in facts.items():
        if fact_id in available or len(matches) != 1:
            continue
        sources = matches[0].get("evidence_ids", [])
        if sources and all(source in available for source in sources):
            aliases[fact_id] = sources

    changed = False
    blocks = []
    for block in content.blocks:
        if block.origin == "model_proposal" or not any(
            reference in aliases for reference in block.evidence_ids
        ):
            blocks.append(block)
            continue
        references = list(
            dict.fromkeys(
                source
                for reference in block.evidence_ids
                for source in aliases.get(reference, [reference])
            )
        )
        blocks.append(block.model_copy(update={"evidence_ids": references}))
        changed = True
    return content.model_copy(update={"blocks": blocks}) if changed else content


async def revalidate_saved_references(engine) -> None:
    """Resume repairs use the normal validator and save only completed results."""
    sections = {section.section_id: section for section in engine.plan.sections}
    owners = {
        section_id: task.task_id
        for task in engine.work_plan.tasks
        for section_id in task.section_ids
    }
    works = engine.store.work_states(engine.job_id)
    for state in engine.store.states(engine.job_id):
        work = works.get(owners.get(state.section_id))
        section = sections.get(state.section_id)
        if work is None or section is None or section.content_mode == "container":
            continue
        content = normalize_fact_references(state.content, state.evidence, work.record.model_dump())
        if content is state.content:
            continue
        revised = await engine.validate_section(
            section, content, state.evidence, state.revision + 1
        )
        engine.store.save_section(engine.job_id, revised)
