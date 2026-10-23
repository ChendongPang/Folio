"""Describe example documents without exposing their business content as evidence."""

from collections import Counter
from pathlib import Path
from statistics import median

from folio.document.reader import read_docx
from folio.models import digest


def sample_profile(paths: list[str | Path], view: str = "final") -> dict:
    documents = []
    for path in paths:
        if Path(path).suffix.lower() != ".docx":
            from folio.document.style_sampling import measure_source, read_style_source

            documents.append(measure_source(read_style_source(Path(path), revision_view=view)))
            continue
        source = read_docx(Path(path), view)
        body = [b for b in source.blocks if b.path]
        paragraphs = [len(b.text) for b in body if b.kind == "paragraph" and b.text]
        tables = [b for b in body if b.kind == "table"]
        # Only aggregate measurements cross into model context. No example titles,
        # cells, paragraphs, comments, cover values, or filenames are included.
        documents.append(
            {
                "version": digest(source.path.read_bytes().hex()),
                "heading_counts_by_level": dict(
                    sorted(Counter(b.level for b in body if b.kind == "heading").items())
                ),
                "paragraph_count": len(paragraphs),
                "median_paragraph_chars": int(median(paragraphs)) if paragraphs else 0,
                "table_count": len(tables),
                "table_column_counts": sorted({len(t.rows[0]) for t in tables if t.rows}),
            }
        )
    return {
        "role": "style_only",
        "documents": documents,
        "guidance": [
            "Use examples only for expression and detail, never as project facts or scope evidence.",
            "Organize scenarios, input tables and processing steps according to writing requirements and the confirmed directory; statistics are not word or section quotas.",
            "Where supported by evidence, specify roles, triggers, inputs/outputs, processing steps, exceptions and human confirmation.",
            "Do not inherit example project names, dates, business rules, periods, comments or approval status.",
        ]
        if documents
        else [],
    }
