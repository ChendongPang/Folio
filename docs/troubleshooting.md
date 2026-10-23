# Troubleshooting

> 中文版: [故障排查](troubleshooting.zh-CN.md)

Start with the job's status, stage, error category, and saved issues. Keep the original record so that a retry or a new export can be distinguished from the failed attempt.

```bash
uv run folio --data-dir .folio status JOB_ID
uv run python scripts/audit_document_run.py \
  --root .folio --job JOB_ID --output .folio/audits/JOB_ID
```

The audit command is offline and does not resume generation. Its reports can include source text; review them before sharing. See [testing and acceptance](testing.md) for timing and call-count interpretation.

## Connections and optional services

| Symptom | Check and next action |
| --- | --- |
| Main model connection or tool-call failure | Verify the Base URL, model ID, credentials, and native tool-calling support. The Base URL excludes `/chat/completions`. A text-only or legacy function-call response does not satisfy the native tool protocol. |
| Jev or Tavily appears configured but inactive | Test the exact configuration, explicitly enable the service, and save. Changing relevant fields invalidates the previous successful test; a failed retest of the saved configuration revokes its capability. |
| Authentication or billing error | Correct credentials or account availability before retrying. HTTP 401/403 and 402 are not transient service failures. |
| Rate limit, temporary disconnect, or server error | Inspect the recorded retry and timeout information. Recovery is bounded; an unavailable service can still stop the task. |
| Capacity measurement fails | Verify model metadata/tokenization availability and configured input/output limits. A short character count does not establish that the request fits the model window. |

Decision routing varies by operation. Strict relevance selection with an enabled decision model does not silently fall back to the main LLM on service failure. A successful probe also does not establish that every subsequent request will fit the model's capacity. See [context and capacity](context-and-capacity.md).

## Outline planning and recovery

Inspect all reported validation locations: duplicate sibling titles or IDs, invalid hierarchy, missing writing requirements, and incomplete coverage can coexist. A repaired candidate still needs final validation and preview inspection. Do not accept truncated JSON or bypass validation to obtain an outline.

For a timeout, use the recorded transport phase, elapsed time, and received-character count to distinguish connection, first-response, and streaming delays when those diagnostics are available. Older records without this detail cannot establish a particular network cause. A `length_limit` error indicates incomplete output and should be checked against the configured output budget.

Use the current workspace's retry or continuation controls after correcting the underlying cause. Replanning from sources and editing an existing outline have different scopes; inspect and confirm the resulting outline before body generation. A successful outline preview verifies planning and preview export only. See [outline planning](outline-planning.md).

## Incomplete research or pending confirmation

Source discovery, text extraction, and verified coverage are separate states. A readable file or a listed structure node does not prove that every page, sheet, table, or visual was examined. PDF bookmarks describe page ranges, which may overlap; OCR text still needs verification against the original. Check the requested scope and remaining coverage obligations before treating a gap as absent information.

Missing research images or conversion tools should be corrected in the execution environment. Folio does not fall back to executing generated research code on the host. For sandbox configuration, original-page viewing, OCR, and derived-artifact provenance, see [agent research](agent-research.md).

A `needs_confirmation` export preserves the draft and its unresolved issues. Review the issue list and provide the requested answers through the workspace. A downloadable DOCX does not mean review passed; exhausted capacity or call budgets can leave completed prose with unfinished checks. Do not delete issues or relabel proposals merely to obtain a published status.

## Provenance and red text

Red text marks `origin=model_proposal`: business content proposed without sufficient supporting evidence. Evidence-backed text can also have been written by an LLM. Color therefore describes provenance, not whether a model participated in writing.

Check both reference integrity and meaning. An evidence-backed factual block without citations, or a citation that does not resolve to retained evidence, needs investigation. A valid citation still does not prove the source supports the claim. Headings and empty data cells do not assert business facts by themselves.

Reformatting reports include `provenance`, with proposal counts, untraced blocks, and invalid references. This report does not perform semantic review and does not reclassify old content. Preserve the distinction among original project sources, public sources, derived results, and model proposals when correcting content.

## Fonts, lists, tables, and pagination

Inspect the actual Word document and, when available, its paginated PDF. Check that:

- The intended fonts are installed and the selected font is used for both text and table-of-contents entries. Theme fonts and character styles can affect rendering.
- Heading levels, TOC indentation, page numbers, headers, and footers remain consistent after pagination or a Word field update.
- Wrapped list items retain their hanging indent and child numbering. Decimal values and version numbers must remain ordinary text.
- Tables fit within page boundaries and preserve required headers, widths, and meaningful emphasis. Do not remove all colored or italic text to clear an example's formatting.
- Required figures are present, legible, and associated with the current body version. Red proposal text retains its provenance marking.

The current generation workflow does not run final Jev page scoring. Automatic format checks and successful export do not replace inspecting the delivered pages. See [illustrations](illustrations.md) and [source materials and layout](source-materials-and-layout.md).

For a completed job (`published`, `draft`, or `needs_confirmation`), create a separate export from saved content without model calls:

```bash
uv run folio --data-dir .folio reformat JOB_ID \
  --output-dir .folio/exports/reformatted \
  --require-pagination
```

Use a new output directory, separate from the original artifact. `--require-pagination` requires the pagination environment; omit it only when a nonpaginated export is acceptable. Inspect the returned layout report and `provenance` summary. Reformatting preserves saved business state and may reuse compatible saved formatting records; it cannot resolve missing evidence or supply a new semantic review.
