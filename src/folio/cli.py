import argparse
import asyncio
import json
import sys
from pathlib import Path

from filelock import Timeout

from folio.document.guidance import load_guide
from folio.models import JobInput, RagflowConfig
from folio.research.contracts import ResearchConfig
from folio.service import execute
from folio.store import Store


def print_job(job):
    print(
        json.dumps(
            {
                key: job[key]
                for key in (
                    "id",
                    "document_id",
                    "status",
                    "stage",
                    "completed_sections",
                    "total_sections",
                    "calls",
                    "artifact",
                    "error",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def worker(store, config, once):
    while True:
        ran = False
        for job_id in store.candidates():
            try:
                print_job(await execute(store, job_id, config))
                ran = True
                if once:
                    return
            except Timeout:
                continue  # Another process owns the job; also covers crashed-worker recovery.
            except Exception as exc:
                print(f"{job_id}: {exc}", file=sys.stderr)
                if once:
                    raise
        if once:
            return
        if not ran:
            await asyncio.sleep(2)


def main():
    parser = argparse.ArgumentParser(prog="folio")
    parser.add_argument("--data-dir", type=Path, default=Path(".folio"))
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("generate", help="Create and run a document generation job")
    request = run.add_mutually_exclusive_group(required=True)
    request.add_argument("--request", help="The document's writing requirements")
    request.add_argument("--request-file", type=Path, help="UTF-8 writing requirements file")
    sources = run.add_mutually_exclusive_group()
    sources.add_argument("--knowledge", type=Path)
    sources.add_argument("--mcp-url")
    run.add_argument("--dataset-id", action="append", default=[])
    run.add_argument("--source-document-id", action="append", default=[])
    run.add_argument(
        "--reference",
        type=Path,
        action="append",
        default=[],
        help="Explicit project factual source (not an output example)",
    )
    run.add_argument(
        "--sample",
        type=Path,
        action="append",
        default=[],
        help="Output example: writing form and detail only",
    )
    run.add_argument("--revision-view", choices=["final", "original"], default="final")
    run.add_argument("--title", help="Explicit document title; otherwise derive it from the planned outline")
    run.add_argument(
        "--document-type",
        choices=["urd", "srs"],
        help="Optional advisory writing guide: URD business needs or SRS system requirements",
    )
    run.add_argument("--document-id", default="")
    run.add_argument("--runtime", choices=["llm", "demo"], default="llm")
    run.add_argument("--config", type=Path)
    run.add_argument("--enqueue", action="store_true")
    preview = sub.add_parser(
        "prepare", help="Use the configured LLM to plan the complete directory and render a preview"
    )
    request = preview.add_mutually_exclusive_group(required=True)
    request.add_argument("--request", help="The document's writing requirements")
    request.add_argument("--request-file", type=Path, help="UTF-8 writing requirements file")
    preview.add_argument("--knowledge", type=Path, required=True)
    preview.add_argument("--output-dir", type=Path, required=True)
    preview.add_argument("--title", help="Explicit document title; otherwise derive it from the planned outline")
    preview.add_argument(
        "--document-type",
        choices=["urd", "srs"],
        help="Optional advisory writing guide for directory planning",
    )
    preview.add_argument("--config", type=Path)
    reformat = sub.add_parser(
        "reformat", help="Re-layout a completed job without calling the model"
    )
    reformat.add_argument("job_id")
    reformat.add_argument("--output-dir", type=Path, required=True)
    reformat.add_argument("--require-pagination", action="store_true")
    for command, help_text in (
        ("render-ui", "Render a static HTML interface prototype to a PNG image"),
        ("render-flowchart", "Render a Mermaid flowchart to a PNG image"),
    ):
        visual = sub.add_parser(command, help=help_text)
        visual.add_argument("source", type=Path, help="UTF-8 source file")
        visual.add_argument("--output-dir", type=Path, required=True)
    resume = sub.add_parser("resume", help="Resume an interrupted job from checkpoints")
    resume.add_argument("job_id")
    resume.add_argument("--config", type=Path)
    status = sub.add_parser("status")
    status.add_argument("job_id")
    work = sub.add_parser("worker")
    work.add_argument("--config", type=Path)
    work.add_argument("--once", action="store_true")
    datasets = sub.add_parser("datasets", help="List RAGFlow datasets through MCP")
    datasets.add_argument("--mcp-url", required=True)
    datasets.add_argument("--name")
    datasets.add_argument("--page", type=int, default=1)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        if args.command in {"render-ui", "render-flowchart"}:
            from folio.visuals.render import render_flowchart, render_ui

            try:
                source = args.source.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Figure source files must be UTF-8 text") from exc
            renderer = render_ui if args.command == "render-ui" else render_flowchart
            result = asyncio.run(renderer(source, args.output_dir))
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return

        request_text = ""
        if args.command in {"generate", "prepare"}:
            request_text = (
                args.request_file.read_text(encoding="utf-8")
                if args.request_file else args.request
            )
            if not request_text.strip():
                raise ValueError("Writing requirements must not be empty")
        store = Store(args.data_dir)
        if args.command == "generate":
            writing_guide = load_guide(args.document_type) if args.document_type else None
            from folio.runtime.language import output_language
            from folio.source_documents import explicit_request_title

            title = (
                args.title
                or explicit_request_title(
                    [{"role": "user", "content": request_text, "action": "generate"}],
                    writing_guide,
                )
                or ("Document" if output_language(request_text) == "en" else "文档")
            )
            job_id = store.create(
                JobInput(
                    title=title,
                    document_id=args.document_id,
                    user_request=request_text,
                    language=output_language(request_text),
                    knowledge_path=str(args.knowledge) if args.knowledge else None,
                    ragflow=RagflowConfig(
                        url=args.mcp_url,
                        dataset_ids=args.dataset_id,
                        document_ids=args.source_document_id,
                    )
                    if args.mcp_url
                    else None,
                    runtime=args.runtime,
                    research=ResearchConfig() if args.runtime == "llm" else None,
                    reference_paths=[str(p) for p in args.reference],
                    sample_paths=[str(p) for p in args.sample],
                    reference_revision_view=args.revision_view,
                    writing_guide=writing_guide,
                )
            )
            # Emit the identifier before any external call so failures can be inspected/resumed.
            print(f"job_id={job_id}", flush=True)
            job = (
                store.get(job_id)
                if args.enqueue
                else asyncio.run(execute(store, job_id, args.config))
            )
            print_job(job)
        elif args.command == "prepare":
            from folio.document.prepare import prepare_with_model
            from folio.runtime.llm import LLMRuntime
            from folio.service import resolve_model_config

            writing_guide = load_guide(args.document_type) if args.document_type else None

            async def preview_plan():
                runtime = LLMRuntime(resolve_model_config(store.root, args.config))
                try:
                    from folio.source_documents import source_catalog

                    catalog = await asyncio.to_thread(
                        source_catalog, args.knowledge, cache_root=store.root / "source-cache",
                        include_content=False,
                    )
                    return await prepare_with_model(
                        request_text,
                        catalog,
                        args.output_dir,
                        runtime=runtime,
                        title=args.title,
                        writing_guide=writing_guide,
                        source_dir=args.knowledge,
                        research_cache_root=store.root / "source-cache",
                    )
                finally:
                    await runtime.aclose()

            result = asyncio.run(preview_plan())
            print(
                json.dumps(
                    {
                        k: result[k]
                        for k in [
                            "status",
                            "artifact",
                            "plan_nodes",
                            "content_sections",
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "reformat":
            from folio.document.reformat import reformat_job

            print(
                json.dumps(
                    reformat_job(
                        store,
                        args.job_id,
                        args.output_dir,
                        require_pagination=args.require_pagination,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "resume":
            store.resume_job(args.job_id)
            print_job(asyncio.run(execute(store, args.job_id, args.config)))
        elif args.command == "status":
            print_job(store.get(args.job_id))
        elif args.command == "worker":
            asyncio.run(worker(store, args.config, args.once))
        elif args.command == "datasets":
            from folio.knowledge.ragflow import list_datasets

            print(
                json.dumps(
                    asyncio.run(list_datasets(args.mcp_url, name=args.name, page=args.page)),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "serve":
            import uvicorn

            from folio.api.app import create_app

            uvicorn.run(create_app(args.data_dir), host="127.0.0.1", port=args.port)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"folio: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
