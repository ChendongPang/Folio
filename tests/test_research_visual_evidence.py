"""Diagram relationships become evidence only after independent image review."""
from pathlib import Path

import pytest
from PIL import Image

from folio.models import Evidence
from folio.research.artifact_coverage import CoverageManager
from folio.research.artifacts import ArtifactError, derived_evidence_error
from folio.research.extraction_review import ExtractionReviewManager, evidence_for_reviewed_block
from folio.research.formats import SourceBlock, SourceDocument
from folio.research.workspace import WorkspaceManager


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from test_research_completeness import mixed_pdf

    monkeypatch.setenv('FOLIO_ARTIFACT_KEY_PATH', str(tmp_path / 'key'))
    sources = tmp_path / 'sources'
    sources.mkdir()
    mixed_pdf(sources / 'flow.pdf')
    workspace = WorkspaceManager(sources, tmp_path / 'task')
    Image.new('RGB', (100, 100), 'white').save(workspace.task_root / 'artifacts/original-page.png')
    return workspace


def record(workspace, *, text='Reject -> return to applicant; approve -> archive.', supported=True, complete=True):
    manager = ExtractionReviewManager(workspace.task_root, workspace)
    prepared = manager.prepare_visual('flow.pdf', 1, text, page_count=2)
    result = manager.record_visual(prepared, review_key='separate-visual-model', supported=supported,
                                   complete=complete, reason='Compared labels and arrow directions',
                                   image_paths=['artifacts/original-page.png'])
    return manager, result


def test_visual_relationships_have_signed_distinct_source_provenance(workspace):
    manager, result = record(workspace)
    evidence = Evidence.model_validate(result['evidence'][0])
    assert evidence.evidence_id.startswith('visual:')
    assert evidence.metadata['source_kind'] == 'visual'
    assert evidence.metadata['provenance'] == 'source'
    assert evidence.metadata['visual_extraction']['method'] == 'vision'
    assert 'extraction' not in evidence.metadata
    assert evidence.metadata['page'] == 1
    assert derived_evidence_error(evidence) is None
    assert manager.resolve_visual(evidence) == evidence
    assert workspace.resolve_evidence(evidence) == evidence
    restored = WorkspaceManager(workspace.source_root, workspace.task_root)
    assert restored.resolve_evidence(evidence) == evidence


def test_complete_visual_page_replaces_native_and_inaccurate_ocr_enumeration(workspace):
    from test_research_coverage_fragments import state

    workspace.register_extraction('flow.pdf', {'blocks': [{'page': 1, 'text': 'Incorrect OCR: reject -> archive'}]},
                                  execution_id='ocr', image_digest='sha256:ocr')
    _, result = record(workspace, text='Page 1 body. Reject returns to applicant; approve proceeds to archive.')
    evidence = Evidence.model_validate(result['evidence'][0])
    manager = CoverageManager(workspace.task_root, workspace)
    scope = manager.create('flow.pdf')['scope']
    assert len(scope['items']) == 2
    assert [item['text'] for item in scope['items']] == ['Page 2 body', evidence.text]
    item = next(item for item in scope['items'] if item['locator'] == evidence.locator)
    assert manager.covering_references(scope, item, [state([evidence])])
    # Exact slices preserve independent provenance and can jointly cover the page.
    document, _, _ = ExtractionReviewManager(workspace.task_root, workspace).verified_document('flow.pdf')
    block = next(block for block in document.blocks if block.locator == evidence.locator)
    split = [evidence_for_reviewed_block('flow.pdf', document, block, start, end)
             for start, end in [(0, 20), (20, len(block.text))]]
    assert all(derived_evidence_error(part) is None for part in split)
    assert manager.covering_references(scope, item, [state(split)])


def test_faithful_partial_visual_statement_is_evidence_but_cannot_claim_full_coverage(workspace):
    _, result = record(workspace, text='Reject returns to applicant.', complete=False)
    assert result['supported'] and not result['complete']
    evidence = Evidence.model_validate(result['evidence'][0])
    assert derived_evidence_error(evidence) is None
    with pytest.raises(ArtifactError, match='unread'):
        CoverageManager(workspace.task_root, workspace).create('flow.pdf')


def test_rejected_visual_statement_never_becomes_evidence(workspace):
    _, result = record(workspace, text='Invented rule', supported=False, complete=False)
    assert not result['evidence'] and result['review_id'] is None
    assert not list((workspace.task_root / '.research/extraction_reviews').glob('*.json'))


