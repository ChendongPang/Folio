"""Deterministic offline demonstration; never treated as semantic validation."""

import json
import re

from folio.models import digest
from folio.research.native_tools import NATIVE_TOOL_TASKS
from folio.runtime.base import RuntimeResult
from folio.runtime.language import localize_text, normalize_language, output_language


def _demo_text(text, context):
    language = context.get("language") or context.get("output_language")
    if not language:
        language = output_language(context.get("user_request") or context.get("requirements") or context.get("latest_user_message"))
    return localize_text(text, normalize_language(language))


class DemoRuntime:
    async def complete(
        self, task: str, context: dict, schema: dict, session_key: str
    ) -> RuntimeResult:
        if task == "style_analysis":
            result = {
                "rules": [], "request_sample_ids": [],
                "uncertainties": [_demo_text("离线演示未执行语义风格判断，仅采用可靠的程序测量。", context)],
            }
        elif task == "directory_research":
            result = self.directory_research(context, session_key)
        elif task == "review_directory_research":
            node = next(item for item in context["directory"] if item["content_mode"] == "generate")
            result = {"checks": [{
                "requirement_id": item["requirement_id"], "disposition": "covered",
                "node_ids": [node["node_id"]], "evidence_ids": item["evidence_ids"],
                "reason": _demo_text("离线演示映射，不代表真实语义覆盖验收。", context),
            } for item in context["researched_requirements"]]}
        elif task == "coverage_obligations":
            result = {"required_scopes": [], "unresolved_scopes": [],
                      "reason": _demo_text("离线演示不推断完整覆盖义务，不代表真实业务范围判断。", context)}
        elif task == "research_step":
            files = context.get("catalog", {}).get("files", [])
            if not context.get("observations") and files:
                result = {"tool": "fs.read", "arguments": {"path": files[0]["path"]},
                          "summary": _demo_text("离线演示读取一份资料", context), "notes": _demo_text("离线演示，不代表自主研究质量。", context)}
            else:
                result = {"tool": "finish", "notes": _demo_text("离线演示研究结束，不代表完整资料覆盖。", context),
                          "missing_information": [] if context.get("evidence_catalog") else [_demo_text("离线演示未取得可写依据", context)]}
        elif task == "verify_derivation":
            result = {"supported": False, "source_matches": False, "computation_matches": False,
                      "business_scope_matches": False, "reason": _demo_text("离线演示无法独立证明计算结论", context)}
        elif task == "review_coverage_exclusion":
            result = {"approved": False, "reason": _demo_text("离线演示不能代替独立业务范围审查", context)}
        elif task == "review_unused_artifact":
            result = {
                "artifact_id": context["issue"]["artifact_id"],
                "body_version": context["body_version"],
                "resolved_as_unused": False,
                "claim_present": False,
                "task_requirements_met": False,
                "alternative_evidence_ids": [],
                "block_refs": [],
                "reason": _demo_text("离线演示不能证明未核验计算已不影响交付，保留待确认。", context),
            }
        elif task == "plan_review":
            result = {"checks": []}
        elif task == "read_review_materials":
            result = {
                "notes": [
                    {
                        "source_refs": [item["context_ref"]],
                        "source_kind": item["kind"],
                        "text": _demo_text("离线演示阅读记录，不代表真实语义审阅。", context),
                    }
                    for item in context["catalog"]
                ]
            }
        elif task == "select_context":
            result = {
                "selected_refs": [
                    x["context_ref"] for x in context["catalog"] if x["kind"] != "risk"
                ]
            }
        elif task == "work_plan":
            # Offline fixtures deliberately keep independent tasks; semantic grouping belongs to LLM.
            result = {
                "tasks": [
                    {
                        "task_id": f"work_{i}",
                        "title": s["heading_path"][-1],
                        "objective": s["instruction"] or s["heading_path"][-1],
                        "section_ids": [s["section_id"]],
                        "questions": [_demo_text("材料给出的业务规则是什么？", context)],
                        "completion_criteria": [_demo_text("准确描述材料中的规则", context)],
                        "rationale": _demo_text("离线演示，不代表语义分组判断", context),
                    }
                    for i, s in enumerate(context["sections"])
                ]
            }
        elif task == "understand_work":
            result = {
                "summary": context["work_task"]["objective"],
                "facts": [
                    {
                        "fact_id": f"fact_{i}",
                        "statement": e["text"][:1500],
                        "evidence_ids": [e["evidence_id"]],
                    }
                    for i, e in enumerate(context["evidence"])
                ],
            }
        elif task == "read_gap_materials":
            result = {
                "constraints": [
                    {
                        "source_refs": [item["context_ref"]],
                        "source_kind": item["kind"],
                        "text": _demo_text("离线演示阅读记录，不代表语义理解或真实业务判断。", context),
                    }
                    for item in context["catalog"]
                ]
            }
        elif task == "complete_gaps":
            result = {
                "proposals": [
                    {
                        "gap_id": g["gap_id"],
                        "section_ids": [s["section_id"] for s in context["sections"]],
                        "content": _demo_text("建议由业务人员录入并复核业务要素，校验通过后提交处理；校验失败时记录原因并退回修改。此为离线演示建议。", context),
                    }
                    for g in context["gaps"]
                ]
            }
        elif task == "next_action":
            result = {
                "action": "search"
                if context["search_count"] == 0
                else "write"
                if context["evidence"]
                else "pending",
                "summary": _demo_text("演示：仅依据原文判断，不代表真实语义评估", context),
                "missing_information": [] if context["evidence"] else [_demo_text("缺少业务依据", context)],
            }
        elif task == "plan":
            # Offline examples may use Markdown headings to make their fixtures
            # repeatable; production planning is always performed by the LLM.
            request = context.get("requirements", context.get("user_request", ""))
            sections = []
            for line in request.splitlines():
                heading = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
                if heading:
                    sections.append({
                        "node_id": f"demo_{len(sections) + 1}",
                        "title": heading[2],
                        "level": min(len(heading[1]), sections[-1]["level"] + 1)
                        if sections else 1,
                        "instruction": _demo_text(f"描述{heading[2]}的业务要求", context),
                        "content_mode": "generate",
                    })
                elif sections and line.strip():
                    sections[-1]["instruction"] += "\n" + line.strip()
            result = {
                "sections": sections or [{
                    "node_id": "demo_business",
                    "title": _demo_text("业务说明", context),
                    "level": 1,
                    "instruction": request or _demo_text("根据资料描述业务要求", context),
                    "content_mode": "generate",
                }],
                "notes": [_demo_text("离线演示目录，不代表真实模型对业务范围的规划。", context)],
            }
        elif task == "query":
            result = {"queries": [context["work_task"]["title"]]}
        elif task == "integrate_supplement":

            proposal = context["integration_item"]
            result = {
                "updates": [],
                "resolved_pending_items": [
                    text
                    for text in context["current"].get("pending_items", [])
                    if digest(text) == proposal["gap_id"]
                ],
            }
            if proposal.get("disposition", "body") == "body":
                evidence = context.get("evidence", [])
                if not proposal.get("evidence_refresh") or evidence:
                    retrieved = bool(proposal.get("evidence_refresh"))
                    result["updates"] = [
                        {
                            "block_id": "supplement_" + proposal["gap_id"],
                            "text": evidence[0]["text"][:1500]
                            if retrieved
                            else proposal["content"],
                            "origin": "evidence" if retrieved else "model_proposal",
                            "evidence_ids": [evidence[0]["evidence_id"]] if retrieved else [],
                        }
                    ]
                    if any(
                        block["origin"] == result["updates"][0]["origin"]
                        and block.get("text") == result["updates"][0]["text"]
                        and block.get("evidence_ids", []) == result["updates"][0]["evidence_ids"]
                        for block in context["current"]["blocks"]
                    ):
                        result["updates"] = []
                    # Offline fixture mirrors a source audit only for blocks
                    # explicitly named in this gap; unrelated paragraphs stay intact.
                    for block in context["current"]["blocks"]:
                        if (
                            block["block_id"] in proposal.get("gap_description", "")
                            and block["origin"] == "evidence"
                            and not block.get("evidence_ids")
                        ):
                            result["updates"].append(block | {"origin": "model_proposal"})
                else:
                    result["resolved_pending_items"] = []
        elif task == "check_supplement_conflicts":
            result = {
                "checks": [
                    {
                        "gap_id": p["gap_id"],
                        "status": "uncertain",
                        "reason": _demo_text("离线演示不能核实冲突。", context),
                    }
                    for p in context["proposals"]
                ]
            }
        elif task in {"generate", "repair"}:
            evidence = context["evidence"]
            result = {
                "blocks": [
                    {
                        "block_id": "body",
                        "kind": "paragraph",
                        "text": evidence[0]["text"][:1500]
                        if evidence
                        else _demo_text("建议录入业务要素后进行完整性校验，失败时保留记录并提示补正。", context),
                        "origin": "evidence" if evidence else "model_proposal",
                        "evidence_ids": [evidence[0]["evidence_id"]] if evidence else [],
                    }
                ],
                "pending_items": [],
            }
            # Demo models the writer integrating business supplements, never an engine append.
            for index, proposal in enumerate(context.get("model_proposals", [])):
                if (
                    proposal.get("disposition", "body") == "body"
                    and context["section"]["section_id"] in proposal["section_ids"]
                ):
                    result["blocks"].append(
                        {
                            "block_id": f"supplement_{index}",
                            "kind": "paragraph",
                            "text": proposal["content"],
                            "origin": "model_proposal",
                            "evidence_ids": [],
                        }
                    )
        elif task == "extract":
            result = {
                "summary": (context["content"]["blocks"][0]["text"] or _demo_text("表格内容", context))[:500],
                "claims": [
                    {
                        "claim_id": f"claim_{i}",
                        "block_ids": [b["block_id"]],
                        "statement": b["text"],
                        "subject": context["section"]["title"],
                        "predicate": _demo_text("材料原文", context),
                        "value": b["text"][:1000],
                        "evidence_ids": b["evidence_ids"],
                        "status": "candidate",
                    }
                    for i, b in enumerate(context["content"]["blocks"])
                    if b["evidence_ids"]
                ],
                "entities": [],
            }
        elif task == "validate":
            evidence = {e["evidence_id"]: e["text"] for e in context["evidence"]}
            result = {
                "verdicts": [
                    {
                        "claim_id": c["claim_id"],
                        "supported": any(
                            c["statement"] in evidence.get(e, "") for e in c["evidence_ids"]
                        ),
                        "reason": _demo_text("演示模式仅检查材料原文匹配", context),
                    }
                    for c in context["extraction"]["claims"]
                ],
                "findings": [],
            }
        elif task in {"review", "review_work", "review_focus"}:
            result = {"findings": []}
        elif task == "drill_down":
            result = {
                "confirmed": True,
                "reason": _demo_text("演示运行器不裁决冲突", context),
                "needs_confirmation": True,
                "affected_section_ids": [],
            }
        else:
            raise ValueError(f"Unknown task: {task}")
        if task in NATIVE_TOOL_TASKS:
            value = dict(result)
            if task in {"directory_research", "research_step"}:
                updates = self.research_notes(context)
                if updates:
                    value["research_notes"] = updates
                if context.get("memory_checkpoint_required"):
                    value = {"tool": "research.save", "research_notes": updates,
                             "summary": _demo_text("离线演示保存已提供原文的阅读记录", context)}
                name = value.pop("tool").replace(".", "_")
                args = value.pop("arguments", {})
                # Directory finish owns a substantive conclusion summary.
                args = {"summary": _demo_text("离线演示资料研究", context), **value, **args}
            elif task == "next_action":
                name, args = value.pop("action"), value
            elif task == "coverage_obligations":
                name, args = "submit_coverage", value
            else:
                name = "search_sources" if task == "query" else "search_web"
                args = value
            return RuntimeResult("", tool_calls=[{
                "id": "demo_" + digest(session_key + json.dumps(args, ensure_ascii=False))[:24],
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }])
        return RuntimeResult(json.dumps(result, ensure_ascii=False))

    @staticmethod
    def research_notes(context):
        """Offline read receipts with quotes, not a claim of semantic analysis."""
        updates = []
        for observation in context.get("observations", []):
            if observation.get("tool") != "fs.read":
                continue
            result = observation.get("result", {})
            raw = {item["evidence_id"]: item for item in result.get("evidence", [])}
            text = observation.get("model_text", "")
            if observation.get("delivery_complete") is False:
                text = text[:observation["delivered_text_chars"]]
            visible = [span["evidence_id"] for span in result.get("model_evidence_spans", [])
                       if span.get("complete", True) and span["evidence_id"] in raw
                       and span["end"] <= len(text)
                       and text[span["start"]:span["end"]] == raw[span["evidence_id"]]["text"]]
            if not visible:
                continue
            first = raw[visible[0]]
            updates.append({
                "topic": _demo_text(f"离线阅读记录 · {first.get('locator', '原文')}", context),
                "text": _demo_text(f"离线演示已提供本批原文，不代表语义研究质量。首段摘录：{first['text'][:500]}", context),
                "kind": "source_statement", "evidence_ids": visible[:80],
            })
        return updates[:12]

    def directory_research(self, context, session_key):
        """Exercise real paginated tools offline; never claim semantic evaluation."""
        sessions = getattr(self, "_directory_sessions", {})
        self._directory_sessions = sessions
        state = sessions.setdefault(context.get("research_session_id") or session_key.rsplit(":", 1)[0], {
            "files": {}, "seen": set(), "inspected": {}, "reads": {}, "evidence": {},
            "list_next": context["manifest"]["next_offset"],
        })
        state["files"].update({item["path"]: item for item in context["manifest"]["files"]})
        for observation in context["observations"]:
            if observation["request_id"] in state["seen"]:
                continue
            state["seen"].add(observation["request_id"])
            value = observation["result"]
            if "error" in value:
                continue
            tool, args = observation["tool"], observation.get("arguments", {})
            if tool == "fs.list":
                state["files"].update({item["path"]: item for item in value["files"]})
                state["list_next"] = value["next_offset"]
            elif tool == "fs.inspect":
                state["inspected"][args["path"]] = value["next_offset"]
            elif tool == "fs.read" and "range_id" in value:
                state["reads"][args["path"]] = value
                state["evidence"].update({item["evidence_id"]: item for item in value["evidence"]})
        tool, args = "finish", {}
        if state["list_next"] is not None:
            tool, args = "fs.list", {"offset": state["list_next"]}
        else:
            for path in state["files"]:
                if path not in state["inspected"] or state["inspected"][path] is not None:
                    tool, args = "fs.inspect", {"path": path, "offset": state["inspected"].get(path) or 0}
                    break
                read = state["reads"].get(path)
                if read is None or read["next_offset"] is not None:
                    tool, args = "fs.read", {"path": path, "offset": read["next_offset"] if read else 0, "limit": 100}
                    break
        if tool == "finish":
            args = {
                "summary": _demo_text("离线演示已执行文件浏览与分页读取，不代表真实研究质量。", context),
                "requirements": [{"requirement_id": f"r{index}", "text": item["text"][:1000],
                                  "evidence_ids": [item["evidence_id"]]}
                                 for index, item in enumerate(state["evidence"].values())],
            }
        return {"tool": tool, "arguments": args,
                "summary": _demo_text("离线演示资料研究", context), "notes": _demo_text("离线演示，不代表模型研究质量。", context)}

    async def aclose(self) -> None:
        pass
