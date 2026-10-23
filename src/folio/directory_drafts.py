"""Versioned directory drafts, reviewed by a person before any business generation."""

import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from folio.document.guidance import WritingGuide
from folio.document.prepare import DIRECTORY_PREVIEW_VERSION, prepare, prepare_with_model
from folio.document.research_context import directory_confirmation_issues
from folio.legacy_upgrade import upgrade_plan, upgrade_proposal
from folio.models import DocumentPlan
from folio.requirements import RequirementSnapshot
from folio.runtime.language import output_language
from folio.runtime.llm import LLMRuntime


class DirectoryDrafts:
    def __init__(self, root, library):
        self.root = root / "directory-drafts"
        self.library = library

    def load(self, revision, conversation_id):
        if len(revision) != 32 or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("无效目录版本")
        folder = self.root / revision
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        if metadata["conversation_id"] != conversation_id:
            raise ValueError("目录不属于当前会话")
        style_file = folder / "style" / "profile.json"
        return metadata | {
            "folder": folder,
            "sample_paths": [str(folder / name) for name in metadata.get("sample_paths", [])],
            "writing_profile": json.loads(style_file.read_text(encoding="utf-8"))
            if style_file.is_file() else None,
            "plan": DocumentPlan.model_validate(
                upgrade_plan(json.loads((folder / "document-plan.json").read_text()))
            ),
        }

    def refresh_preview(self, directory, conversation_id):
        """Refresh only a generated directory's derived presentation, never its saved inputs."""
        revision = directory.get("revision")
        if not revision:
            # A normal uploaded or delivered document has no authoritative directory plan.
            return directory
        # Read the plan directly; viewing an old directory does not require its source files.
        saved = DirectoryDrafts.load(self, revision, conversation_id)
        if directory.get("confirmable") is False:
            issues = saved.get("confirmation_issues") or directory_confirmation_issues(
                saved.get("research"), saved.get("research_review"))
            if issues:
                directory = directory | {"confirmation_issues": issues}
        if saved.get("source_ids") and not saved.get("research"):
            directory = directory | {
                "confirmable": False,
                "research": {
                    "status": "partial",
                    "summary": "这份旧目录仅依据有限摘录生成，尚未执行目录前资料研究。",
                    "unresolved": ["请重新生成目录，系统会先浏览文件结构并读取相关原文。"],
                    "files": [], "calls": 0, "rounds": 0,
                },
            }
        previous = directory.get("preview") or {}
        if previous.get("presentation_version") == DIRECTORY_PREVIEW_VERSION:
            try:
                self.library.path(previous["id"])
            except KeyError:
                pass
            else:
                return directory
        guide = saved.get("writing_guide") or {}
        cover = saved.get("cover") or {}
        title = saved.get("title") or cover.get("document_title") or guide.get("name") or ("Document" if saved.get("language") == "en" else "文档")
        identity = {
            "revision": revision,
            "plan": saved["plan"].model_dump(mode="json"),
            "title": title,
            "cover": cover,
            "presentation_version": DIRECTORY_PREVIEW_VERSION,
            "writing_profile": saved.get("writing_profile"),
            "language": saved.get("language", "zh-CN"),
        }
        key = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cache = self.root.parent / "directory-preview-cache"
        cache.mkdir(parents=True, exist_ok=True)
        manifest = cache / (key + ".json")
        with FileLock(cache / (key + ".lock"), timeout=60):
            if manifest.is_file():
                preview = json.loads(manifest.read_text(encoding="utf-8"))
                try:
                    self.library.path(preview["id"])
                except KeyError:
                    pass  # A removed derived artifact can be regenerated from the saved plan.
                else:
                    return directory | {"preview": preview}
            with tempfile.TemporaryDirectory(prefix="render-", dir=cache) as temporary:
                report = prepare(
                    Path(temporary),
                    plan=saved["plan"],
                    title=title,
                    cover=cover,
                    defer_pagination=True,
                    writing_profile=saved.get("writing_profile"),
                    language=saved.get("language", "zh-CN"),
                )
                preview = self.library.add(Path(report["artifact"]), "完整目录预览") | {
                    "presentation_version": DIRECTORY_PREVIEW_VERSION
                }
            pending = manifest.with_suffix(".tmp")
            pending.write_text(json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            pending.replace(manifest)
        return directory | {"preview": preview}

    async def create(
        self,
        config,
        conversation_id,
        request,
        *,
        source_catalog,
        previous=None,
        advice="",
        on_progress=None,
        writing_guide=None,
        cover=None,
        context_extra=None,
        title=None,
        requirements=None,
        template_path=None,
        template_name=None,
        source_path=None,
        sample_paths=None,
        resume_from=None,
        language=None,
        explicit_title=None,
    ):
        """``resume_from`` is a loaded draft whose interrupted research should continue."""
        research_replay = None
        if resume_from is not None:
            research_replay = Path(resume_from["folder"]) / "research"
            if not (research_replay / "report.json").is_file():
                raise ValueError("上次资料研究记录不可用，请重新生成目录。")
        requirements = (
            RequirementSnapshot.model_validate(requirements)
            if requirements is not None
            else RequirementSnapshot.from_text(request)
        )
        requirements.ensure_capacity()
        if requirements.text != request:
            raise ValueError("目录需求文本与需求版本不一致")
        guide = WritingGuide.model_validate(writing_guide) if writing_guide is not None else None
        guide_snapshot = guide.model_dump(mode="json") if guide else None
        if previous and previous.get("writing_guide") != guide_snapshot:
            raise ValueError("文档写作指南已改变，不能沿用旧版目录修改")
        revision = uuid4().hex
        folder = self.root / revision
        folder.mkdir(parents=True)
        frozen_samples = []
        for index, sample in enumerate(sample_paths or []):
            sample = Path(sample)
            destination = folder / "style-references" / str(index) / sample.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(sample, destination)
            frozen_samples.append(destination)
        if source_path is not None:
            # Move the frozen inputs before research so cancelled/failed runs keep
            # their exact sources beside the audit, rather than dangling staging paths.
            source_path = Path(source_path)
            shutil.move(source_path, folder / "sources")
            index_path = source_path.with_name(source_path.name + ".index")
            if index_path.exists():
                shutil.move(index_path, folder / "sources.index")
        template = None
        if template_path is not None:
            from folio.document.template_contract import read_template

            frozen_template = folder / "template.docx"
            shutil.copyfile(template_path, frozen_template)
            template = await asyncio.to_thread(
                read_template, frozen_template, name=template_name
            )
            (folder / "template-contract.json").write_text(
                template.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
        context = {}
        from folio.runtime.language import normalize_language

        if language:
            language = normalize_language(language)
            context["ui_language"] = language
        if explicit_title:
            context["document_title_requirement"] = explicit_title
        context["requirements_revision"] = requirements.revision
        context["requirements_note"] = (
            "requirements preserves accepted user requirements and later revisions in chronological order. "
            "Ordinary questions and unconfirmed ambiguous changes are not active requirements. "
            "For explicitly changed or withdrawn requirements, the later original instruction takes precedence; other existing constraints remain active."
        )
        context["requirement_sources"] = [
            {"source_id": entry.source_id, "kind": entry.kind} for entry in requirements.entries
        ]
        context.update(context_extra or {})
        if resume_from is not None:
            # Continuing source research still plans from the current complete
            # requirements. Keep prior edits in the audit without turning this
            # into a second request to revise the interrupted preview.
            context["revision_history"] = list(resume_from.get("revision_history", []))
        if previous:
            context["previous_proposal"] = upgrade_proposal(
                json.loads(
                    (previous["folder"] / "planning-proposal.json").read_text(encoding="utf-8")
                )
            )
            context["revision_history"] = previous.get("revision_history", []) + [advice]
            context["revision_request"] = advice
        runtime = LLMRuntime(config)
        document_id = "web_" + conversation_id
        try:
            report = await prepare_with_model(
                request,
                source_catalog,
                folder,
                runtime=runtime,
                title=title or (cover or {}).get("document_title") or (guide.name if guide else None),
                document_id=document_id,
                context_extra=context,
                on_progress=on_progress,
                writing_guide=guide,
                cover=cover,
                template=template,
                source_dir=folder / "sources" if source_path is not None else None,
                research_cache_root=self.root.parent / "source-cache",
                samples=frozen_samples,
                research_replay=research_replay,
            )
        finally:
            await runtime.aclose()
        # Only publish a new review state after BOTH model validation and Word rendering succeed.
        if on_progress:
            on_progress(
                "Word 目录预览已生成，正在保存可确认版本。"
                if report.get("confirmable", True)
                else "Word 目录初稿已生成，正在保存研究成果和未通过检查的具体原因。"
            )
        preview = await asyncio.to_thread(
            self.library.add,
            Path(report["artifact"]),
            "完整目录预览",
        )
        preview["presentation_version"] = DIRECTORY_PREVIEW_VERSION
        plan = DocumentPlan.model_validate_json((folder / "document-plan.json").read_text())
        if report.get("research") is not None:
            report["research"]["download_url"] = (
                f"/conversations/{conversation_id}/directories/{revision}/research"
            )
        metadata = {
            "conversation_id": conversation_id,
            "revision": revision,
            "language": output_language(request),
            "document_id": document_id,
            "source": "model",
            "revision_history": context.get("revision_history", []),
            "writing_guide": guide_snapshot,
            "cover": dict(cover or {}),
            "requirements": requirements.model_dump(mode="json"),
            "confirmable": report.get("confirmable", True),
            "research": report.get("research"),
            "research_review": report.get("research_review"),
            "confirmation_issues": report.get("confirmation_issues", []),
            "sample_paths": [str(path.relative_to(folder)) for path in frozen_samples],
            "style": report.get("style"),
            **({"resumed_from": resume_from["revision"]} if resume_from is not None else {}),
        }
        (folder / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return {
            "revision": revision,
            "document_id": document_id,
            "source": metadata["source"],
            "title": report["title"],
            "task_usage": report.get("task_usage", []),
            "preview": preview,
            "sections": [
                {
                    "title": s.title,
                    "level": s.level,
                    "number": s.display_number,
                    "instruction": s.instruction,
                    "mode": s.content_mode,
                }
                for s in plan.sections
            ],
            "revision_history": metadata["revision_history"],
            "writing_guide": guide_snapshot,
            "cover": metadata["cover"],
            "requirements_revision": requirements.revision,
            "confirmable": metadata["confirmable"],
            "confirmation_issues": metadata["confirmation_issues"],
            "research": metadata["research"],
            "style": metadata["style"],
            "notes": plan.notes,
        }
