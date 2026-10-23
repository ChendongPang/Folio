"""Carry compatible task knowledge into a new research session, never execution state."""

import json
from copy import deepcopy
from pathlib import Path

from folio.models import digest
from folio.orchestration.context import dependency_research_identity


def compatibility_key(assignment, policy, *, legacy_instruction=False):
    # Repair questions and a new allowance do not invalidate source knowledge.
    # Formal responsibilities and source/fact/dependency versions still do.
    fields = ("task", "sections", "source_snapshot_id", "facts_snapshot_id",
              "dependencies")
    identity = {key: assignment.get(key) for key in fields}
    if legacy_instruction:
        identity["instruction"] = assignment.get("instruction")
    identity["dependencies"] = dependency_research_identity(identity.get("dependencies") or [])
    return digest(json.dumps([identity, policy], sort_keys=True))


def saved_assignment(root, round_id):
    path = root / "context" / "sessions" / round_id / "assignment.json"
    if not path.is_file():
        path = root / "context" / "assignment.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def owned_workspace(root, value):
    """Accept only an existing workspace of this exact job/business task."""
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != root.resolve():
        return None
    return path.resolve()


def prepare_session_memory(ledger, task, work, workspace, session, assignment, policy, *, round_id):
    """Return the workspace to use; resume wins over inheritance on every retry."""
    key = compatibility_key(assignment, policy)
    owner_root = workspace.task_root.parent
    if session.get("round_id"):
        # A restored session may have pending calls. Do not import anything again.
        saved_root = owned_workspace(owner_root, session.get("workspace_root"))
        if session.get("workspace_root") and saved_root is None:
            raise ValueError('The task workspace associated with the research checkpoint is unavailable. Existing IDs cannot be discarded to continue')
        if session.get("memory_compatibility_key", key) != key:
            saved = saved_assignment(saved_root or workspace.task_root, round_id)
            if (not saved or compatibility_key(saved, policy) != key
                    or session["memory_compatibility_key"] != compatibility_key(saved, policy, legacy_instruction=True)):
                raise ValueError("The research session's task or source version changed. The old research checkpoint cannot be reused")
            session["memory_compatibility_key"] = key
            ledger.save_session(task.task_id, round_id, session)
        if saved_root and session.get("memory_compatibility_key") == key:
            return saved_root
        return workspace.task_root

    session["memory_compatibility_key"] = key
    session["workspace_root"] = str(workspace.task_root)
    previous = work.research
    old_round = previous.get("round_id")
    if not old_round or old_round == round_id:
        return workspace.task_root
    old = ledger.load_session(task.task_id, old_round)
    if not old.get("round_id"):
        return workspace.task_root
    old_root = owned_workspace(owner_root, old.get("workspace_root", previous.get("task_root")))
    if old_root is None:
        return workspace.task_root
    old_key = old.get("memory_compatibility_key")
    legacy_assignment = None
    if old_key != key:
        # Earlier keys included the current repair question. Compare the saved
        # formal task/source assignment before migrating; never trust a changed
        # key merely because the workspace exists.
        saved = saved_assignment(old_root, old_round)
        if not saved:
            return workspace.task_root
        old_quota = old.get("quota_task_id", task.task_id)
        if old_quota != task.task_id and not old_quota.startswith(task.task_id + ":"):
            return workspace.task_root
        if old_key is None:
            saved_context = digest(json.dumps([
                saved["task"], dependency_research_identity(saved.get("dependencies") or []),
                saved.get("instruction", ""),
            ], sort_keys=True))
            identity = [saved_context, saved.get("writing_gaps", []), policy]
            if old_quota != task.task_id:
                identity.append(old_quota[len(task.task_id) + 1:])
            # Verify a pre-inheritance session using its original repair input,
            # not the new question now being asked.
            if digest(json.dumps(identity, sort_keys=True)) != old_round:
                return workspace.task_root
        elif old_key != compatibility_key(saved, policy, legacy_instruction=True):
            return workspace.task_root
        old_key = compatibility_key(saved, policy)
        legacy_assignment = saved
    if old_key != key:
        return workspace.task_root
    if legacy_assignment is not None:
        # Upgrade the donor before its shared workspace receives a new assignment.
        # A later retry may still reference this donor if the continuation stops.
        old_context = old_root / "context" / "sessions" / old_round
        old_context.mkdir(parents=True, exist_ok=True)
        workspace._atomic_json(old_context / "assignment.json", legacy_assignment)
        old["memory_compatibility_key"] = key
        old["workspace_root"] = str(old_root)
        ledger.save_session(task.task_id, old_round, old)

    # Preserve aliases, OCR reviews and signed artifacts in their original directory.
    # Their identities bind to this path; copying them would invalidate provenance.
    for field in ("notes", "research_memory", "observations", "evidence", "read_progress",
                  "model_deliveries", "evidence_delivery_ranges", "research_seen_results",
                  "coverage_contract", "coverage_confirmed_scopes", "coverage_auto_scopes",
                  "coverage_ids", "required_coverage_ids", "manual_coverage_ids",
                  "coverage_unresolved", "coverage_supersessions", "coverage_review_attempts",
                  "coverage_review_navigation", "coverage_review_observations", "coverage_review_pending"):
        if field in old:
            session[field] = deepcopy(old[field])
    memory = session.get("research_memory", {})
    memory["attempts"] = {}
    memory["stalled_count"] = 0
    if memory:
        session["research_memory"] = memory
    session["missing"] = list(dict.fromkeys([*work.missing, *assignment.get("writing_gaps", [])]))
    session["inherited_from"] = old_round
    session["workspace_root"] = str(old_root)
    # A renewed allowance may have a different quota ID. Grant read access only
    # to the specific receipts already present in this business task's memory.
    allowed = set(old.get("inherited_history_ids", []))
    old_quota = old.get("quota_task_id", task.task_id)
    inherited_ids = []
    for observation in session.get("observations", []):
        request_id = observation.get("request_id")
        record = ledger.get(request_id) if request_id else None
        if record and (record["task_id"] == old_quota or request_id in allowed):
            inherited_ids.append(request_id)
    session["inherited_history_ids"] = list(dict.fromkeys(inherited_ids))
    return old_root
