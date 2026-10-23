"""Host-owned, signed provenance for computed research evidence.

Saving an artifact is not a claim of correctness.  A separate review, after the
input/script/output chain has been checked, authorizes its use as evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any

from filelock import FileLock

from folio.models import Evidence

MAX_RECORD_BYTES = 4_000_000
MAX_RESULT_BYTES = 200_000
MAX_SCRIPT_BYTES = 200_000
MAX_INPUT_CHARACTERS = 200_000
_ID = re.compile(r"^[a-f0-9]{64}$")


class ArtifactError(ValueError):
    """A research result has incomplete or invalid provenance."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe_path(root: Path, name: str, *, exists: bool = True) -> Path:
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ArtifactError('Research sources and artifacts must use paths relative to the workspace')
    root = root.resolve()
    path = root / relative
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ArtifactError('Research sources and artifacts cannot use symbolic links')
    if not path.resolve().is_relative_to(root):
        raise ArtifactError('The research source or artifact is outside the workspace')
    if exists and not path.is_file():
        raise ArtifactError('The research source or artifact does not exist')
    return path


def _stat_key(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=512)
def _cached_hash(path: str, stat_key: tuple) -> str:
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    if _stat_key(Path(path)) != stat_key:
        raise ArtifactError('A research file changed during verification')
    return result.hexdigest()


def _file_hash(path: Path) -> str:
    return _cached_hash(str(path), _stat_key(path))


def _key_path() -> Path:
    configured = os.environ.get("FOLIO_ARTIFACT_KEY_PATH")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "state" / "folio" / "artifact-signing.key"
    )


def _signing_key(*, create: bool = False) -> bytes:
    path = _key_path()
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise ArtifactError('The derived-evidence signing key cannot use a symbolic link')
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path) + ".lock"):
            if not path.exists():
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(secrets.token_bytes(32))
    if not path.is_file() or path.stat().st_size != 32:
        raise ArtifactError('The derived-evidence signing key is missing or invalid')
    return path.read_bytes()


def _write_record(path: Path, record: dict) -> None:
    payload = {key: value for key, value in record.items() if key != "signature"}
    payload["signature"] = hmac.new(_signing_key(create=True), _canonical(payload), "sha256").hexdigest()
    encoded = _canonical(payload)
    if len(encoded) > MAX_RECORD_BYTES:
        raise ArtifactError('The derived-evidence record exceeds the size limit')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp-" + secrets.token_hex(6))
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_record(path: Path) -> dict:
    if (
        not _ID.fullmatch(path.stem)
        or path.suffix != ".json"
        or path.parent.name != "artifact_records"
        or path.parent.parent.name != ".research"
        or any(parent.is_symlink() for parent in [path, *path.parents])
    ):
        raise ArtifactError('Invalid derived-evidence record location')
    if not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
        raise ArtifactError('The derived-evidence record is missing or exceeds the size limit')
    try:
        record = json.loads(path.read_bytes())
        if not isinstance(record, dict):
            raise ValueError
        signature = record.pop("signature", "")
        expected = hmac.new(_signing_key(), _canonical(record), "sha256").hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise ArtifactError('Invalid derived-evidence record signature')
        if record["version"] != 1 or record["artifact_id"] != path.stem:
            raise ArtifactError('Invalid derived-evidence record version or identity')
        if Path(record["task_root"]).resolve() != path.parent.parent.parent.resolve():
            raise ArtifactError('The derived-evidence record does not belong to the current task')
        if _digest(record["registration"]) != record["artifact_id"]:
            raise ArtifactError('The derived-evidence record identity does not match')
        return record
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        if isinstance(exc, ArtifactError):
            raise
        raise ArtifactError('Invalid derived-evidence record format') from exc