@pytest.mark.parametrize('change', ['text', 'page', 'method', 'image', 'source'])
def test_visual_evidence_tampering_invalidates_its_receipt(workspace, change):
    _, result = record(workspace)
    evidence = Evidence.model_validate(result['evidence'][0])
    if change == 'text':
        evidence.text = 'Reject proceeds to archive'
    elif change == 'page':
        evidence.metadata['page'] = 2
    elif change == 'method':
        evidence.metadata['visual_extraction']['method'] = 'ocr'
    elif change == 'image':
        (workspace.task_root / 'artifacts/original-page.png').write_bytes(b'changed')
    else:
        (workspace.source_root / 'flow.pdf').write_bytes(b'changed')
    assert derived_evidence_error(evidence)


def test_visual_evidence_can_be_shared_but_coverage_verification_stays_task_local(workspace):
    _, result = record(workspace)
    evidence = Evidence.model_validate(result['evidence'][0])
    other = WorkspaceManager(workspace.source_root, workspace.task_root.with_name('other-task'))
    assert ExtractionReviewManager(other.task_root, other).resolve_visual(evidence) == evidence
    with pytest.raises(ArtifactError):
        CoverageManager(other.task_root, other).create('flow.pdf')


def test_visual_slice_cannot_invent_a_valid_range(workspace):
    _, result = record(workspace)
    evidence = Evidence.model_validate(result['evidence'][0])
    import json

    receipt = json.loads(Path(evidence.metadata['extraction_review']['manifest_path']).read_bytes())
    raw = receipt['registration']['pages'][0]['blocks'][0]
    block = SourceBlock(**raw)
    block.metadata['extraction_review'] = evidence.metadata['extraction_review']
    document = SourceDocument(evidence.source_version, {'format': 'pdf'})
    quote = evidence_for_reviewed_block('flow.pdf', document, block, 0, 6)
    assert derived_evidence_error(quote) is None
    quote.metadata['characters_end'] += 1
    assert derived_evidence_error(quote)


def test_visual_page_count_is_bound_to_native_source(workspace):
    manager = ExtractionReviewManager(workspace.task_root, workspace)
    with pytest.raises(ArtifactError, match='page'):
        manager.prepare_visual('flow.pdf', 3, 'invented page', page_count=3)


@pytest.mark.parametrize('supported,complete', [(True, True), (True, False), (False, False)])
async def test_visual_extract_tool_requires_independent_original_image_verdict(tmp_path, monkeypatch, supported, complete):
    from types import SimpleNamespace

    from PIL import ImageDraw

    from folio.orchestration.context import evidence_view
    from folio.research.contracts import ResearchConfig
    from folio.research.visual_extraction import VisualExtractionReview
    from folio.research.workflow import ResearchTools

    monkeypatch.setenv('FOLIO_ARTIFACT_KEY_PATH', str(tmp_path / 'key'))
    sources = tmp_path / 'sources'
    sources.mkdir()
    image = Image.new('RGB', (500, 300), 'white')
    ImageDraw.Draw(image).text((20, 20), 'Reject -> Applicant\nApprove -> Archive', fill='black')
    image.save(sources / 'flow.png')
    workspace = WorkspaceManager(sources, tmp_path / 'task')
    text = 'Reject returns to applicant; approve proceeds to archive.'
    calls = []

    async def model(action, context, schema):
        calls.append((action, context, schema))
        return schema.model_validate({'supported': supported, 'complete': complete, 'source_matches': True,
                                      'reason': 'Checked arrow directions against the original image'})

    tools = ResearchTools.__new__(ResearchTools)
    tools.workspace = workspace
    tools.sandbox = None
    tools.config = ResearchConfig()
    tools.engine = SimpleNamespace(
        runtime=SimpleNamespace(config={'supports_image_input': True}),
        request_context=lambda _action, context: context,
        request_schema=lambda _action, _context, schema: schema,
        request_cache_key=lambda *_args: 'visual-independent-call',
    )
    tools.model = model
    result = await tools.save_artifact({
        'operation': 'visual.extract', 'path': 'flow.png', 'page': 1, 'text': text,
        'supported': True, 'complete': True, 'verified': True,
    })
    assert len(calls) == 1
    action, context, schema = calls[0]
    assert action == 'verify_visual_extraction' and schema is VisualExtractionReview
    assert context['proposed_text'] == text
    assert context['page_images'][0]['source_id'] == 'flow.png'
    assert Path(context['page_images'][0]['path']).is_file()
    assert result['supported'] is supported and result['complete'] is complete
    if not supported:
        assert not result['evidence'] and result['review_id'] is None
        assert not list((workspace.task_root / '.research/extraction_reviews').glob('*.json'))
        return
    evidence = Evidence.model_validate(result['evidence'][0])
    assert evidence.text == text and derived_evidence_error(evidence) is None
    displayed = evidence_view(evidence.model_dump())
    assert displayed['metadata']['source_kind'] == 'visual'
    assert displayed['metadata']['extraction_review']['status'] == 'verified'
    assert displayed['metadata']['requires_extraction_review'] is False
    coverage = CoverageManager(workspace.task_root, workspace)
    if complete:
        scope = coverage.create('flow.png')['scope']
        assert [item['text'] for item in scope['items']] == [text]
    else:
        with pytest.raises(ArtifactError):
            coverage.create('flow.png')
    # Saved shared evidence restores in another business task, without granting
    # that task an independent complete-source coverage record.
    other = WorkspaceManager(workspace.source_root, workspace.task_root.with_name('other-task'))
    engine = SimpleNamespace(request=SimpleNamespace(research=ResearchConfig()))
    restored = ResearchTools(engine, SimpleNamespace(task_id='other'), SimpleNamespace(evidence=[]),
                             other, SimpleNamespace(verify_execution=lambda *_: None), None,
                             {'evidence': [evidence.model_dump()]})
    assert restored.evidence[evidence.evidence_id].text == text
    with pytest.raises(ArtifactError):
        restored.coverage.create('flow.png')


