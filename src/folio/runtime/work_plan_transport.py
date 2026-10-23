"""Bound each model-selected business group and verify exact node coverage."""

from copy import deepcopy


class WorkPlanFormatError(ValueError):
    """A complete but invalid plan can be corrected without losing its grouping."""

    def __init__(self, message, candidate):
        super().__init__(message)
        self.candidate = candidate


class WorkPlanCoverageError(WorkPlanFormatError):
    def __init__(self, candidate, missing):
        super().__init__(f"The work plan is missing {len(missing)} content nodes; assign only the unassigned nodes", candidate)
        self.missing = missing


def work_plan_payload(context, schema):
    sections = context.get("sections", [])
    aliases = {s["section_id"]: f"n{i}" for i, s in enumerate(sections, 1)}

    def translate(value, field=None):
        if isinstance(value, dict):
            return {k: translate(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [translate(v, field) for v in value]
        if isinstance(value, str):
            if field in {"section_id", "section_ids"}:
                return aliases.get(value, value)
            if field == "format_feedback":
                for old, new in aliases.items():
                    value = value.replace(old, new)
        return value

    wire = translate(context)
    previous = wire.get("previous_invalid_work_plan")
    if isinstance(previous, dict) and isinstance(previous.get("tasks"), list):
        for task in previous["tasks"]:
            task["node_refs"] = task.pop("section_ids", task.get("node_refs", []))
    schema = deepcopy(schema)
    task = schema["$defs"]["WorkTask"]
    task["properties"].pop("section_ids")
    task["required"].remove("section_ids")
    task["properties"]["node_refs"] = {
        "type": "array",
        "items": {"type": "string", "enum": list(aliases.values())},
        "minItems": 1,
        "maxItems": context.get("execution_budget", {}).get("max_sections_per_task", len(sections)),
        "description": "Select the nodes this task actually owns before summarizing its objective, questions and completion criteria. "
        "Assign every input node exactly once, including overview headings. "
        "The node limit is a maximum, not a requirement to fill each group or select consecutive IDs.",
    }
    task["required"].append("node_refs")
    # Constrained decoders commonly follow property order. Choose responsibility
    # before summarizing it, so a prefabricated title does not drive arbitrary
    # contiguous node ranges. This changes neither IDs nor canonical ownership.
    order = (
        "task_id",
        "node_refs",
        "title",
        "objective",
        "questions",
        "completion_criteria",
        "depends_on",
        "rationale",
    )
    properties = task["properties"]
    task["properties"] = {name: properties[name] for name in order if name in properties} | {
        name: value for name, value in properties.items() if name not in order
    }
    task["required"] = [name for name in order if name in task["required"]] + [
        name for name in task["required"] if name not in order
    ]
    fixed_ids = {t["task_id"] for t in context.get("fixed_work_tasks", [])}
    if fixed_ids:
        available = [
            f"t{i}"
            for i in range(1, len(fixed_ids) + len(sections) + 1)
            if f"t{i}" not in fixed_ids
        ]
        task["properties"]["task_id"]["enum"] = available
        task["properties"]["depends_on"]["items"]["enum"] = sorted(fixed_ids) + available
    minimum = context.get("execution_budget", {}).get("minimum_tasks", 1)
    schema["properties"]["tasks"].update(minItems=max(1, minimum), maxItems=len(sections))
    schema["required"] = ["tasks"]
    return wire, schema, {short: long for long, short in aliases.items()}


def restore_work_plan(data, references, fixed_tasks=()):
    try:
        return _restore_work_plan(data, references, fixed_tasks)
    except WorkPlanCoverageError:
        raise
    except ValueError as exc:
        raise WorkPlanFormatError(str(exc), data) from exc


def _restore_work_plan(data, references, fixed_tasks):
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("The work plan must return nonempty tasks")
    owners, task_ids = {}, {t["task_id"] for t in fixed_tasks}
    restored = deepcopy(list(fixed_tasks))
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
            raise ValueError("A task is missing a valid task_id")
        if task["task_id"] in task_ids:
            raise ValueError("Duplicate business task ID")
        task_ids.add(task["task_id"])
        nodes = task.get("node_refs")
        if not isinstance(nodes, list) or not nodes or any(not isinstance(n, str) for n in nodes):
            raise ValueError("Each task must provide nonempty node_refs")
        for node in nodes:
            if node not in references:
                raise ValueError(f"Unknown node {node}; use only IDs supplied in the input")
            if node in owners:
                raise ValueError(f"Node {node} is assigned more than once; already owned by {owners[node]}")
            owners[node] = task["task_id"]
        restored.append(
            {k: v for k, v in task.items() if k != "node_refs"}
            | {"section_ids": [canonical for ref, canonical in references.items() if ref in nodes]}
        )
    missing = references.keys() - owners.keys()
    if missing:
        raise WorkPlanCoverageError(
            {"tasks": restored}, [references[n] for n in references if n in missing]
        )
    return {k: v for k, v in data.items() if k != "assignments"} | {"tasks": restored}