def _validate_chain(record: dict) -> None:
    task_root, source_root = Path(record["task_root"]), Path(record["source_root"])
    registration = record["registration"]
    for item in registration["input_files"]:
        path = _safe_path(source_root, item["path"])
        if _file_hash(path) != item["sha256"]:
            raise ArtifactError('The calculation input source version changed: ' + item["path"])
    for item in [registration["script"], registration["output"]]:
        path = _safe_path(task_root, item["path"])
        if _file_hash(path) != item["sha256"]:
            raise ArtifactError('The research script or result file changed: ' + item["path"])
    execution_path = _safe_path(task_root, registration["execution_record"]["path"])
    if _file_hash(execution_path) != registration["execution_record"]["sha256"]:
        raise ArtifactError('The research execution record changed')
    for previous in registration.get("intermediate_executions", []):
        for item in [previous["script"], previous["execution_record"], *previous["outputs"]]:
            path = _safe_path(task_root, item["path"])
            if _file_hash(path) != item["sha256"]:
                raise ArtifactError('A preceding calculation step, result, or execution record changed: ' + item["path"])
        for item in previous["input_files"]:
            if _file_hash(_safe_path(source_root, item["path"])) != item["sha256"]:
                raise ArtifactError('The source version used by a preceding calculation changed: ' + item["path"])


def _evidence(record: dict, manifest_path: Path) -> Evidence:
    registration = record["registration"]
    artifact_id = record["artifact_id"]
    # This separator is part of signed evidence, not a UI label. Existing
    # receipts must reconstruct their original bytes after a locale update.
    separator = (
        "\nCalculation result:\n"
        if record.get("evidence_text_format") == 2
        else "\n计算结果：\n"
    )
    return Evidence(
        evidence_id="derived:" + artifact_id,
        source_id="research:" + Path(record["task_root"]).name,
        source_version=artifact_id,
        locator=registration["output"]["path"],
        text=registration["description"] + separator + registration["result_text"],
        metadata={
            "provenance": "derived",
            "derivation": {
                "artifact_id": artifact_id,
                "manifest_path": str(manifest_path.resolve()),
                "execution_id": registration["execution_id"],
                "environment_key": _digest(registration["environment"]),
                "status": record["validation"]["status"],
            },
        },
    )


def derived_evidence_error(evidence: Evidence) -> str | None:
    """Recheck the host receipt, never trust a model-provided ``verified`` flag."""
    extraction = evidence.metadata.get("extraction")
    if (
        evidence.metadata.get("requires_extraction_review")
        or evidence.metadata.get("source_kind") == "ocr"
        or (isinstance(extraction, dict) and extraction.get("method") == "ocr")
        or evidence.evidence_id.startswith(("extracted_", "extracted:", "visual:"))
        or evidence.metadata.get("source_kind") == "visual"
        or evidence.metadata.get("visual_extraction")
    ):
        from folio.research.extraction_review import reviewed_evidence_error

        return reviewed_evidence_error(evidence)
    derivation = evidence.metadata.get("derivation")
    is_derived = evidence.metadata.get("provenance") == "derived" or evidence.evidence_id.startswith("derived:")
    if derivation is None and not is_derived:
        return None
    try:
        if not isinstance(derivation, dict):
            raise ArtifactError('Derived evidence lacks a trusted research record')
        path = Path(derivation.get("manifest_path", ""))
        record = _read_record(path)
        expected = _evidence(record, path)
        if (
            evidence.evidence_id != expected.evidence_id
            or evidence.source_id != expected.source_id
            or evidence.source_version != expected.source_version
            or evidence.locator != expected.locator
            or evidence.text != expected.text
            or derivation.get("artifact_id") != record["artifact_id"]
        ):
            raise ArtifactError('Derived-evidence content does not match the research record')
        _validate_chain(record)
        if record["validation"]["status"] != "verified":
            raise ArtifactError('The derived result has not passed calculation-method and source verification')
        # A computed result cannot outlive the visual verification of its OCR
        # inputs. Registration only permits original sources, so this is not an
        # unbounded chain of model-created derived references.
        for source in record["registration"]["sources"]:
            if error := derived_evidence_error(Evidence.model_validate(source)):
                raise ArtifactError('Calculation input verification is no longer valid: ' + error)
        return None
    except (ArtifactError, OSError, ValueError, TypeError, KeyError) as exc:
        return str(exc) if isinstance(exc, ArtifactError) else 'The derived-evidence research record cannot be verified'


