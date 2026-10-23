"""Conversation-bound human answers and explicit retry grants."""

from pathlib import Path

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, field_validator

from folio.confirmations import confirmation_view, rounds, submit_confirmation
from folio.requirements import sync_requirements


class ConfirmationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_id: str = Field(min_length=1, max_length=100)
    answer: str = Field(min_length=1, max_length=600000)
    close_comment: bool = False

    @field_validator("answer")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("请填写问题的答复")
        return value.strip()


class ConfirmationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: str = Field(min_length=1, max_length=100)
    answers: list[ConfirmationAnswer] = Field(default_factory=list, max_length=40)
    additional_calls: int = Field(default=200, ge=1, le=10000)


def install_confirmation_routes(
    app, store, check_origin, conversation, save_conversation, start_job
):
    def owned(job_id, conversation_id):
        state = conversation(conversation_id)
        try:
            store.get(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        if state.get("active_job") != job_id:
            raise HTTPException(409, "任务不属于当前会话，请刷新后重试。")
        return state

    @app.get("/jobs/{job_id}/confirmation")
    def confirmation(job_id: str, conversation_id: str = Query(pattern=r"^[a-f0-9]{32}$")):
        owned(job_id, conversation_id)
        return confirmation_view(store, job_id)

    @app.post("/jobs/{job_id}/confirmation")
    async def answer(job_id: str, data: ConfirmationInput, request: Request):
        from folio.runtime.language import request_language

        english = request_language(request) == "en"
        check_origin(request)
        try:
            with FileLock(store.root / f"conversation-{data.conversation_id}.lock").acquire(
                timeout=0
            ):
                state = owned(job_id, data.conversation_id)
                with FileLock(store.root / f"{job_id}.lock").acquire(timeout=0):
                    job = submit_confirmation(
                        store,
                        job_id,
                        request_id=data.request_id,
                        revision=data.revision,
                        answers=[a.model_dump() for a in data.answers],
                        additional_calls=data.additional_calls,
                    )
                marker = "confirmation:" + data.request_id
                if not any(m.get("confirmation_id") == marker for m in state.get("messages", [])):
                    saved_round = next(
                        r for r in rounds(store, job_id) if r["request_id"] == data.request_id
                    )
                    amendments = [
                        a
                        for a in saved_round["data"]["answers"]
                        if not a["finding"]["key"].startswith(
                            ("review-incomplete:", "local-check-incomplete:")
                        )
                    ]
                    state.setdefault("messages", []).append(
                        {
                            "role": "user",
                            "action": "confirmation",
                            "confirmation_id": marker,
                            "content": ("Confirm details and continue:\n" if english else "补充确认并继续处理：\n")
                            + "\n".join(
                                ("Sections " if english else "章节 ")
                                + "、".join(a["finding"]["section_ids"])
                                + "："
                                + a["finding"]["description"]
                                + ("\nAnswer: " if english else "\n答复：")
                                + a["answer"]
                                for a in amendments
                            )
                            if amendments
                            else ("Recheck the saved document" if english else "重新检查已保存的文档"),
                            "requirement": {
                                "status": "active" if amendments else "command",
                                "kind": "revision",
                                "source": "confirmation",
                            },
                        }
                    )
                    sync_requirements(state)
                    save_conversation(data.conversation_id, state)
                if job["status"] in {"queued", "running"}:
                    start_job(job_id)
                return {
                    "job_id": job_id,
                    "status": job["status"],
                    "text": "已保存本轮答复和调用额度，将继续检查已有文档。",
                }
        except Timeout as exc:
            raise HTTPException(409, "任务或会话正在更新，请稍后重试。") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/jobs/{job_id}/confirmations/{request_id}/artifact")
    def previous_artifact(job_id: str, request_id: str, request: Request):
        from folio.runtime.language import request_language
        matches = [row for row in rounds(store, job_id) if row["request_id"] == request_id]
        path = matches[0]["data"].get("previous_artifact") if matches else None
        if not path or not Path(path).is_file():
            raise HTTPException(404, "原草稿不存在")
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="draft-before-answers.docx" if request_language(request) == "en" else "补答前草稿.docx",
        )
