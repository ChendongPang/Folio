# Folio Workspace Migration

> 中文版: [中文版](workspace-migration.zh-CN.md)

Folio uses `folio` as both its Python package name and command name, `.folio` as its default workspace, and `folio.sqlite3` as its main database. Absolute paths in the database do not update automatically when the project or data directory changes. [`scripts/relocate_workspace.py`](../scripts/relocate_workspace.py) migrates known active paths offline and renames the specified main database file to `folio.sqlite3`.

By default, the script performs a **dry-run and only reports planned changes**. It does not move the project directory, start services, or call models. Historical conversations remain in the database; renaming browser storage keys is a separate UI state change.

## Migration Scope

The script only replaces paths that exactly match the original project or data directory, along with their subpaths, matching longer directory prefixes first. It updates:

- Job material paths, reference and style file paths, template paths, and artifact paths in the main database, as well as research directories and note paths for saved business tasks.
- File paths in upload records and `workspace_root` in the `research_sessions` research ledger.
- Research paths in `jobs/*/work-records.json`, and document and PDF paths in `directory-drafts/*/inspection.json`.

The script does not perform global string replacement in original uploads, material structure indexes, signed artifacts, historical model requests, events, or audit logs. Original paths in historical records preserve the facts of past executions; this does not mean that all such paths can be used as entry points after migration.

**If the script finds `artifact_records/*.json`, it refuses to proceed, even in dry-run mode.** These derived evidence records have content and provenance-chain signatures and require a dedicated process for signature verification, path migration, and re-signing; the current script does not implement this process. Do not delete signature records, tamper with originals, or recompute source hashes to bypass this restriction. Copying the signing key alone is also insufficient to keep modified records valid.

## Stop Services, Back Up, and Move Directories

1. First pause running tasks in the UI and wait for their status to become “Paused,” then stop all services and Workers using the workspace. The script refuses to proceed if it detects concurrent modifications, but it does not stop services automatically.
2. Copy the **entire data directory** to a backup location outside the project, preserving original uploads, jobs, databases and their companion files, model configuration, and research configuration. Back up the host signing key separately. Do not copy only the main database file.
3. Record the absolute project path, absolute data path, and database filename before moving, then move the project and data directories. The paths below are examples only and must be replaced with actual locations. If the data is inside the project, you may also need to rename the data subdirectory after moving the project.
4. Run a dry-run from the new directory, review the report, and only then use `--apply`. If the script refuses to proceed, keep services stopped and investigate the cause. Do not start an empty workspace at the new location before migration is complete.

```bash
# Enter the actual paths before and after the move; these variables do not move directories automatically.
original_root='/srv/projects/original-project'
folio_root='/srv/projects/folio'
original_data='/srv/projects/original-project/.workspace'
folio_data='/srv/projects/folio/.folio'
original_database='workspace.sqlite3'

cd "$folio_root"
python3 scripts/relocate_workspace.py \
  --data-dir "$folio_data" \
  --from-root "$original_root" --to-root "$folio_root" \
  --from-data "$original_data" --to-data "$folio_data" \
  --database-name "$original_database"
```

This command uses the Python standard library and does not depend on whether the relocated virtual environment has been repaired. `--data-dir` must point to the data directory that currently exists. The script does not copy original materials from `--from-root` or `--from-data`.

After confirming that the databases to be changed, record counts, and JSON files in the report match expectations, run it again with the same parameters plus:

```bash
python3 scripts/relocate_workspace.py \
  --data-dir "$folio_data" \
  --from-root "$original_root" --to-root "$folio_root" \
  --from-data "$original_data" --to-data "$folio_data" \
  --database-name "$original_database" \
  --backup-dir '/srv/backups/folio-active-state-migration-01' \
  --apply
```

`--backup-dir` must be a **new directory that does not yet exist**, located outside the current, original, and target data directories. Before writing, the script uses the SQLite backup API to back up databases it will read or modify, including committed WAL contents, and backs up JSON files it will modify. This active-state backup does not replace the full data backup in step 2. Backup directory permissions are `0700`; database backups, JSON backups, and migration reports have permissions of `0600`.

Writes use per-database transactions, original-value checks, and JSON replacement via temporary files. If a subsequent write fails, the script attempts to restore the active state from backup. The main database is renamed only after successful writes and a WAL checkpoint. If both the original database and the target `folio.sqlite3` exist, the script refuses to overwrite either; first determine which workspace each belongs to.

