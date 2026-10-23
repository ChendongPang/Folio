"""Continue an interrupted directory research run from its recorded model responses.

Replay re-executes the same local tools against the same frozen sources. A
recorded model response is reused only while the rebuilt request is identical to
the one originally sent; the first difference, or the first request without a
recorded response, goes to the model as usual. No research state is guessed or
copied, so the continued run keeps every existing validation.
"""

import json
from copy import deepcopy
from pathlib import Path

from folio.models import digest
from folio.runtime.base import RuntimeResult

# Interruptions that still produced a saved draft. Exhausted call/round budgets,
# context limits and validated conclusions need new inputs or a fresh run.
RESUMABLE_TERMINATIONS = frozenset({"model_timeout", "time_limit"})
REPLAYABLE_WEB_STATUSES = frozenset({"completed", "failed"})


def request_signature(context):
    """Identify the model-visible research state, not the run folder or wall clock."""
    value = {key: item for key, item in context.items() if key != "research_session_id"}
    if isinstance(value.get("budget"), dict):
        value["budget"] = {key: item for key, item in value["budget"].items() if key != "seconds_remaining"}
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Research audit records must be JSON objects")
    return value


class DirectoryResearchReplay:
    def __init__(self, root):
        self.root = Path(root)
        try:
            self.inputs = _read(self.root / "input.json")
            report = _read(self.root / "report.json")
            web = self.root / "web-research.json"
            web_state = _read(web) if web.is_file() else {}
            if any(not isinstance(web_state.get(key, []), list) for key in ("searches", "evidence")):
                raise ValueError("Public research records must contain lists")
            self.steps = {}
            events = self.root / "events"
            for path in sorted(events.glob("research_[0-9][0-9][0-9]-request.json")):
                request_id = path.name.removesuffix("-request.json")
                response = events / f"{request_id}-response.json"
                if not response.is_file():
                    continue
                request = _read(path)
                context = request["context"]
                if not isinstance(context, dict):
                    raise ValueError("Research request context must be an object")
                if context.get("research_finalizing"):
                    # A finish-only handoff is never the next step of a continued run.
                    continue
                recorded = _read(response)
                if (not isinstance(recorded.get("usage") or {}, dict)
                        or not isinstance(recorded.get("tool_calls") or [], list)
                        or (recorded.get("reasoning_content") is not None
                            and not isinstance(recorded["reasoning_content"], str))):
                    raise ValueError("Research response has invalid usage or tool calls")
                self.steps[request_id] = (request_signature(context), recorded)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError('Previous source research records are unavailable. Generate the outline again.') from exc
        self.termination_reason = report.get("termination_reason")
        self.web_searches = [item for item in web_state.get("searches", []) if isinstance(item, dict)]
        self.web_evidence = {item["evidence_id"]: item for item in web_state.get("evidence", [])
                             if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)}
        self.active = True
        self.replayed = 0
        self.continued_at = None

    def check(self, requirements, template, source_snapshot_id):
        if self.termination_reason not in RESUMABLE_TERMINATIONS:
            raise ValueError('Previous source research was not interrupted by a timeout and cannot resume from that point. Generate the outline again.')
        if (self.inputs.get("requirements") != requirements
                or _canonical(self.inputs.get("template")) != _canonical(template)):
            raise ValueError('Writing requirements or the document template changed. Previous source research cannot be reused; generate the outline again.')
        if self.inputs.get("source_snapshot_id") != source_snapshot_id:
            raise ValueError('Sources changed. Previous source research cannot be reused; generate the outline again.')

    def take(self, request_id, context):
        """Return the recorded response for an identical request; otherwise stop replaying."""
        if self.active:
            recorded = self.steps.get(request_id)
            if recorded is not None and recorded[0] == request_signature(context):
                self.replayed += 1
                response = recorded[1]
                return RuntimeResult(
                    response.get("content") or "", usage=dict(response.get("usage") or {}),
                    stop_reason=response.get("stop_reason") or "completed",
                    tool_calls=deepcopy(response.get("tool_calls") or []),
                    reasoning_content=response.get("reasoning_content"),
                )
            self.active = False
        if self.continued_at is None:
            self.continued_at = request_id
        return None

    def take_web(self, query, purpose):
        """Reuse the next recorded public search, timestamps and W references included."""
        if not self.active:
            return None
        record = self.web_searches[0] if self.web_searches else None
        evidence = [self.web_evidence.get(key) for key in (record or {}).get("evidence_ids", [])]
        if (record is None or record.get("query") != query or record.get("purpose") != purpose
                or record.get("status") not in REPLAYABLE_WEB_STATUSES or None in evidence):
            # The original run never received this result, so the next request
            # differs as well; it and everything after it go to the model.
            self.active = False
            return None
        self.web_searches.pop(0)
        return deepcopy(record), deepcopy(evidence)

    def summary(self):
        return {"resumed_from": str(self.root), "replayed_calls": self.replayed,
                "continued_at": self.continued_at}
