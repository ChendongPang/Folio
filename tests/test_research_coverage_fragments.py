"""Coverage joins authentic source slices without allowing gaps or forged spans."""

import pytest

from folio.models import Block, Evidence, Extraction, SectionContent, SectionState
from folio.research.artifact_coverage import CoverageManager
from folio.research.formats import read_source
from folio.research.workspace import WorkspaceManager, evidence_for_block


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLIO_ARTIFACT_KEY_PATH", str(tmp_path / "host" / "key"))
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "long.txt").write_text("甲乙丙丁" * 1250, encoding="utf-8")
    workspace = WorkspaceManager(sources, tmp_path / "task")
    manager = CoverageManager(workspace.task_root, workspace)
    manifest = manager.create("long.txt")
    evidence, offset, character_offset = [], 0, 0
    while offset is not None:
        page = workspace.read(
            "long.txt", offset=offset, character_offset=character_offset, max_chars=1700,
        )
        assert page["total"] == 1
        evidence.extend(Evidence.model_validate(item) for item in page["evidence"])
        offset, character_offset = page["next_offset"], page["next_character_offset"]
    return workspace, manager, manifest, evidence


def state(evidence, section_id="s1", *, proposal=False):
    return SectionState(
        section_id=section_id,
        content=SectionContent(
            blocks=[
                Block(
                    block_id=f"body{index}",
                    text=item.text,
                    evidence_ids=[item.evidence_id],
                    origin="model_proposal" if proposal else "evidence",
                )
                for index, item in enumerate(evidence)
            ]
        ),
        extraction=Extraction(summary="源段落分片"),
        evidence=evidence,
    )


def test_all_slices_across_actual_body_blocks_complete_coverage(scope):
    _, manager, manifest, evidence = scope
    assert [len(item.text) for item in evidence] == [1700, 1700, 1600]
    states = [state(evidence[:2]), state(evidence[2:], "s2")]
    item = manifest["scope"]["items"][0]
    references = manager.covering_references(manifest["scope"], item, states)
    assert len(references) == 3
    manager.record(manifest["coverage_id"], item["item_id"], "covered", references=references)
    assert manager.report(manifest["coverage_id"], states)["complete"]


@pytest.mark.parametrize("selected", [[0], [1, 2], [0, 2], [0, 1]])
def test_partial_intervals_never_cover_a_complete_source_item(scope, selected):
    _, manager, manifest, evidence = scope
    states = [state([evidence[index] for index in selected])]
    item = manifest["scope"]["items"][0]
    assert not manager.covering_references(manifest["scope"], item, states)
    references = [
        {"section_id": "s1", "block_id": f"body{index}"} for index in range(len(selected))
    ]
    manager.record(manifest["coverage_id"], item["item_id"], "covered", references=references)
    assert not manager.report(manifest["coverage_id"], states)["complete"]


def test_overlapping_authentic_slices_cover_without_double_counting(scope):
    workspace, manager, manifest, _ = scope
    document = read_source(workspace.source_root / "long.txt")
    evidence = [
        evidence_for_block("long.txt", document, document.blocks[0], start, end)
        for start, end in [(0, 2000), (1500, 3500), (3000, 5000)]
    ]
    assert manager.covering_references(
        manifest["scope"], manifest["scope"]["items"][0], [state(evidence)]
    )


@pytest.mark.parametrize("tamper", ["text", "id", "offset", "boolean_offset", "locator"])
def test_forged_fragment_cannot_fill_a_coverage_gap(scope, tamper):
    _, manager, manifest, original = scope
    evidence = [item.model_copy(deep=True) for item in original]
    if tamper == "text":
        evidence[1].text = "错误" * 1000
    elif tamper == "id":
        evidence[1].evidence_id = "invented"
    elif tamper == "offset":
        evidence[1].metadata["characters_end"] = 5000
    elif tamper == "boolean_offset":
        evidence[1].metadata["characters_start"] = True
    else:
        evidence[1].locator = evidence[0].locator
    assert not manager.covering_references(
        manifest["scope"], manifest["scope"]["items"][0], [state(evidence)]
    )


def test_report_only_accepts_registered_actual_anchors(scope):
    _, manager, manifest, evidence = scope
    current = state(evidence)
    item = manifest["scope"]["items"][0]
    manager.record(
        manifest["coverage_id"],
        item["item_id"],
        "covered",
        references=[{"section_id": "s1", "block_id": "body0"}],
    )
    assert not manager.report(manifest["coverage_id"], [current])["complete"]
    refs = manager.covering_references(manifest["scope"], item, [current])
    manager.record(manifest["coverage_id"], item["item_id"], "covered", references=refs)
    current.content.blocks[1].block_id = "modified"
    assert not manager.report(manifest["coverage_id"], [current])["complete"]


def test_proposal_cannot_close_an_original_source_interval(scope):
    _, manager, manifest, evidence = scope
    states = [state(evidence[:2]), state(evidence[2:], "s2", proposal=True)]
    assert not manager.covering_references(manifest["scope"], manifest["scope"]["items"][0], states)
