"""Snapshot uploaded sources and build bounded planning context from their shared index."""

import hashlib
import json
import re
import shutil
from pathlib import Path
from uuid import uuid4

from folio.directory_drafts import DirectoryDrafts
from folio.research.source_index import SourceIndex
from folio.runtime.language import localize_text, output_language

_TITLE_PATTERNS = (
    re.compile(
        r"(?:文档标题|报告标题|标题|题目)\s*(?:为|是|叫|：|:)\s*"
        r"[“\"「『《]?([^\n”\"」』》]{1,200})"
    ),
    re.compile(
        r"(?:document\s+title|title)\s*(?:is|:)\s*"
        r"[“\"']?([^\n”\"']{1,200})",
        re.IGNORECASE,
    ),
    re.compile(r"\btitled\s+[“\"']?([^\n”\"']{1,200})", re.IGNORECASE),
    re.compile(r"\bcall\s+it\s+[“\"']?([^\n”\"']{1,200})", re.IGNORECASE),
)


def explicit_request_title(messages, writing_guide=None):
    """Return only a title the user explicitly supplied, never an inferred prompt prefix."""
    commands = {
        "你好",
        "您好",
        "hi",
        "hello",
        "开始",
        "继续",
        "开始生成",
        "继续生成",
        "生成文档",
        "编写文档",
        "生成目录",
        "规划目录",
        "确认目录",
        "根据已上传资料和我们的讨论规划目录",
    }
    for message in reversed(messages):
        requirement = message.get("requirement")
        if requirement and (
            requirement.get("status") != "active" or requirement.get("kind") != "request"
        ):
            continue
        if message.get("role") != "user" or message.get("action", "chat") not in {
            "chat",
            "generate",
        }:
            continue
        content = message["content"].strip()
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].startswith("# "):
            candidate = lines[0][2:].strip()
            if candidate.rstrip("。！!,.，?？").lower() not in commands:
                return candidate[:200]
        for pattern in _TITLE_PATTERNS:
            match = pattern.search(content)
            if not match:
                continue
            candidate = match[1].strip().strip("“”\"'「」『』《》").rstrip("。！!,.，?？ ")
            if candidate and candidate.lower() not in commands:
                return candidate[:200]
    name = (writing_guide or {}).get("name")
    if name:
        request = "\n".join(message.get("content", "") for message in messages if message.get("role") == "user")
        return localize_text(name, output_language(request))
    return None


def request_title(messages, writing_guide=None):
    """Compatibility fallback for callers that require a non-empty job title."""
    request = "\n".join(message.get("content", "") for message in messages if message.get("role") == "user")
    return explicit_request_title(messages, writing_guide) or ("Document" if output_language(request) == "en" else "文档")


