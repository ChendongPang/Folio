"""The business tool requests an independent visual check, never self-approves."""
from types import SimpleNamespace

import pytest
from docx import Document
from PIL import Image, ImageDraw

from folio.models import Evidence
from folio.research.artifacts import derived_evidence_error
from folio.research.contracts import ResearchConfig
from folio.research.extraction_review import ExtractionReview
from folio.research.workflow import ResearchTools
from folio.research.workspace import WorkspaceManager


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv('FOLIO_ARTIFACT_KEY_PATH', str(tmp_path / 'host-key'))
    sources = tmp_path / 'sources'
    sources.mkdir()
    image = Image.new('RGB', (500, 200), 'white')
    ImageDraw.Draw(image).text((20, 20), 'Approval limit = 1000', fill='black')
    image.save(sources / 'scan.png')
    return WorkspaceManager(sources, tmp_path / 'task')


def tools_for(workspace, *, text='Approval limit = 1000', approved=True, vision=True, blank=False,
              read_max_chars=None):
    if text is not None:
        workspace.register_extraction('scan.png', {'blocks': [{'page': 1, 'text': text}]},
                                      execution_id='ocr', image_digest='sha256:converter')
    calls = []

    async def model(action, context, schema):
        calls.append((action, context, schema))
        return schema.model_validate({
            'approved_pages': [1] if approved else [], 'reason': 'Checked original image',
            'pages': [{'page': 1, 'source_matches': True, 'complete': approved,
                       'blank_or_decorative': blank, 'reason': 'All content represented' if approved else 'Missing content'}],
        })

    tools = ResearchTools.__new__(ResearchTools)
    tools.workspace = workspace
    tools.sandbox = None
    tools.config = ResearchConfig(max_artifact_review_chars=80000)
    tools.engine = SimpleNamespace(
        runtime=SimpleNamespace(config={'supports_image_input': vision}),
        request_context=lambda _task, context: context,
        request_schema=lambda _task, _context, schema: schema,
        request_cache_key=lambda *_args: 'independent-review-' + str(len(calls)),
    )
    tools.evidence, offset, character_offset = {}, 0, 0
    while offset is not None:
        page = workspace.read(
            'scan.png', offset=offset, character_offset=character_offset, max_chars=read_max_chars,
        )
        tools.evidence.update({item['evidence_id']: Evidence.model_validate(item)
                               for item in page['evidence']})
        offset, character_offset = page['next_offset'], page['next_character_offset']
    tools.model = model
    return tools, calls


async def test_verify_tool_uses_separate_image_review_and_refreshes_every_slice(workspace):
    tools, calls = tools_for(workspace, text='Approval limit = 1000. ' * 230, read_max_chars=1800)
    before = dict(tools.evidence)
    assert len(before) == 3
    assert ''.join(item.text for item in before.values()) == 'Approval limit = 1000. ' * 230
    result = await tools.save_artifact({'operation': 'extraction.verify', 'path': 'scan.png', 'pages': [1]})
    assert len(calls) == 1
    action, context, schema = calls[0]
    assert action == 'verify_extraction' and schema is ExtractionReview
    assert context['page_images'][0]['source_id'] == 'scan.png'
    assert context['page_images'][0]['page'] == 1
    assert context['pages'][0]['blocks'][0]['text'] == 'Approval limit = 1000. ' * 230
    assert result['approved_pages'] == [1] and result['review_id']
    assert len(result['evidence']) == 1  # Full block has a different ID than slices.
    assert set(tools.evidence) == set(before)
    for evidence_id, current in tools.evidence.items():
        assert current.text == before[evidence_id].text
        assert derived_evidence_error(before[evidence_id])
        assert derived_evidence_error(current) is None
    # A second independent call can refresh previously verified slices too.
    await tools.save_artifact({'operation': 'extraction.verify', 'path': 'scan.png', 'pages': [1]})
    assert len(calls) == 2
    assert all(derived_evidence_error(item) is None for item in tools.evidence.values())


async def test_failed_visual_review_never_issues_a_receipt(workspace):
    tools, calls = tools_for(workspace, approved=False)
    result = await tools.save_artifact({'operation': 'extraction.verify', 'path': 'scan.png', 'pages': [1],
                                      'approved_pages': [1], 'verified': True})
    assert calls[0][0] == 'verify_extraction'
    assert result['review_id'] is None and not result['approved_pages']
    assert not list((workspace.task_root / '.research/extraction_reviews').glob('*.json'))
    assert all(derived_evidence_error(item) for item in tools.evidence.values())