class ArtifactManager:
    """Register computed artifacts and review them independently from execution."""

    def __init__(self, task_root: Path, workspace, *, execution_lookup=None):
        self.task_root = Path(task_root).resolve()
        self.workspace = workspace
        self.source_root = Path(workspace.source_root).resolve()
        self.root = self.task_root / ".research" / "artifact_records"
        self.execution_lookup = execution_lookup

    def _path(self, artifact_id: str) -> Path:
        if not _ID.fullmatch(artifact_id):
            raise ArtifactError('Invalid research artifact ID')
        return self.root / (artifact_id + ".json")

    def _execution(self, execution_id: str) -> tuple[dict, Path]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", execution_id):
            raise ArtifactError('Invalid research execution ID')
        path = _safe_path(self.task_root, ".research/executions/" + execution_id + ".json")
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise ArtifactError('The research execution record exceeds the size limit')
        record = (
            self.execution_lookup(execution_id)
            if self.execution_lookup is not None
            else json.loads(path.read_bytes())
        )
        if not record or record.get("status") != "completed" or record.get("exit_code") != 0:
            raise ArtifactError('Only fully successful executions can register research evidence')
        return record, path

    def _validate_source(self, evidence: Evidence, documents: dict) -> None:
        path = _safe_path(self.source_root, evidence.source_id)
        if _file_hash(path) != evidence.source_version:
            raise ArtifactError('The cited source version does not match: ' + evidence.source_id)
        if evidence.metadata.get("extraction") or evidence.metadata.get("visual_extraction"):
            resolver = getattr(self.workspace, "resolve_evidence", None)
            if resolver is None or not resolver(evidence):
                raise ArtifactError('Extracted evidence does not match the actual extraction record')
            return
        if evidence.source_id not in documents:
            documents[evidence.source_id] = self.workspace.source_index.load(evidence.source_id)
        document = documents[evidence.source_id]
        if document.source_version != evidence.source_version:
            raise ArtifactError('The source version changed during parsing')
        for block in document.blocks:
            if evidence.locator == block.locator and evidence.text == block.text:
                return
            prefix = block.locator + "/characters "
            alternate = block.locator + " /characters "
            if evidence.locator.startswith((prefix, alternate)):
                suffix = evidence.locator.split("/characters ", 1)[1]
                match = re.fullmatch(r"(\d+):(\d+)", suffix)
                if match:
                    start, end = map(int, match.groups())
                    if 0 <= start < end <= len(block.text) and block.text[start:end] == evidence.text:
                        return
        raise ArtifactError('The cited location or text does not belong to the actual source: ' + evidence.locator)

    @staticmethod
    def _intermediate_text(path: Path) -> str:
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise ArtifactError('A preceding calculation artifact is too large to verify the complete calculation chain')
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            from folio.research.formats import read_source

            try:
                document = read_source(path)
            except ValueError as exc:
                raise ArtifactError('A preceding artifact cannot be read. Provide verifiable intermediate results') from exc
            if not document.blocks or document.metadata.get("complete") is False:
                raise ArtifactError('A preceding artifact still contains unread ranges; the full calculation chain cannot be verified') from None
            text = json.dumps({
                "blocks": [{"locator": block.locator, "text": block.text, "metadata": block.metadata} for block in document.blocks],
                "warnings": document.warnings,
            }, ensure_ascii=False)
        if len(text) > MAX_RESULT_BYTES:
            raise ArtifactError('Preceding calculation results exceed complete-verification capacity. Split the calculation')
        return text

    def _intermediate_executions(self, execution: dict) -> list[dict]:
        """Capture every mounted predecessor, including its actual computation.

        A successful final script cannot validate a wrong upstream calculation.
        The reviewer must see those scripts and intermediate values as well.
        """
        result, visiting = {}, set()

        def visit(current: dict, depth: int):
            if depth > 20 or len(result) > 100:
                raise ArtifactError('The calculation dependency chain is too deep or large. Split the research artifact')
            current_id = current.get("execution_id")
            if current_id in visiting:
                raise ArtifactError('The research execution record contains a dependency cycle')
            visiting.add(current_id)
            try:
                for item in current.get("input_artifacts", []):
                    previous_id = item.get("execution_id", "")
                    relative = item.get("path", "")
                    if not relative.startswith("artifacts/" + previous_id + "/"):
                        raise ArtifactError('The preceding artifact does not belong to its execution record')
                    previous, receipt = self._execution(previous_id)
                    listed = next((output for output in previous.get("output_files", previous.get("artifacts", [])) if output.get("path") == relative), None)
                    path = _safe_path(self.task_root, relative)
                    if listed is None or listed.get("sha256") != item.get("sha256") or _file_hash(path) != item.get("sha256"):
                        raise ArtifactError('The preceding calculation artifact does not match its execution record')
                    if previous_id not in result:
                        visit(previous, depth + 1)
                        script_path = previous.get("script_path", "")
                        script = _safe_path(self.task_root, script_path)
                        if _file_hash(script) != previous.get("script_sha256") or script.stat().st_size > MAX_SCRIPT_BYTES:
                            raise ArtifactError('A preceding calculation script is missing, invalid, or too large')
                        if not previous.get("image_digest") or not previous.get("profile"):
                            raise ArtifactError('A preceding calculation lacks exact environment versions')
                        result[previous_id] = {
                            "execution_id": previous_id,
                            "execution_record": {"path": receipt.relative_to(self.task_root).as_posix(), "sha256": _file_hash(receipt)},
                            "script": {"path": script_path, "sha256": _file_hash(script)},
                            "script_text": script.read_text(encoding="utf-8"),
                            "input_files": previous.get("source_manifest", []),
                            "environment": {"image_digest": previous["image_digest"], "profile": previous["profile"], "dependencies": previous.get("dependencies", [])},
                            "outputs": [],
                        }
                    outputs = result[previous_id]["outputs"]
                    if not any(output["path"] == relative for output in outputs):
                        outputs.append({"path": relative, "sha256": item["sha256"], "text": self._intermediate_text(path)})
            finally:
                visiting.remove(current_id)

        visit(execution, 0)
        return list(result.values())

    def register(
        self,
        execution_id: str,
        output_path: str,
        sources: list[Evidence | dict],
        methodology: str,
        description: str,
    ) -> Evidence:
        if not methodology.strip() or not description.strip() or len(methodology) > 20_000:
            raise ArtifactError('Research artifacts must provide a specific calculation method and conclusion')
        if not sources or len(sources) > 500:
            raise ArtifactError('Research artifacts must cite original source ranges, up to 500 references')
        evidence = [item if isinstance(item, Evidence) else Evidence.model_validate(item) for item in sources]
        if sum(len(item.text) for item in evidence) > MAX_INPUT_CHARACTERS:
            raise ArtifactError('The research input range is too large. Split the research artifact')
        execution, execution_path = self._execution(execution_id)
        environment = {
            "image_digest": execution.get("image_digest"),
            "profile": execution.get("profile"),
            "dependencies": execution.get("dependencies", []),
        }
        if not environment["image_digest"] or not environment["profile"]:
            raise ArtifactError('The research execution lacks exact environment versions')
        outputs = execution.get("output_files", execution.get("artifacts", []))
        selected = next((item for item in outputs if item.get("path") == output_path), None)
        if selected is None or not output_path.startswith("artifacts/" + execution_id + "/"):
            raise ArtifactError('The artifact does not belong to the specified research execution')
        output = _safe_path(self.task_root, output_path)
        script_path = execution.get("script_path", "")
        script = _safe_path(self.task_root, script_path)
        if _file_hash(output) != selected.get("sha256") or _file_hash(script) != execution.get("script_sha256"):
            raise ArtifactError('The research script or artifact does not match the execution record')
        if output.stat().st_size > MAX_RESULT_BYTES or script.stat().st_size > MAX_SCRIPT_BYTES:
            raise ArtifactError('The research result or script is too large. Produce a concise, verifiable result')
        try:
            result_text = output.read_text(encoding="utf-8")
            script_text = script.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise ArtifactError('Derived evidence requires a readable text result. Save a verification summary for binary files') from exc
        if not result_text.strip():
            raise ArtifactError('An empty result cannot be registered as research evidence')
        inputs = execution.get("source_manifest", [])
        input_versions = {item["path"]: item["sha256"] for item in inputs}
        documents = {}
        for item in evidence:
            if item.metadata.get("derivation") or item.metadata.get("provenance") == "derived":
                raise ArtifactError('Derived evidence must trace back to original inputs; citing only another calculation conclusion is insufficient')
            if input_versions.get(item.source_id) != item.source_version:
                raise ArtifactError("The cited source is absent from this execution's input snapshot")
            self._validate_source(item, documents)
        registration = {
            "execution_id": execution_id,
            "execution_record": {
                "path": execution_path.relative_to(self.task_root).as_posix(),
                "sha256": _file_hash(execution_path),
            },
            "input_files": [{"path": item["path"], "sha256": item["sha256"]} for item in inputs],
            "sources": [item.model_dump(mode="json") for item in evidence],
            "script": {"path": script_path, "sha256": _file_hash(script)},
            "script_text": script_text,
            "output": {"path": output_path, "sha256": _file_hash(output)},
            "result_text": result_text,
            "environment": environment,
            "intermediate_executions": self._intermediate_executions(execution),
            "methodology": methodology,
            "description": description,
        }
        artifact_id = _digest(registration)
        path = self._path(artifact_id)
        if path.exists():
            return self.load(artifact_id)
        record = {
            "version": 1,
            "evidence_text_format": 2,
            "artifact_id": artifact_id,
            "task_root": str(self.task_root),
            "source_root": str(self.source_root),
            "registration": registration,
            "validation": {"status": "registered", "reason": "", "review_key": ""},
        }
        _validate_chain(record)
        _write_record(path, record)
        return _evidence(record, path)

    def load(self, artifact_id: str) -> Evidence:
        path = self._path(artifact_id)
        record = _read_record(path)
        _validate_chain(record)
        return _evidence(record, path)

    def validate_manifest(self, artifact_id: str) -> dict:
        try:
            record = _read_record(self._path(artifact_id))
            _validate_chain(record)
            return {"valid": True, "status": record["validation"]["status"], "errors": []}
        except (ArtifactError, OSError, ValueError, KeyError) as exc:
            return {"valid": False, "status": "invalid", "errors": [str(exc)]}

    def verification_context(self, artifact_id: str) -> dict:
        record = _read_record(self._path(artifact_id))
        _validate_chain(record)
        registration = record["registration"]
        return {
            "artifact_id": artifact_id,
            "instruction": "Separately check source scope, script implementation, actual results, and business/statistical definitions. Successful execution does not prove the conclusion.",
            "sources": registration["sources"],
            "script": registration["script_text"],
            "result": registration["result_text"],
            "methodology": registration["methodology"],
            "conclusion": registration["description"],
            "environment": registration["environment"],
            "intermediate_executions": registration.get("intermediate_executions", []),
        }

    def record_validation(
        self, artifact_id: str, passed: bool, reason: str, review_key: str
    ) -> Evidence:
        if not isinstance(passed, bool) or not reason.strip() or not review_key.strip():
            raise ArtifactError('Derived-result verification must link an explicit conclusion, reason, and independent review call')
        path = self._path(artifact_id)
        with FileLock(str(path) + ".lock"):
            record = _read_record(path)
            _validate_chain(record)
            if passed:
                for source in record["registration"]["sources"]:
                    if error := derived_evidence_error(Evidence.model_validate(source)):
                        raise ArtifactError('Calculation inputs have not been verified: ' + error)
            record["validation"] = {
                "status": "verified" if passed else "rejected",
                "reason": reason,
                "review_key": review_key,
            }
            _write_record(path, record)
        return _evidence(record, path)

    def reuse(
        self, manifest_path: str, *, source_versions: dict[str, str], environment: dict,
        intermediate_environments: dict[str, dict] | None = None,
    ) -> Evidence | None:
        """Reuse only signed, reviewed results with exactly matching inputs/environment."""
        try:
            path = Path(manifest_path)
            record = _read_record(path)
            _validate_chain(record)
            registration = record["registration"]
            if record["validation"]["status"] != "verified":
                return None
            if registration["environment"] != environment:
                return None
            environments = {environment["profile"]: environment} | (intermediate_environments or {})
            for previous in registration.get("intermediate_executions", []):
                if environments.get(previous["environment"]["profile"]) != previous["environment"]:
                    return None
            for item in registration["input_files"]:
                if source_versions.get(item["path"]) != item["sha256"]:
                    return None
                if _file_hash(_safe_path(self.source_root, item["path"])) != item["sha256"]:
                    return None
            return _evidence(record, path)
        except (ArtifactError, OSError, ValueError, KeyError):
            return None
