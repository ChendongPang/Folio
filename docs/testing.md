# Testing and acceptance

> 中文版: [测试与验收](testing.zh-CN.md)

Run commands from the repository root. Follow the [setup instructions](../README.md) first.

## Local checks

```bash
uv run pytest -q -r s
uv run ruff check src tests scripts
npm --prefix frontend run build
git diff --check
```

Record the command, code revision, result, and skip reasons. A focused rerun verifies its selected cases; it does not replace a failed full-suite result. Overlapping test groups must not be added together as a unique test count.

Some integration tests require local tools or fixtures:

| Area | Prerequisites and scope |
| --- | --- |
| Browser interactions and illustration rendering | Playwright Chromium; use `uv run playwright install chromium` to install it. Mocked provider responses test the interface and workflow, not live service compatibility. |
| Word pagination and page rendering | LibreOffice/UNO and Poppler. Inspect the skip reasons when these tools are missing. |
| Sandboxed research, Office conversion, and OCR | Build the Docker research images described in [agent research](agent-research.md). Missing Docker or images cause relevant tests to skip. |
| Dependency installation | `FOLIO_TEST_DEPENDENCY_INSTALL=1 uv run pytest -q tests/test_research_sandbox.py` additionally contacts the configured trusted package index. |
| Synthetic document fixtures | Public, reproducible source materials and expected outcomes are available under `examples/acceptance_cases/`. |

Useful focused checks include:

```bash
uv run pytest -q tests/test_native_tool_runtime.py tests/test_optional_decision_routing.py
uv run pytest -q tests/test_directory_research_resume.py tests/test_directory_review.py
uv run pytest -q tests/test_research_sandbox.py tests/test_source_index.py
uv run pytest -q tests/test_document_run_audit.py tests/test_audited_replay.py
uv run pytest -q tests/test_acceptance_cases.py tests/test_acceptance_output_counting.py
```

Controlled runtimes and synthetic evidence verify protocol, scheduling, provenance, cancellation, and recovery contracts. They do not establish real-model reasoning quality, OCR accuracy, visual quality, or end-to-end performance.

## Live acceptance

Treat live provider probes and full generation as separate tests. A successful connection test establishes basic compatibility; it does not establish that research, writing, review, and export succeed together. For a representative document, check:

1. The confirmed outline covers the requested scope and each required section has a single owner.
2. Facts, calculations, exceptions, source versions, and coverage match the original materials. Reading a manifest or finding a citation alone does not prove that all source content was understood.
3. OCR and visual evidence are checked against their original pages; derived calculations retain their input and execution provenance.
4. Unsupported proposals remain visibly marked, and unresolved questions remain explicit. A `needs_confirmation` draft can preserve useful work while review is incomplete.
5. The DOCX opens, contains the required sections and figures, and matches the saved body version. Inspect actual Word/PDF pages for fonts, lists, table boundaries, page numbers, diagram labels, and pagination.

Use synthetic acceptance packages with their expected answers kept on the evaluator side. Keyword matches and output-size checks help find omissions; semantic correctness and business approval still require review. See [source materials and layout](source-materials-and-layout.md) and [illustrations](illustrations.md).

## Reproducible replays

[`scripts/replay_audited.py`](../scripts/replay_audited.py) performs a fresh run with live services. Set a run limit and retain failed or interrupted reservations. Use a new output directory for a new series, keep the source job read-only, and keep the code, source files, confirmed outline, and service configuration stable throughout each run. The script creates an independent store and does not import cached model results.

For a single-run series, create `.folio/benchmarks/acceptance/ledger.json` with the following content, replacing `SOURCE_JOB_ID` with the actual source job ID. Do not overwrite an existing ledger.

```json
{
  "source_job": "SOURCE_JOB_ID",
  "max_full_runs": 1,
  "max_optimization_rounds": 1,
  "optimization_rounds": [{"round": 1, "status": "ready"}],
  "full_runs": []
}
```

```bash
uv run python scripts/replay_audited.py SOURCE_JOB_ID \
  --root .folio \
  --output-root .folio/benchmarks/acceptance \
  --timeout 1200 --generation-timeout 900 --parallel 16
```

The default mode uses the source job's confirmed outline. Add `--from-uploads` to rerun source preparation, intent recognition, outline planning, and preview before generation. This mode uses the GUI's service functions with automated confirmation; it does not test browser interactions or include user reading time. Compare runs only when their starting points, inputs, and limits are equivalent.

Inspect `result.json`, `timing.json`, and the actual artifact in the new run directory. `elapsed_seconds` and `generation_to_export_seconds` measure different scopes. The script's `passed` field includes a built-in 420-second target and automatic artifact checks; it is not a general performance promise or proof of business quality. Record the actual outcome, unresolved issues, `review_passed`, and manual inspection separately. A failure before export is not a fast completed delivery.

## Offline audit and timing

[`scripts/audit_document_run.py`](../scripts/audit_document_run.py) reads saved records without calling providers or resuming a job:

```bash
uv run python scripts/audit_document_run.py \
  --root .folio \
  --job JOB_ID \
  --output .folio/audits/JOB_ID
```

For a replay, point `--root` at that run's `data` directory and use its new job ID. The output includes `report.html`, `report.json`, and CSV files for stage timing, research, provenance, review, revisions, and cache reuse. Reports can contain source text; keep them in local, ignored storage and redact material before sharing.

| Measurement | Interpretation |
| --- | --- |
| Stage wall-clock time | Disjoint stage intervals can be added. Overlapping task or request durations cannot. Record an unexecuted stage as unexecuted, not as a successful zero-second stage. |
| Engine attempts and `jobs.calls` | Logical actions and retries. They are not provider HTTP counts and do not include all preparation work. |
| Recorded provider operations | Pair starts and completions by operation ID; retain failures, cancellations, and unfinished requests. Internal retries can create multiple operations. |
| Auxiliary requests and retrieval | Model metadata/tokenization calls are separate from generation. MCP tool-call counts do not include all transport handshakes or heartbeats. |
| Cache hits | Reused results do not represent new provider calls. |
| Token usage | Count only usage returned by the provider. Missing usage remains unknown; reported totals are not a complete billing record. |

Action durations can include local queueing. Investigate repeated reads, retries, output truncation, capacity checks, and task-slot waits before attributing a slow run to model inference or increasing concurrency. Missing instrumentation cannot be reconstructed as exact timings or HTTP counts. See [troubleshooting](troubleshooting.md) for diagnosis and recovery.