def source_catalog(source_dir, *, cache_root=None, entries=None, extra_paths=(), include_content=True):
    """Describe every indexed source with explicit bounds; never invent an input directory."""
    from folio.models import digest
    from folio.research.formats import SUPPORTED_SUFFIXES
    from folio.research.source_index import cached_source
    from folio.research.structure import build_structure

    source_dir = Path(source_dir) if source_dir is not None else None
    index = SourceIndex(source_dir, cache_root=cache_root) if source_dir is not None else None
    if index is not None:
        index.prepare()
    if entries is None:
        entries = []
        for path in sorted(source_dir.rglob("*")) if source_dir is not None else []:
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                if not path.resolve().is_relative_to(source_dir.resolve()):
                    raise ValueError("Source symlink points outside the source directory")
                filename = path.relative_to(source_dir).as_posix()
                entries.append(({"id": digest(filename), "name": filename}, filename))
    prepared = [
        (item, index.structure(filename), index.load(filename)) for item, filename in entries
    ]
    seen_paths = {str((source_dir / filename).resolve()) for _, filename in entries}
    for path in map(Path, extra_paths):
        if str(path.resolve()) in seen_paths:
            continue
        seen_paths.add(str(path.resolve()))
        parsed = cached_source(path, cache_root)
        prepared.append(
            (
                {"id": digest(str(path.resolve())), "name": path.name},
                build_structure(parsed),
                parsed,
            )
        )
    catalog = []
    entry_budget = (600000 - 2) // max(1, len(prepared)) - 2
    if entry_budget < 256:
        raise ValueError("目录规划资料数量超出容量，请缩小本次资料范围")
    for item, structure, parsed in prepared:
        metadata = parsed.metadata | structure["metadata"]
        text = "\n".join(block.text for block in parsed.blocks)
        headings = [block.text for block in parsed.blocks if block.kind == "heading"]
        headings.extend(sheet["name"] for sheet in metadata.get("sheets", []))
        headings.extend(slide["title"] for slide in metadata.get("slides", []))
        if structure["format"] == "pdf":
            headings.extend(
                node["title"]
                for node in structure["nodes"]
                if node["kind"] in {"section", "bookmark"}
            )
        entry = {
            "source_id": item["id"],
            "name": item["name"][:120],
            "name_truncated": len(item["name"]) > 120,
            "characters": len(text),
            "source_version": parsed.source_version,
            "format": structure["format"],
            "needs_ocr": metadata.get("needs_ocr", False),
            "warnings": structure["warnings"][:3],
            "headings": [heading[:160] for heading in headings[:8]],
            "headings_truncated": len(headings) > 8 or any(len(h) > 160 for h in headings[:8]),
            "excerpt": text[:entry_budget],
            "excerpt_truncated": len(text) > entry_budget,
        }
        if not include_content:
            # Discovery starts from the manifest. Content is supplied only after
            # the planning researcher actually requests a structure or read view.
            for key in ("headings", "headings_truncated", "excerpt", "excerpt_truncated"):
                entry.pop(key)
            entry["path"] = next(
                (filename for source, filename in entries if source["id"] == item["id"]),
                item["name"],
            )
        # The budget includes JSON keys, names, flags, escaped quotes/newlines,
        # and headings, rather than just counting the main excerpt.
        while len(json.dumps(entry, ensure_ascii=False)) > entry_budget:
            if entry.get("headings"):
                entry["headings"].pop()
                entry["headings_truncated"] = True
            elif entry.get("excerpt"):
                overflow = len(json.dumps(entry, ensure_ascii=False)) - entry_budget
                entry["excerpt"] = entry["excerpt"][: max(0, len(entry["excerpt"]) - overflow)]
                entry["excerpt_truncated"] = True
            elif entry["warnings"]:
                entry["warnings"].pop()
            elif entry["name"]:
                entry["name"] = entry["name"][: len(entry["name"]) // 2]
                entry["name_truncated"] = True
            else:
                raise ValueError("资料元信息超出目录规划容量")
        catalog.append(entry)
    return catalog


def _prepare_inputs(folder, attachments, *, cache_root=None):
    sources = folder / "sources"
    sources.mkdir(parents=True)
    copied = []
    templates = [item for item in attachments if item.get("role", "source") == "template"]
    if len(templates) > 1:
        raise ValueError("每份文档只能使用一个模板，请将其余文件设为普通资料。")
    template_hash = hashlib.sha256(Path(templates[0]["path"]).read_bytes()).digest() if templates else None
    style_hashes = {
        hashlib.sha256(Path(item["path"]).read_bytes()).digest()
        for item in attachments if item.get("role") == "style_reference"
    }
    style_position = 0
    for item in attachments:
        path = Path(item["path"])
        role = item.get("role", "source")
        if role not in {"source", "template", "style_reference"}:
            raise ValueError("文件用途只能是业务资料、文档模板或风格示例。")
        if role == "template":
            if path.suffix.lower() != ".docx":
                raise ValueError("模板请使用 DOCX 文件。")
            shutil.copyfile(path, folder / "template.docx")
            continue
        if role == "style_reference":
            # Keep original names and attachment order; only these isolated
            # snapshots may be sampled for style, never for business evidence.
            destination = folder / "style-references" / str(style_position) / Path(item["name"]).name
            destination.parent.mkdir(parents=True)
            shutil.copyfile(path, destination)
            style_position += 1
            continue
        if template_hash and hashlib.sha256(path.read_bytes()).digest() == template_hash:
            raise ValueError(
                f"普通资料“{item['name']}”与当前模板内容相同，请移除重复附件，避免将示例值作为项目依据。"
            )
        if style_hashes and hashlib.sha256(path.read_bytes()).digest() in style_hashes:
            raise ValueError(
                f"业务资料“{item['name']}”与风格示例内容相同，请移除重复附件，避免将示例值作为项目依据。"
            )
        stem = Path(item["name"]).stem.encode("utf-8")[:170].decode("utf-8", errors="ignore")
        filename = item["id"] + "__" + stem + path.suffix
        shutil.copyfile(path, sources / filename)
        copied.append((item, filename))
    return source_catalog(sources, cache_root=cache_root, entries=copied, include_content=False)


class SourceDirectoryDrafts(DirectoryDrafts):
    async def create_from_sources(
        self,
        config,
        conversation_id,
        attachments,
        request,
        *,
        previous=None,
        advice="",
        writing_guide=None,
        on_progress=None,
        title=None,
        requirements=None,
        resume_from=None,
        language=None,
        explicit_title=None,
    ):
        staging = self.root / ("source-input-" + uuid4().hex)
        staging.mkdir(parents=True)
        explicit_title = explicit_title or explicit_request_title(
            [{"role": "user", "content": request, "action": "generate"}],
            writing_guide,
        )
        if previous:
            title = previous.get("title", title)
        try:
            from folio.research.workflow import local

            if on_progress:
                on_progress("正在准备资料清单和结构索引，随后研究相关原文再规划目录。")
            # A second task.cancel() must not let the finally block delete a
            # staging folder while its copying/indexing worker is still active.
            catalog = await local(
                _prepare_inputs,
                staging,
                attachments,
                cache_root=self.root.parent / "source-cache",
            )
            result = await self.create(
                config,
                conversation_id,
                request,
                source_catalog=catalog,
                source_path=staging / "sources",
                sample_paths=[
                    path for directory in sorted(
                        (staging / "style-references").iterdir(), key=lambda path: int(path.name)
                    ) for path in directory.iterdir() if path.is_file()
                ] if (staging / "style-references").is_dir() else [],
                previous=previous,
                advice=advice,
                writing_guide=writing_guide,
                on_progress=on_progress,
                title=title,
                requirements=requirements,
                resume_from=resume_from,
                language=language,
                explicit_title=explicit_title,
                template_path=(staging / "template.docx") if (staging / "template.docx").is_file() else None,
                template_name=next((f["name"] for f in attachments if f.get("role") == "template"), None),
                context_extra={
                    "source_context_note": "source_catalog lists the same-version sources; it does not prove they have been read. "
                    "directory_research records original text actually read and coverage requirements before planning; unread ranges are unverified. "
                    "After directory confirmation, the business agent reuses this original evidence and continues deeper research.",
                    "planning_instructions": "Plan from the user's writing requirements and sources. "
                    "When template is present, strictly preserve its fixed sections, required tables and extension boundaries, then assign section instructions. "
                    "Without a template, organize a complete directory yourself; do not use filenames or entire user instructions as section titles. "
                    "Template instructions guide completion; example values are not project facts. Preserve template formatting when provided. "
                    "writing_profile supplements unspecified style only; it is neither business evidence nor a fixed section requirement. "
                    "Use built-in formatting for remaining unspecified details. "
                    "Where sources are missing, state what evidence writing requires; never invent confirmed facts from excerpts.",
                },
            )
            folder = self.root / result["revision"]
            metadata_path = folder / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.update(
                title=result["title"],
                source_ids=[f["id"] for f in attachments],
                source_roles=[{"id": f["id"], "role": f.get("role", "source")} for f in attachments],
            )
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return result | {
                "title": result["title"],
                "source_count": sum(f.get("role", "source") == "source" for f in attachments),
                **({"style_url": f"/conversations/{conversation_id}/directories/{result['revision']}/style"}
                   if (folder / "style" / "style.md").is_file() else {}),
            }
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def load(self, revision, conversation_id):
        previous = super().load(revision, conversation_id)
        previous["knowledge_path"] = previous["folder"] / "sources"
        if not previous["knowledge_path"].is_dir():
            raise ValueError("旧版目录未保存当前资料快照，请重新生成目录")
        return previous
