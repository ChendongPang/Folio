"""Public documentation uses paired English/Chinese names and resolvable local links."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def technical_documents():
    directories = [ROOT, ROOT / "docs", ROOT / "docker/research", ROOT / "examples/acceptance_cases"]
    return sorted(path for directory in directories for path in directory.glob("*.md"))


def documentation_pairs():
    for source in technical_documents():
        if source.name.endswith(".zh-CN.md"):
            yield source, source.with_name(source.name.replace(".zh-CN.md", ".md"))


def structure(text):
    return (
        len(re.findall(r"^#{1,6} ", text, re.MULTILINE)),
        text.count("```"),
        len(re.findall(r"^```mermaid", text, re.MULTILINE)),
    )


def test_technical_document_names_and_language_pairs():
    documents = technical_documents()
    assert documents
    for path in documents:
        assert path.name.isascii(), path
        assert re.fullmatch(r"(?:README|[a-z0-9]+(?:-[a-z0-9]+)*)(?:\.zh-CN)?\.md", path.name), path
        if not path.name.endswith(".zh-CN.md"):
            assert path.with_name(path.stem + ".zh-CN.md").is_file(), path
    for source, target in documentation_pairs():
        assert target.is_file(), source


def test_translated_documents_preserve_structure_and_have_english_prose():
    mismatches = []
    chinese_prose = []
    for source, target in documentation_pairs():
        chinese = source.read_text(encoding="utf-8")
        english = target.read_text(encoding="utf-8")
        if structure(chinese) != structure(english):
            mismatches.append(str(target.relative_to(ROOT)))
        prose = re.sub(r"\]\([^)]*\)", "](LINK)", english)
        prose = "\n".join(
            line for line in prose.splitlines()
            if not line.startswith(("> 中文版:", "English |", "[English]"))
        )
        has_fixture_paths = target.is_relative_to(ROOT / "examples" / "acceptance_cases")
        if not has_fixture_paths and re.search(r"[\u3400-\u9fff]", prose):
            chinese_prose.append(str(target.relative_to(ROOT)))
    assert mismatches == []
    assert chinese_prose == []


def test_local_documentation_links_resolve_after_relocation():
    missing = []
    for source in technical_documents():
        text = source.read_text(encoding="utf-8")
        for link in re.findall(r"\]\(([^)]+)\)", text):
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = source.parent / unquote(parsed.path)
            if not target.exists():
                missing.append(f"{source.relative_to(ROOT)}: {link}")
    assert missing == []
