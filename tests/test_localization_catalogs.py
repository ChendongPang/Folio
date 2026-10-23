"""Guard translation placeholders and the English maintenance-language policy."""

import ast
import io
import json
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HAN = re.compile(r"[\u3400-\u9fff]")
SLOT = re.compile(r"\{\w+\}")


def test_catalogs_preserve_all_interpolation_parameters():
    for path in (ROOT / "src/folio/locales").glob("*.json"):
        pairs = json.loads(path.read_text(encoding="utf-8"))
        assert pairs, path
        for source, english in pairs.items():
            assert english.strip() and not HAN.search(english), (path.name, source)
            assert set(SLOT.findall(source)) == set(SLOT.findall(english)), (path.name, source)


def test_python_comments_and_docstrings_are_english():
    violations = []
    for directory in (ROOT / "src", ROOT / "scripts"):
        for path in directory.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT and HAN.search(token.string):
                    violations.append((str(path.relative_to(ROOT)), token.start[0]))
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    if HAN.search(ast.get_docstring(node) or ""):
                        violations.append((str(path.relative_to(ROOT)), getattr(node, "lineno", 1)))
    assert violations == []
