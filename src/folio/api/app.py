"""Local backend API. A separate worker consumes persisted jobs."""

from pathlib import Path
from urllib.parse import urlsplit
from zipfile import BadZipFile

from docx.opc.exceptions import PackageNotFoundError
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from lxml.etree import XMLSyntaxError

from folio.api.artifacts import artifact_filename
from folio.api.localization import LocalizationMiddleware
from folio.i18n import localize_payload
from folio.model_settings import (
    ModelProbeInput,
    ModelSettingsInput,
    ModelSettingsStore,
    test_connection,
    test_decision_connection,
    test_tavily_connection,
)
from folio.models import JobInput
from folio.research.contracts import ResearchConfig
from folio.runtime.language import output_language, request_language
from folio.store import Store

WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
UI_BUILD_INSTRUCTIONS = (
    "前端尚未构建。请在项目根目录运行："
    "npm --prefix frontend ci && npm --prefix frontend run build"
)


def create_app(root: Path | str = ".folio") -> FastAPI:
    app = FastAPI(title="Folio", version="0.1.0")
    app.add_middleware(LocalizationMiddleware)
    store = Store(Path(root))
    app.state.store = store
    model_settings = ModelSettingsStore(store.root)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/settings/model"):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": [
                        {"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()
                    ]
                },
            )
        return await request_validation_exception_handler(request, exc)

    def check_origin(request: Request):
        origin = request.headers.get("origin")
        if origin and (
            urlsplit(origin).netloc != request.url.netloc
            or urlsplit(origin).scheme != request.url.scheme
        ):
            raise HTTPException(403, "请从当前工作台操作模型设置")

    @app.get("/settings/model")
    def get_model_settings():
        return model_settings.public()

    @app.put("/settings/model")
    def save_model_settings(data: ModelSettingsInput, request: Request):
        check_origin(request)
        try:
            return model_settings.save(data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/settings/model/test")
    @app.post("/settings/model/test/llm")
    async def test_model_settings(data: ModelProbeInput, request: Request):
        check_origin(request)
        try:
            config = model_settings.resolve(data, component="llm")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result = await test_connection(config)
        model_settings.record_probe(config, "llm", result["ok"])
        return result

    @app.post("/settings/model/test/decision")
    async def test_decision_settings(data: ModelProbeInput, request: Request):
        check_origin(request)
        try:
            config = model_settings.resolve(data, component="decision")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result = await test_decision_connection(config)
        model_settings.record_probe(config, "decision", result["ok"])
        return result

    @app.post("/settings/model/test/search")
    @app.post("/settings/model/test/tavily")
    async def test_tavily_settings(data: ModelProbeInput, request: Request):
        check_origin(request)
        try:
            config = model_settings.resolve(data, component="tavily")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result = await test_tavily_connection(config)
        model_settings.record_probe(config, "tavily", result["ok"])
        return result

    web = WEB_DIST

    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next):
        if request.url.path.startswith("/ui/") and not (web / "index.html").is_file():
            return JSONResponse(
                localize_payload({"detail": UI_BUILD_INSTRUCTIONS}, request_language(request)), status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        response = await call_next(request)
        if request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/ui", StaticFiles(directory=web, check_dir=False), name="ui")

    @app.get("/", include_in_schema=False)
    def workspace(request: Request):
        if not (web / "index.html").is_file():
            language = request_language(request)
            instruction = (
                "The frontend has not been built. Run from the project root: "
                "npm --prefix frontend ci && npm --prefix frontend run build"
                if language == "en" else UI_BUILD_INSTRUCTIONS
            )
            return HTMLResponse(
                f"<!doctype html><html lang='{language}'><meta charset='UTF-8'>"
                f"<title>Folio</title><h1>Folio</h1><p>{instruction}</p></html>",
                status_code=503, headers={"Cache-Control": "no-store"},
            )
        html = (web / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    def job_or_404(job_id: str) -> dict:
        try:
            return store.get(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/jobs", status_code=202)
    def create_job(job_input: JobInput, request: Request):
        try:
            if job_input.language is None:
                job_input.language = output_language(job_input.user_request, default=request_language(request))
            if job_input.ui_language is None:
                job_input.ui_language = request_language(request)
            if job_input.runtime == "llm" and "research" not in job_input.model_fields_set:
                job_input.research = ResearchConfig()
            job_id = store.create(job_input)
        except (ValueError, OSError, BadZipFile, PackageNotFoundError, XMLSyntaxError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"job_id": job_id, "status": "queued", "status_url": f"/jobs/{job_id}"}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str):
        return job_or_404(job_id)

    @app.get("/jobs/{job_id}/events")
    def events(job_id: str, after: int = Query(default=0, ge=0)):
        job_or_404(job_id)
        return store.events(job_id, after)

    @app.get("/jobs/{job_id}/sections")
    def sections(job_id: str):
        job_or_404(job_id)
        return [s.model_dump() for s in store.states(job_id)]

    @app.get("/jobs/{job_id}/work")
    def business_work(job_id: str):
        job = job_or_404(job_id)
        from folio.api.workspace import task_usage_summary

        return {
            "plan": job["work_plan"],
            "states": {k: v.model_dump() for k, v in store.work_states(job_id).items()},
            "task_usage": task_usage_summary(store, job_id),
        }

    @app.get("/jobs/{job_id}/artifact")
    def artifact(job_id: str):
        job = job_or_404(job_id)
        if not job["artifact"] or job["status"] not in {"published", "draft", "needs_confirmation"}:
            raise HTTPException(409, "No downloadable artifact yet")
        path = Path(job["artifact"])
        if not path.is_file():
            raise HTTPException(404, "Artifact is missing")
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=artifact_filename(job),
        )

    from folio.api.workspace import install_workspace

    install_workspace(app, store, check_origin)
    from folio.api.research import install_research_routes

    install_research_routes(app, store)
    return app
