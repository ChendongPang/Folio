"""Style examples stay out of facts and retain their reviewed source snapshots."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

from folio.api.app import create_app
from folio.api.workspace import FileSelection, material_context
from folio.models import JobInput
from folio.source_documents import _prepare_inputs
from folio.store import Store


def attachment(path, role, identifier="a"):
    return {"id": identifier * 32, "name": path.name, "path": str(path), "role": role}


def test_style_example_and_renamed_duplicate_never_enter_chat_facts(tmp_path):
    sample = tmp_path / "example.md"
    sample.write_text("# 老项目\n旧预算是 987654321 元。", encoding="utf-8")
    duplicate = tmp_path / "business.txt"
    duplicate.write_bytes(sample.read_bytes())
    facts = tmp_path / "facts.txt"
    facts.write_text("当前项目采用双人复核。", encoding="utf-8")
    context = material_context([
        attachment(duplicate, "source", "b"),
        attachment(sample, "style_reference"),
        attachment(facts, "source", "c"),
    ])
    encoded = json.dumps(context, ensure_ascii=False)
    assert "987654321" not in encoded and "老项目" not in encoded
    assert context[0]["excluded_as_nonfactual_duplicate"]
    assert "parsed" not in context[1]
    assert "当前项目采用双人复核" in encoded


def test_style_role_persists_for_non_docx_and_revokes_directory(tmp_path):
    app = create_app(tmp_path / "state")
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        other = client.post("/conversations").json()["id"]
        source = client.post(
            f"/conversations/{cid}/sources?filename=示例.md", content="# 示例\n正式的表达。"
        ).json()
        with app.state.store.connect() as conn:
            state = json.loads(conn.execute("SELECT data FROM web_conversations WHERE id=?", (cid,)).fetchone()[0])
            state["directory_review"] = {"revision": "old"}
            conn.execute("UPDATE web_conversations SET data=? WHERE id=?", (json.dumps(state), cid))
        url = f"/conversations/{cid}/sources/{source['id']}"
        saved = client.patch(url, json={"role": "style_reference"})
        assert saved.status_code == 200 and saved.json()["directory_invalidated"]
        assert saved.json()["files"][0]["role"] == "style_reference"
        assert client.get(f"/conversations/{cid}").json()["files"][0]["role"] == "style_reference"
        assert client.put(f"/conversations/{cid}/draft", json={"files": [{"id": source["id"]}]}).status_code == 200
        assert client.get(f"/conversations/{cid}").json()["files"][0]["role"] == "style_reference"
        assert client.patch(f"/conversations/{other}/sources/{source['id']}", json={"role": "style_reference"}).status_code == 404
        assert client.patch(url, json={"role": "template"}).status_code == 422
    assert "style_reference" in FileSelection.model_json_schema()["properties"]["role"]["enum"]


def test_planning_snapshots_style_separately_in_attachment_order(tmp_path):
    first = tmp_path / "z.md"
    second = tmp_path / "a.txt"
    facts = tmp_path / "facts.txt"
    first.write_text("示例金额 111111", encoding="utf-8")
    second.write_text("示例金额 222222", encoding="utf-8")
    facts.write_text("当前业务依据", encoding="utf-8")
    folder = tmp_path / "inputs"
    catalog = _prepare_inputs(folder, [
        attachment(first, "style_reference"), attachment(facts, "source", "b"),
        attachment(second, "style_reference", "c"),
    ])
    assert len(catalog) == 1 and catalog[0]["name"] == "facts.txt"
    assert (folder / "style-references" / "0" / first.name).read_bytes() == first.read_bytes()
    assert (folder / "style-references" / "1" / second.name).read_bytes() == second.read_bytes()
    assert len(list((folder / "sources").iterdir())) == 1
    assert "示例金额" not in json.dumps(catalog, ensure_ascii=False)


def test_planning_rejects_renamed_style_as_source_regardless_of_order(tmp_path):
    sample = tmp_path / "sample.md"
    sample.write_text("示例公司有 99999 位员工", encoding="utf-8")
    duplicate = tmp_path / "renamed.txt"
    duplicate.write_bytes(sample.read_bytes())
    with pytest.raises(ValueError, match="与风格示例内容相同"):
        _prepare_inputs(tmp_path / "inputs", [
            attachment(duplicate, "source", "b"), attachment(sample, "style_reference"),
        ])


def test_job_isolates_style_duplicates_from_knowledge_and_references(tmp_path):
    sample = tmp_path / "sample.docx"
    document = Document()
    document.add_paragraph("旧示例公司预算 888888 元")
    document.save(sample)
    references = tmp_path / "renamed.docx"
    references.write_bytes(sample.read_bytes())
    text_sample = tmp_path / "style.md"
    text_sample.write_text("# 写作示例\n先描述问题，再提出建议。", encoding="utf-8")
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "duplicate.docx").write_bytes(sample.read_bytes())
    (sources / "duplicate.txt").write_bytes(text_sample.read_bytes())
    (sources / "facts.txt").write_text("真实业务依据", encoding="utf-8")
    hashes = [sha256(path.read_bytes()).hexdigest() for path in (sample, text_sample)]
    profile = {"role": "style_only", "source_hashes": hashes, "documents": [{"source_hash": value} for value in hashes],
               "markdown": "# 风格说明\n正式、客观。"}
    store = Store(tmp_path / "state")
    job_id = store.create(JobInput(
        title="新文档", user_request="写需求说明", knowledge_path=str(sources),
        reference_paths=[str(references)], sample_paths=[str(sample), str(text_sample)],
        runtime="demo",
    ), writing_profile=profile)
    saved = store.get(job_id)["input"]
    assert saved["reference_paths"] == []
    assert [path.name for path in Path(saved["knowledge_path"]).iterdir()] == ["facts.txt"]
    assert len(saved["sample_paths"]) == 2
    assert Path(saved["sample_paths"][1]).read_bytes() == text_sample.read_bytes()
    frozen = store.root / "jobs" / job_id / "inputs" / "style"
    assert json.loads((frozen / "profile.json").read_text()) == profile
    assert (frozen / "style.md").read_text() == profile["markdown"]
    sample.write_bytes(b"changed original")
    assert Path(saved["sample_paths"][0]).read_bytes() == references.read_bytes()


def test_job_rejects_changed_style_profile_before_persisting_job(tmp_path):
    sample = tmp_path / "sample.md"
    sample.write_text("新示例", encoding="utf-8")
    store = Store(tmp_path / "state")
    sources = tmp_path / "sources"
    sources.mkdir()
    with pytest.raises(ValueError, match="style profile does not match the reference-file snapshot"):
        store.create(JobInput(title="文档", user_request="写作", sample_paths=[str(sample)], knowledge_path=str(sources)),
                     writing_profile={"source_hashes": ["stale"], "documents": []})
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert not list((store.root / "jobs").iterdir())


def test_style_markdown_download_uses_reviewed_revision_and_conversation(tmp_path):
    from folio.models import DocumentPlan

    app = create_app(tmp_path / "state")
    with TestClient(app) as client:
        cid = client.post("/conversations").json()["id"]
        other = client.post("/conversations").json()["id"]
        revision = "e" * 32
        folder = app.state.store.root / "directory-drafts" / revision
        (folder / "sources").mkdir(parents=True)
        (folder / "style").mkdir()
        (folder / "metadata.json").write_text(json.dumps({"conversation_id": cid}), encoding="utf-8")
        (folder / "document-plan.json").write_text(DocumentPlan(sections=[]).model_dump_json(), encoding="utf-8")
        (folder / "style" / "style.md").write_text("# 风格说明\n采用客观表达。", encoding="utf-8")
        url = f"/conversations/{cid}/directories/{revision}/style"
        response = client.get(url)
        assert response.status_code == 200 and "采用客观表达" in response.text
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["content-disposition"].startswith("inline")
        assert client.get(f"/conversations/{other}/directories/{revision}/style").status_code == 404
        assert client.get(f"/conversations/{cid}/directories/{'f' * 32}/style").status_code == 404
