"""Trusted scripts for Office conversion and page-bounded OCR inside Docker."""

from __future__ import annotations

import json


def conversion_script(operation: str, input_relative: str, *, pages: list[int] | None = None) -> tuple[str, str]:
    from folio.research.sandbox import SandboxError

    if operation not in {"ocr", "office_ocr", "office_pdf", "office_recalculate"}:
        raise SandboxError('Unsupported file conversion operation', code="operation_denied")
    if pages is not None:
        if operation not in {"ocr", "office_ocr"}:
            raise SandboxError('Only OCR supports selecting page numbers', code="invalid_pages")
        if (not isinstance(pages, list) or not 1 <= len(pages) <= 20
                or any(type(page) is not int or page < 1 or page > 2000 for page in pages)
                or len(set(pages)) != len(pages)):
            raise SandboxError('Each OCR request must select 1 to 20 distinct valid page numbers', code="invalid_pages")
    parameters = json.dumps({"operation": operation, "input": input_relative, "pages": sorted(pages) if pages else None})
    script = "PARAMETERS = " + repr(parameters) + "\n" + _SCRIPT
    return script, "ocr" if operation in {"ocr", "office_ocr"} else "office"


_SCRIPT = r'''
import json, os, pathlib, shutil, subprocess, tempfile
from PIL import Image

params = json.loads(PARAMETERS)
source = pathlib.Path('/sources') / params['input']
out = pathlib.Path(os.environ['FOLIO_ARTIFACTS'])
operation = params['operation']
metadata = {'operation': operation, 'input': params['input'], 'warnings': []}

def command(args):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=240)
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:] or 'conversion failed')
    return result.stdout

if operation in {'ocr', 'office_ocr'}:
    metadata['blocks'] = []
    with tempfile.TemporaryDirectory() as directory:
        temp = pathlib.Path(directory)
        if operation == 'office_ocr':
            command(['libreoffice', '-env:UserInstallation=file:///tmp/lo-profile', '--headless',
                     '--convert-to', 'pdf', '--outdir', str(temp), str(source)])
            converted = list(temp.glob('*.pdf'))
            if len(converted) != 1:
                raise RuntimeError('Office did not produce a PDF for OCR')
            source = converted[0]
            shutil.copyfile(source, out / 'source-preview.pdf')
            metadata['preview_pdf'] = 'source-preview.pdf'
        if source.suffix.lower() == '.pdf':
            from pypdf import PdfReader
            pages = len(PdfReader(source).pages)
            original = None
        else:
            original = Image.open(source)
            pages = getattr(original, 'n_frames', 1)
        metadata['page_count'] = pages
        selected = params.get('pages')
        if selected is None:
            if pages > 100:
                raise ValueError('OCR input exceeds 100 pages; select up to 20 original page numbers')
            selected = list(range(1, pages + 1))
        if any(page > pages for page in selected):
            raise ValueError('Selected OCR page is outside the original document')
        metadata['selected_pages'] = selected
        try:
            for page in selected:
                prefix = temp / ('page-' + str(page))
                image_path = prefix.with_suffix('.png')
                if original is None:
                    command(['pdftoppm', '-f', str(page), '-l', str(page), '-scale-to', '2400',
                             '-png', '-singlefile', str(source), str(prefix)])
                else:
                    original.seek(page - 1)
                    image = original.convert('RGB')
                    image.thumbnail((5000, 5000))
                    image.save(image_path)
                text = command(['tesseract', str(image_path), 'stdout', '-l', 'eng+chi_sim'])
                metadata['blocks'].append({'locator': 'page:' + str(page), 'text': text.strip(), 'kind': 'ocr',
                                           'metadata': {'page': page, 'extraction': 'tesseract'}})
                image_path.unlink(missing_ok=True)
        finally:
            if original is not None:
                original.close()
    metadata['text'] = '\n\n'.join(block['text'] for block in metadata['blocks'])
    metadata['warnings'].append('OCR text is derived; verify critical numbers and mandatory clauses against original pages.')
    (out / 'extraction.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
else:
    target = 'pdf' if operation == 'office_pdf' else 'xlsx'
    if operation == 'office_recalculate' and source.suffix.lower() not in {'.xlsx', '.xls', '.ods'}:
        raise ValueError('Recalculation accepts spreadsheets only')
    command(['libreoffice', '-env:UserInstallation=file:///tmp/lo-profile', '--headless',
             '--convert-to', target, '--outdir', str(out), str(source)])
    products = list(out.glob('*.' + target))
    if len(products) != 1:
        raise RuntimeError('Office did not produce the expected document')
    metadata['output'] = products[0].name
    if operation == 'office_recalculate':
        metadata['warnings'].append('Calculated with LibreOffice; formula compatibility may differ from Microsoft Excel.')
    (out / 'conversion.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
print(json.dumps({'operation': operation, 'files': sorted(p.name for p in out.iterdir())}))
'''
