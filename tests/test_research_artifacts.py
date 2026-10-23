"""Computed research results cannot bypass source and publication checks."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from folio.models import (
    Block,
    Claim,
    Evidence,
    Extraction,
    Section,
    SectionContent,
    SectionState,
    Verdict,
)
from folio.orchestration.local_checks import hard_findings
from folio.orchestration.state import registry
from folio.research.artifact_coverage import CoverageManager
from folio.research.artifacts import ArtifactError, ArtifactManager, derived_evidence_error
from folio.research.workspace import WorkspaceManager


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def research(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLIO_ARTIFACT_KEY_PATH", str(tmp_path / "host" / "signing.key"))
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "fields.txt").write_text("id：必填\nname：选填\nid：必填", encoding="utf-8")
    task_root = tmp_path / "task-a"
    workspace = WorkspaceManager(sources, task_root)
    script = task_root / "scripts" / "count.py"
    script.write_text("print(len({'id'}))\n", encoding="utf-8")
    output = task_root / "artifacts" / "run1" / "count.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"required_unique_fields": 1}', encoding="utf-8")
    receipt = {
        "execution_id": "run1",
        "status": "completed",
        "exit_code": 0,
        "image_digest": "sha256:" + "1" * 64,
        "profile": "python",
        "dependencies": [{"name": "pandas", "version": "2.2.3", "sha256": "a" * 64}],
        "script_path": "scripts/count.py",
        "script_sha256": sha(script),
        "output_files": [{"path": "artifacts/run1/count.json", "sha256": sha(output), "size": output.stat().st_size}],
        "source_manifest": [{"path": "fields.txt", "sha256": sha(sources / "fields.txt")}],
    }
    record = task_root / ".research" / "executions" / "run1.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(receipt), encoding="utf-8")
    manager = ArtifactManager(task_root, workspace)
    evidence = [Evidence.model_validate(item) for item in workspace.read("fields.txt")["evidence"]]
    return SimpleNamespace(
        manager=manager, workspace=workspace, source_root=sources, task_root=task_root,
        receipt=receipt, record=record, script=script, output=output, evidence=evidence,
    )


def register(research, **overrides):
    args = {
        "execution_id": "run1", "output_path": "artifacts/run1/count.json",
        "sources": research.evidence, "methodology": "仅统计必填字段，并按字段名去重",
        "description": "共有 1 个不同的必填字段",
    }
    return research.manager.register(**(args | overrides))


def artifact_id(evidence):
    return evidence.metadata["derivation"]["artifact_id"]


def verify(research, evidence):
    return research.manager.record_validation(artifact_id(evidence), True, "输入、去重口径和结果一致", "review-cache-1")


def state_for(evidence):
    return SectionState(
        section_id="fields",
        content=SectionContent(blocks=[Block(block_id="count", text="共有 1 个不同的必填字段", evidence_ids=[evidence.evidence_id])]),
        evidence=[evidence],
        extraction=Extraction(summary="字段数量", claims=[Claim(
            claim_id="count", block_ids=["count"], statement="共有 1 个不同的必填字段",
            subject="必填字段", predicate="数量", value="1", evidence_ids=[evidence.evidence_id],
        )]),
        claim_verdicts=[Verdict(claim_id="count", supported=True, reason="核对结果")],
        validation_completed=True,
    )


def test_registration_is_not_validation_and_restart_preserves_review(research):
    evidence = register(research)
    assert "has not passed" in derived_evidence_error(evidence)
    context = research.manager.verification_context(artifact_id(evidence))
    assert len(context["sources"]) == 3
    assert context["methodology"] == "仅统计必填字段，并按字段名去重"
    assert context["script"] == research.script.read_text()
    assert json.loads(context["result"])["required_unique_fields"] == 1
    reviewed = verify(research, evidence)
    assert derived_evidence_error(reviewed) is None
    recovered = ArtifactManager(research.task_root, research.workspace).load(artifact_id(evidence))
    assert recovered == reviewed
    assert register(research) == reviewed


@pytest.mark.parametrize("change", ["source", "script", "output", "execution"])
def test_changes_in_any_link_revoke_evidence_and_shared_facts(research, change):
    reviewed = verify(research, register(research))
    state = state_for(reviewed)
    assert registry([state])[0]
    target = {
        "source": research.source_root / "fields.txt", "script": research.script,
        "output": research.output, "execution": research.record,
    }[change]
    target.write_text(target.read_text() + "\nmodified", encoding="utf-8")
    assert derived_evidence_error(reviewed)
    assert not registry([state])[0]
    assert not research.manager.validate_manifest(artifact_id(reviewed))["valid"]


def test_model_supplied_verified_metadata_and_receipt_tampering_do_not_authorize(research):
    evidence = register(research)
    evidence.metadata["derivation"]["status"] = "verified"
    assert derived_evidence_error(evidence)
    manifest = Path(evidence.metadata["derivation"]["manifest_path"])
    forged = json.loads(manifest.read_text())
    forged["validation"]["status"] = "verified"
    manifest.write_text(json.dumps(forged))
    assert 'signature' in derived_evidence_error(evidence)


def test_verified_receipt_is_bound_to_exact_evidence_text(research):
    evidence = verify(research, register(research))
    evidence.text = "共有 999 个不同的必填字段"
    assert "does not match" in derived_evidence_error(evidence)


@pytest.mark.parametrize("field,value", [("locator", "line 900"), ("text", "id：选填"), ("source_id", "../secret.txt"), ("source_version", "f" * 64)])
def test_fabricated_source_reference_is_rejected(research, field, value):
    evidence = research.evidence[0].model_copy(update={field: value})
    with pytest.raises(ArtifactError):
        register(research, sources=[evidence])


def test_slice_sources_are_checked_against_actual_characters(research):
    from folio.research.formats import read_source
    from folio.research.workspace import evidence_for_block

    document = read_source(research.source_root / "fields.txt")
    fragment = evidence_for_block("fields.txt", document, document.blocks[0], 0, 2)
    assert register(research, sources=[fragment])
    with pytest.raises(ArtifactError, match='cited location or text'):
        register(research, sources=[fragment.model_copy(update={"text": "xx"})])


def test_failed_execution_and_unregistered_output_are_rejected(research):
    with pytest.raises(ArtifactError, match='does not belong'):
        register(research, output_path="artifacts/run1/unknown.json")
    research.receipt["exit_code"] = 1
    research.record.write_text(json.dumps(research.receipt))
    with pytest.raises(ArtifactError, match='fully successful'):
        register(research)


def test_symlink_cannot_substitute_an_output_or_manifest(research):
    evidence = verify(research, register(research))
    original = research.output.read_text()
    target = research.task_root.parent / "outside.json"
    target.write_text(original)
    research.output.unlink()
    research.output.symlink_to(target)
    assert "symbolic links" in derived_evidence_error(evidence)


def test_reuse_requires_review_and_exact_source_and_environment(research, tmp_path):
    evidence = register(research)
    manifest = evidence.metadata["derivation"]["manifest_path"]
    consumer = ArtifactManager(tmp_path / "task-b", research.workspace)
    environment = research.manager.verification_context(artifact_id(evidence))["environment"]
    versions = {item["path"]: item["sha256"] for item in research.receipt["source_manifest"]}
    assert consumer.reuse(manifest, source_versions=versions, environment=environment) is None
    reviewed = verify(research, evidence)
    assert consumer.reuse(manifest, source_versions=versions, environment=environment) == reviewed
    assert consumer.reuse(manifest, source_versions={}, environment=environment) is None
    assert consumer.reuse(manifest, source_versions=versions, environment=environment | {"image_digest": "sha256:other"}) is None


def test_pending_derived_evidence_is_a_hard_publication_gate(research):
    evidence = register(research)
    state = state_for(evidence)
    section = Section(section_id="fields", node_id="fields", title="字段", level=1,
                      display_number="1", instruction="说明字段规则")
    findings = hard_findings(section, state.content, state.evidence, state.extraction)
    assert any(issue.key == "derived-evidence:" + evidence.evidence_id for issue in findings)
    assert not registry([state])[0]
    verified = verify(research, evidence)
    assert registry([state_for(verified)])[0]


@pytest.mark.parametrize("metadata,evidence_id", [
    ({"requires_extraction_review": True}, "ocr-evidence"),
    ({"requires_extraction_review": False}, "extracted:actual-id"),
    ({"source_kind": "ocr"}, "ocr-evidence"),
    ({"extraction": {"method": "ocr"}}, "ocr-evidence"),
])
def test_unverified_ocr_is_readable_but_not_shared_or_publishable(metadata, evidence_id):
    evidence = Evidence(evidence_id=evidence_id, source_id="scan.pdf", source_version="version", locator="page 1", text="金额精度为两位", metadata=metadata)
    assert "OCR" in derived_evidence_error(evidence)
    assert not registry([state_for(evidence)])[0]


def test_coverage_enumerates_complete_scope_and_checks_actual_body_references(research):
    manager = CoverageManager(research.task_root, research.workspace)
    manifest = manager.create("fields.txt")
    coverage_id = manifest["coverage_id"]
    assert len(manifest["scope"]["items"]) == 3
    assert not manager.report(coverage_id)["complete"]
    state = state_for(research.evidence[0])
    for item in manifest["scope"]["items"]:
        manager.record(coverage_id, item["item_id"], "covered", references=[{"section_id": "fields", "block_id": "count"}])
    partial = manager.report(coverage_id, [state])
    assert partial["covered"] == 1
    assert len(partial["missing"]) == 2
    state.evidence = research.evidence
    state.content.blocks[0].evidence_ids = [item.evidence_id for item in research.evidence]
    assert manager.report(coverage_id, [state])["complete"]
    state.content.blocks[0].evidence_ids = []
    assert not manager.report(coverage_id, [state])["complete"]


def test_coverage_exclusions_need_independent_review_and_source_changes_invalidate(research):
    manager = CoverageManager(research.task_root, research.workspace)
    manifest = manager.create("fields.txt", "line 1")
    coverage_id, item = manifest["coverage_id"], manifest["scope"]["items"][0]
    manager.record(coverage_id, item["item_id"], "not_applicable", reason="标题行")
    assert not manager.report(coverage_id)["complete"]
    manager.review_exclusion(coverage_id, item["item_id"], review_key="review1", reason="已核对确属标题")
    assert manager.report(coverage_id)["complete"]
    (research.source_root / "fields.txt").write_text("other")
    with pytest.raises(ArtifactError, match='source version.*changed'):
        manager.report(coverage_id)


def add_intermediate_execution(research):
    script = research.task_root / "scripts" / "prepare.py"
    script.write_text("# produce a row count for downstream analysis\n", encoding="utf-8")
    output = research.task_root / "artifacts" / "prepare" / "rows.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"rows": 3, "unique_required": 1}', encoding="utf-8")
    artifact = {"path": "artifacts/prepare/rows.json", "sha256": sha(output), "size": output.stat().st_size}
    receipt = research.receipt | {
        "execution_id": "prepare", "profile": "data",
        "script_path": "scripts/prepare.py", "script_sha256": sha(script),
        "output_files": [artifact], "artifacts": [artifact],
    }
    record_path = research.record.with_name("prepare.json")
    record_path.write_text(json.dumps(receipt), encoding="utf-8")
    research.receipt["input_artifacts"] = [artifact | {"execution_id": "prepare"}]
    research.record.write_text(json.dumps(research.receipt), encoding="utf-8")
    return script, output, record_path


@pytest.mark.parametrize("change", ["script", "output", "receipt"])
def test_intermediate_computation_is_reviewed_and_mutation_revokes_downstream(research, change):
    script, output, receipt = add_intermediate_execution(research)
    evidence = register(research)
    context = research.manager.verification_context(artifact_id(evidence))
    assert len(context["intermediate_executions"]) == 1
    previous = context["intermediate_executions"][0]
    assert previous["script_text"] == script.read_text()
    assert json.loads(previous["outputs"][0]["text"])["rows"] == 3
    evidence = verify(research, evidence)
    assert derived_evidence_error(evidence) is None
    target = {"script": script, "output": output, "receipt": receipt}[change]
    target.write_text(target.read_text() + "\nchanged")
    assert 'preceding calculation' in derived_evidence_error(evidence)
    assert not registry([state_for(evidence)])[0]


def test_intermediate_environment_must_match_on_cross_task_reuse(research):
    add_intermediate_execution(research)
    evidence = verify(research, register(research))
    context = research.manager.verification_context(artifact_id(evidence))
    versions = {item["path"]: item["sha256"] for item in research.receipt["source_manifest"]}
    kwargs = {"source_versions": versions, "environment": context["environment"]}
    path = evidence.metadata["derivation"]["manifest_path"]
    assert research.manager.reuse(path, **kwargs) is None
    previous = context["intermediate_executions"][0]["environment"]
    assert research.manager.reuse(path, **kwargs, intermediate_environments={"data": previous}) == evidence
    assert research.manager.reuse(path, **kwargs, intermediate_environments={"data": previous | {"image_digest": "other"}}) is None


def test_invented_intermediate_input_is_rejected(research):
    add_intermediate_execution(research)
    research.receipt["input_artifacts"][0]["sha256"] = "f" * 64
    research.record.write_text(json.dumps(research.receipt))
    with pytest.raises(ArtifactError, match='preceding calculation artifact'):
        register(research)