async def test_text_only_model_cannot_claim_visual_verification(workspace):
    tools, calls = tools_for(workspace, vision=False)
    with pytest.raises(ValueError, match='image input'):
        await tools.save_artifact({'operation': 'extraction.verify', 'path': 'scan.png', 'pages': [1]})
    assert not calls
    assert not list((workspace.task_root / '.research/extraction_reviews').glob('*.json'))


async def test_empty_extraction_requires_explicit_blank_page_verdict(workspace):
    tools, calls = tools_for(workspace, text=None, approved=True, blank=False)
    with pytest.raises(ValueError, match='extraction is empty'):
        await tools.save_artifact({'operation': 'extraction.verify', 'path': 'scan.png', 'pages': [1]})
    assert len(calls) == 1
    assert not list((workspace.task_root / '.research/extraction_reviews').glob('*.json'))


def office_workspace(tmp_path):
    sources = tmp_path / 'office'
    sources.mkdir()
    word = Document()
    word.add_paragraph('Original title')
    word.save(sources / 'rules.docx')
    return WorkspaceManager(sources, tmp_path / 'office-task')


def test_office_partial_ocr_runs_accumulate_readable_pages(tmp_path):
    workspace = office_workspace(tmp_path)
    for page in [1, 2]:
        result = workspace.register_extraction('rules.docx', {
            'operation': 'office_ocr', 'page_count': 2, 'selected_pages': [page],
            'blocks': [{'page': page, 'text': f'Page {page} content'}],
        }, execution_id=f'run-{page}', image_digest='sha256:office')
        assert result['complete'] is (page == 2)
    assert workspace.inspect('rules.docx')['metadata']['unread_pages'] == []


def test_replacing_ocr_execution_does_not_retain_removed_page_readability(tmp_path):
    workspace = office_workspace(tmp_path)
    result = workspace.register_extraction('rules.docx', {
        'operation': 'office_ocr', 'page_count': 2,
        'blocks': [{'page': page, 'text': f'Page {page} content'} for page in [1, 2]],
    }, execution_id='same-run', image_digest='sha256:office')
    assert result['complete']
    result = workspace.register_extraction('rules.docx', {
        'operation': 'office_ocr', 'page_count': 2, 'selected_pages': [1],
        'blocks': [{'page': 1, 'text': 'Page 1 content'}],
    }, execution_id='same-run', image_digest='sha256:office')
    assert not result['complete']
    assert workspace.inspect('rules.docx')['metadata']['unread_pages'] == [2]


async def test_computed_evidence_rechecks_the_ocr_verification_receipt(workspace):
    import hashlib
    import json
    from pathlib import Path

    from folio.research.artifacts import ArtifactManager

    tools, _ = tools_for(workspace)
    await tools.save_artifact({'operation': 'extraction.verify', 'path': 'scan.png', 'pages': [1]})
    source = next(iter(tools.evidence.values()))
    script = workspace.task_root / 'scripts/calculation.py'
    script.write_text('print(1000)')
    output = workspace.task_root / 'artifacts/calc/value.txt'
    output.parent.mkdir()
    output.write_text('1000')
    receipt = workspace.task_root / '.research/executions/calc.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({
        'execution_id': 'calc', 'status': 'completed', 'exit_code': 0,
        'image_digest': 'sha256:python', 'profile': 'base', 'dependencies': [],
        'script_path': 'scripts/calculation.py', 'script_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
        'output_files': [{'path': 'artifacts/calc/value.txt', 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}],
        'source_manifest': [{'path': 'scan.png', 'sha256': source.source_version}],
    }))
    manager = ArtifactManager(workspace.task_root, workspace)
    derived = manager.register('calc', 'artifacts/calc/value.txt', [source], 'Read the approved threshold', 'Threshold 1000')
    derived = manager.record_validation(derived.metadata['derivation']['artifact_id'], True,
                                         'Original input and calculation agree', 'calculation-review')
    assert derived_evidence_error(derived) is None
    visual_review = json.loads(Path(source.metadata['extraction_review']['manifest_path']).read_bytes())
    image_path = workspace.task_root / visual_review['registration']['images'][0]['path']
    image_path.write_bytes(b'changed original render')
    assert 'Calculation input verification is no longer valid' in derived_evidence_error(derived)
