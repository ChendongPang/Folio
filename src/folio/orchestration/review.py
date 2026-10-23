"""Deterministic review groups and a bounded, cancellation-safe worker pool."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class ReviewGroup:
    sections: list
    reasons: list[str]


def review_groups(plan, work_plan, states, width):
    """Review bounded topic groups, not every pair sharing a broad entity name."""
    width = max(2, width)
    by_id = {state.section_id: state for state in states}
    owner = {sid: task.task_id for task in work_plan.tasks for sid in task.section_ids}
    ordered = [by_id[node.section_id] for node in plan.sections if node.content_mode != "container"]
    positions = {state.section_id: index for index, state in enumerate(ordered)}
    windows = [ordered[i : i + width] for i in range(0, len(ordered), width - 1)]
    result, identities = [], []

    def add(ids, reason, *, merge=True):
        identity = set(ids)
        if len({owner[sid] for sid in identity}) < 2:
            return
        for group, existing in zip(result, identities, strict=True):
            if identity <= existing:
                if reason not in group.reasons:
                    group.reasons.append(reason)
                return
        # Combine overlapping topic checks when their union still fits one request.
        # Keep adjacency windows intact so all boundary coverage remains explicit.
        choices = [
            (len(identity & existing), index)
            for index, existing in enumerate(identities)
            if merge
            and index >= window_count
            and identity & existing
            and len(identity | existing) <= width
        ]
        if choices:
            _, index = max(choices)
            identities[index].update(identity)
            result[index].sections = [
                by_id[sid] for sid in sorted(identities[index], key=positions.get)
            ]
            if reason not in result[index].reasons:
                result[index].reasons.append(reason)
            return
        result.append(
            ReviewGroup([by_id[sid] for sid in sorted(identity, key=positions.get)], [reason])
        )
        identities.append(identity)

    window_count = 0
    for window in windows:
        add([s.section_id for s in window], "Adjacent directory sections: check continuity between sections written by different tasks", merge=False)
    window_count = len(result)

    def topic(ids, reason):
        members = sorted(set(ids), key=positions.get)
        # One overlapping member connects consecutive batches; the shared topic is
        # reviewed across its participating sections without constructing its clique.
        for start in range(0, len(members), width - 1):
            add(members[start : start + width], reason)

    facts, subjects = defaultdict(set), defaultdict(set)
    fact_values = defaultdict(lambda: defaultdict(set))
    for state in ordered:
        for claim in state.extraction.claims:
            facts[claim.key].add(state.section_id)
            fact_values[claim.key][claim.value].add(state.section_id)
            if claim.subject.strip():
                subjects[claim.subject.strip().casefold()].add(state.section_id)
        for entity in state.extraction.entities:
            if entity.strip():
                subjects[entity.strip().casefold()].add(state.section_id)
    for key, ids in sorted(facts.items()):
        if len(ids) > 1:
            topic(ids, "Subject, property and applicability of the same fact: " + key)
        # Compare all value representatives together when bounded. Wider keys are
        # handed to one full-scope fact investigation (wide_fact_candidates), not
        # a quadratic collection of pairs or a lossy chain of comparisons.
        values = [
            sorted(members, key=positions.get) for _, members in sorted(fact_values[key].items())
        ]
        if 1 < len(values) <= width:
            representatives = [members[0] for members in values]
            if len({owner[sid] for sid in representatives}) == 1:
                first_owner = owner[representatives[0]]
                for index, members in enumerate(values):
                    alternate = next((sid for sid in members if owner[sid] != first_owner), None)
                    if alternate is not None:
                        representatives[index] = alternate
                        break
            add(
                representatives,
                "Cross-check different values of the same fact: "
                + key
                + "; check actual conditions and meaning; different wording alone does not establish a conflict",
            )
    for subject, ids in sorted(subjects.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(ids) > 1:
            topic(
                ids, "Shared topic or fact subject: " + subject + "; check applicability separately; omission of repeated explanation alone is not a conflict"
            )
    tasks = {task.task_id: task for task in work_plan.tasks}
    for task in work_plan.tasks:
        for dependency in task.depends_on:
            upstream = tasks[dependency]
            left = [sid for sid in upstream.section_ids if sid in positions]
            right = [sid for sid in task.section_ids if sid in positions]
            if left and right:
                # Cover even the tail of asymmetric task dependencies with an
                # actual representative from the opposite task.
                for members, anchor in ((left, right[0]), (right, left[0])):
                    for start in range(0, len(members), width - 1):
                        add(
                            [anchor, *members[start : start + width - 1]],
                            f"Business dependency handoff: {upstream.title} → {task.title}",
                        )
    return result


async def parallel_reviews(batches, limit, review, progress):
    """At most `limit` active groups; results merge in plan order, not completion order."""
    pending = iter(enumerate(batches))
    results = [None] * len(batches)
    completed = 0

    async def worker():
        nonlocal completed
        while (item := next(pending, None)) is not None:
            index, batch = item
            results[index] = await review(batch)
            completed += 1
            progress(completed, len(batches), index + 1)

    workers = [
        asyncio.create_task(worker(), name=f"review-worker-{i}")
        for i in range(min(limit, len(batches)))
    ]
    try:
        await asyncio.gather(*workers)
    finally:
        # Failure or external cancellation must drain in-flight requests before releasing the job lock.
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    return results