def test_native_pdf_flowchart_labels_do_not_hide_unread_vector_relationships(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    from test_source_structure import pdf

    from folio.research.formats import read_source

    path = tmp_path / 'flow.pdf'
    pdf(path, pages=1)
    writer = PdfWriter(clone_from=PdfReader(path))
    page = writer.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(page.get_contents().get_data() + b' 20 500 m 200 500 l 190 510 l 200 500 l 190 490 l S')
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)
    document = read_source(path)
    assert document.blocks[0].text.strip() == 'Page 1 body'
    assert document.metadata['unread_visual_pages'] == [1]
    assert not document.metadata['complete']


def test_partial_and_other_page_reviews_do_not_change_an_existing_scope_identity(workspace):
    record(workspace, complete=True)
    coverage = CoverageManager(workspace.task_root, workspace)
    whole = coverage.create('flow.pdf')
    page_one = coverage.create('flow.pdf', 'page 1')
    record(workspace, text='Reject returns to applicant.', complete=False)
    assert coverage.create('flow.pdf')['coverage_id'] == whole['coverage_id']
    assert coverage.create('flow.pdf', 'page 1')['coverage_id'] == page_one['coverage_id']
    manager = ExtractionReviewManager(workspace.task_root, workspace)
    prepared = manager.prepare_visual('flow.pdf', 2, 'Page 2 body', page_count=2)
    manager.record_visual(prepared, review_key='page-2', supported=True, complete=True,
                          reason='Second page content matches', image_paths=['artifacts/original-page.png'])
    assert coverage.create('flow.pdf', 'page 1')['coverage_id'] == page_one['coverage_id']


def test_supported_partial_visual_does_not_invalidate_complete_native_scope(tmp_path, monkeypatch):
    from test_source_structure import pdf

    monkeypatch.setenv('FOLIO_ARTIFACT_KEY_PATH', str(tmp_path / 'key'))
    sources = tmp_path / 'sources'
    sources.mkdir()
    pdf(sources / 'text.pdf', pages=1)
    workspace = WorkspaceManager(sources, tmp_path / 'task')
    Image.new('RGB', (100, 100), 'white').save(workspace.task_root / 'artifacts/page.png')
    coverage = CoverageManager(workspace.task_root, workspace)
    before = coverage.create('text.pdf')
    manager = ExtractionReviewManager(workspace.task_root, workspace)
    prepared = manager.prepare_visual('text.pdf', 1, 'Page 1', page_count=1)
    result = manager.record_visual(prepared, review_key='partial-page', supported=True, complete=False,
                                   reason='This exact partial quote is visible', image_paths=['artifacts/page.png'])
    assert derived_evidence_error(Evidence.model_validate(result['evidence'][0])) is None
    assert coverage.create('text.pdf')['coverage_id'] == before['coverage_id']
    document, _, references = manager.verified_document('text.pdf')
    assert document.metadata['complete']
    assert not document.metadata['unread_visual_pages']
    assert references == []


def test_single_pixel_pdf_image_can_encode_meaning_when_stretched(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, NumberObject
    from test_research_completeness import mixed_pdf

    from folio.research.formats import read_source

    path = tmp_path / 'colour-status.pdf'
    mixed_pdf(path)
    writer = PdfWriter(clone_from=PdfReader(path))
    pixel = writer.pages[0]['/Resources']['/XObject']['/Im1'].get_object()
    pixel[NameObject('/Width')] = NumberObject(1)
    pixel[NameObject('/Height')] = NumberObject(1)
    pixel.set_data(b'\x00\xff\x00')
    writer.write(path)
    document = read_source(path)
    assert document.metadata['unread_visual_pages'] == [1]
    assert not document.metadata['complete']
