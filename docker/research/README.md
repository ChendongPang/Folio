# Research execution images

> 中文版: [中文版](README.zh-CN.md)

Build from the repository root:

```sh
docker build --target base -t folio-research-base:v2 -f docker/research/Dockerfile .
docker build --target data -t folio-research-data:v2 -f docker/research/Dockerfile .
docker build --target office -t folio-research-office:v2 -f docker/research/Dockerfile .
docker build --target ocr -t folio-research-ocr:v2 -f docker/research/Dockerfile .
```

The executor does not pull images automatically and never falls back to running generated code on the host. Operators can map the four fixed profile names to their own reviewed images using `profile_images`. Each execution resolves its image tag to an immutable image ID before starting.

User code runs without network access, with a read-only root filesystem, a non-root UID, dropped Linux capabilities, `no-new-privileges`, and explicit CPU, memory, PID, execution-time, log, and artifact limits. Only task scratch space and the execution's artifact directory are writable; source documents, task context, and approved dependencies are read-only. The Docker socket is never mounted.

Dependency requests accept a normalized package name and one exact version. The operator allowlist must include SHA-256 wheel hashes and every transitive dependency. The default permits only `networkx==3.4.2`. The installer downloads binary wheels from the operator's HTTPS `trusted_index_url`, verifies the approved hashes, and installs them with `--no-index --no-deps`. User Python code never runs in the network-enabled installer. Cache keys include the image digest and the complete dependency lock. Arbitrary URLs, indexes, Git repositories, paths, source distributions, and unapproved versions are rejected.

Persistent state is stored in `task_root/.research`; it is not mounted inside the executor. Source documents are mounted at `/sources`, context at `/context`, task scratch space at `/workspace/scratch`, and new outputs at `/workspace/artifacts` (available as `FOLIO_OUTPUT_DIR` and `FOLIO_ARTIFACTS`). Write files intended for registration to that output directory. Each successful result lists exact output hashes. Previously completed and verified output directories are mounted read-only at `$FOLIO_PREVIOUS_ARTIFACTS/<execution_id>`; their hashes are recorded in `input_artifacts`, and changes to an upstream output invalidate downstream provenance.

OCR and Office conversions use the same isolation and produce execution records that include the source hashes and image digest. The supported fixed operations are `ocr`, `office_pdf`, `office_recalculate`, and `office_ocr`. The last operation converts the original Office document to PDF and performs page-by-page OCR within a single execution. OCR results include the actual page count, page locations, and a warning to verify critical numbers against the original. Whole-document OCR has a 100-page/frame limit. Supplying `pages` selects up to 20 original page numbers per execution, allowing a long input to be read progressively without renumbering. `office_ocr` also saves `source-preview.pdf`; subsequent page viewing verifies the trusted conversion script and artifact hashes before use. LibreOffice recalculation retains its compatibility warning.

Operator settings are specified as a flat JSON object in `<data-dir>/settings/research.json`, or in the file specified by `FOLIO_RESEARCH_CONFIG`. For example:

```json
{
  "max_concurrent": 2,
  "memory_mb": 512,
  "pids_limit": 64,
  "profile_images": {
    "base": "folio-research-base:v2",
    "data": "folio-research-data:v2",
    "office": "folio-research-office:v2",
    "ocr": "folio-research-ocr:v2"
  }
}
```

The standalone executor defaults below are further constrained by the job's `ResearchConfig`; the business research loop defaults to 120 seconds per execution and 600 seconds in total.

| Key | Default |
| --- | --- |
| `profile_images` | The four local image tags above |
| `docker_binary` | `docker` |
| `max_concurrent` | 2 executions per process event loop |
| `cpu_limit`, `memory_mb`, `pids_limit` | 1 CPU, 512 MiB, 64 processes |
| `timeout_seconds`, `max_timeout_seconds` | 60, 300 seconds |
| `max_output_bytes` | 128 KiB combined stdout and stderr |
| `max_artifact_bytes`, `max_total_artifact_bytes` | 32 MiB per file, 128 MiB total |
| `max_artifacts`, `max_scratch_bytes` | 100 files, 256 MiB scratch space |
| `trusted_index_url`, `dependency_network` | `https://pypi.org/simple`, `bridge` (installer only) |
| `dependency_cache_root` | Task-local `.research/dependency_cache` |
| `dependency_allowlist` | The exact default NetworkX wheel lock |

An allowlist entry has the form `{"package-name":{"1.2.3":{"sha256":["<64 lowercase hexadecimal characters>"],"dependencies":{"another-package":"4.5.6"}}}}`. Every transitive package must have its own allowed exact version and wheel hashes. The network-enabled installer does not accept a model-selected index and runs only the fixed wheel download/install script. Runtime containers always use `--network none`.

Run the isolation and conversion checks after building the images:

```sh
.venv/bin/python -m pytest -q tests/test_research_sandbox.py
FOLIO_TEST_DEPENDENCY_INSTALL=1 .venv/bin/python -m pytest -q tests/test_research_sandbox.py
```

The second command also contacts the trusted package index to validate dependency installation and reuse. Tests skip profiles that are not installed; skipped execution tests do not demonstrate that isolation or conversion checks passed on that machine.

Source-page viewing uses host Poppler (`pdftoppm`) for bounded PDF rendering and Pillow for raster images. The research model must explicitly enable `supports_image_input` for `fs.view`, independent OCR review, and visual extraction. These reviews bind the hashes of the original source, page, text, and rendered image; successful OCR alone never constitutes verification.