## Parameters and Reports

| Parameter | Meaning |
| --- | --- |
| `--data-dir` | Existing data directory to migrate; required |
| `--from-root` / `--to-root` | Original and new project directories; both required |
| `--from-data` / `--to-data` | Original and new data directories; both required; data path matching takes precedence over project path matching |
| `--database-name` | Current main database filename; paths are not accepted; defaults to `folio.sqlite3` |
| `--backup-dir` | New external backup directory; required when writing changes |
| `--apply` | Write changes; omit to preview only |

The JSON report includes `mode`, the target `database`, whether it will be renamed, change counts for each database, JSON files to be modified, and `changed`. `historical_audits_and_sources_changed=false` means the script did not rewrite historical audits or original materials; it does not mean all old tasks have passed validation for resuming.

After success, you can run another dry-run with the same parameters. If the original file has been renamed, the script reads `folio.sqlite3`; once all paths have been migrated, it should return `changed=false`. Read-only previews and repeated checks do not reset task budgets or model call counts.

## Update the Host Environment Too

The migration script does not modify environment variables, service configuration, browser storage, Office installations, or Docker images. Before starting new services, verify each actual path:

| Configuration | Current Name and Location |
| --- | --- |
| Main model configuration | `settings/model.json` under the data directory, or the file specified by `FOLIO_MODEL_CONFIG` |
| Example model environment variables | `FOLIO_MODEL_BASE_URL`, `FOLIO_MODEL`, `FOLIO_MODEL_API_KEY`; variable references in configuration files must match the new names |
| Research configuration | `settings/research.json` under the data directory, or `FOLIO_RESEARCH_CONFIG` |
| Office | `FOLIO_OFFICE_PYTHON` and `FOLIO_SOFFICE` must be configured together, pointing respectively to a Python executable that can import `uno` and the LibreOffice executable |
| Local Office configuration | If not explicitly configured, `~/.local/share/folio/office/runtime.json` is checked first, and the paths it contains must also exist; only then is a system installation attempted |
| Derived evidence signing key | Defaults to `~/.local/state/folio/artifact-signing.key`; use `FOLIO_ARTIFACT_KEY_PATH` to specify an existing key file |
| Chromium and Mermaid | Use `FOLIO_CHROMIUM_EXECUTABLE` and `FOLIO_MERMAID_JS`, respectively, for custom paths |

The signing key is a separate 32-byte host file. Preserve its original bytes and restrictive permissions; do not commit it to the code repository or let a different key generated by a new installation replace the key used for existing evidence. Existing signed records remain subject to the migration restrictions described above.

If you copy a local Office installation directory, updating `runtime.json` alone is not enough: `fundamentalrc`, `unorc`, `sofficerc`, and absolute symbolic links may still point to the original installation path. Check and update these location settings, retain the original installation if other services still use it, and then run `uv run pytest tests/test_pagination.py -q` to verify actual pagination. Mixing old and new UNO components may prevent LibreOffice from starting.

The default research images are `folio-research-base:v2`, `folio-research-data:v2`, `folio-research-office:v2`, and `folio-research-ocr:v2`. Build them according to the [research environment deployment instructions](../docker/research/README.md), or explicitly configure existing, verified local images in `profile_images`. The program does not pull images automatically, nor does it execute model code on the host if an image cannot be found. Changes to image contents change the immutable image digest, so continued reuse of derived evidence that depends on the original digest cannot be guaranteed.

## Post-Startup Checks

After directories are moved, the old virtual environment may still contain startup scripts and editable-install records referencing the original paths. Archive the old environment separately, then run the following again from the new project root:

```bash
uv sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv run folio --data-dir "$folio_data" serve --port 8765
```

Open `http://127.0.0.1:8765/`. First verify historical conversations, attachments, and downloads of existing documents, then check each task that needs to resume. Removing the document type selector does not clear writing guidelines saved on the backend; new conversations have no additional guidelines by default.

The browser uses the `folio.` prefix to store conversation entry points, unsynced drafts, and panel dimensions. The script does not migrate this browser data. Synced messages should still be recoverable from the server-side history list; save unsynced local drafts separately before moving. Tasks with mismatched material versions, signatures, runtime environments, or checkpoints will still refuse to resume. Successful path updates do not replace these checks.

If rollback is needed, stop services first, preserve the failed state, and then restore the original directories and matching runtime environment from the full backup. Do not run services in both locations at the same time writing to the same workspace.
