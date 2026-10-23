"""Disposable Docker execution with durable, host-owned provenance records.

Generated Python is never executed by the host interpreter. The network-enabled
dependency installer runs a fixed, trusted command, never generated Python.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
import weakref
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from filelock import FileLock, Timeout

PROFILES = frozenset({"base", "data", "office", "ocr"})
DEFAULT_IMAGES = {name: f"folio-research-{name}:v2" for name in PROFILES}
DEFAULT_DEPENDENCIES = {
    "networkx": {
        "3.4.2": {
            "sha256": ["df5d4365b724cf81b8c6a7312509d0c22386097011ad1abe274afd5e9d3bbc5f"],
            "dependencies": {},
        }
    }
}
_POOLS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9.!+_-]{0,79}\Z")


class SandboxError(RuntimeError):
    """An explicit infrastructure, policy, or provenance rejection."""

    def __init__(self, message: str, *, code: str = "sandbox_error"):
        super().__init__(message)
        self.code = code


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _safe(root: Path, relative: str, *, exists: bool = True) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or not raw.parts or any(part in {"..", "."} for part in raw.parts):
        raise SandboxError('Paths must be relative to the working directory', code="invalid_path")
    current = root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise SandboxError('Symbolic-link paths are not accepted', code="invalid_path")
    if not current.resolve().is_relative_to(root.resolve()):
        raise SandboxError('The path is outside the task directory', code="invalid_path")
    if exists and not current.exists():
        raise SandboxError(f'File does not exist: {relative}', code="invalid_path")
    return current


class SandboxManager:
    def __init__(self, source_root: Path, task_root: Path, settings: dict | None = None):
        self.source_root = Path(source_root).resolve()
        self.task_root = Path(task_root).resolve()
        self.settings = dict(settings or {})
        self.images = DEFAULT_IMAGES | self.settings.get("profile_images", {})
        self.docker = str(self.settings.get("docker_binary", "docker"))
        self.control = self.task_root / ".research"
        self.executions = self.control / "executions"
        self.scripts = self.control / "scripts"
        self.context = self.task_root / "context"
        self.scratch = self.task_root / "scratch"
        self.output_root = self.task_root / "artifacts"
        for path in (self.executions, self.scripts, self.context, self.scratch, self.output_root):
            _safe(self.task_root, path.relative_to(self.task_root).as_posix(), exists=False)
            path.mkdir(parents=True, exist_ok=True)
        if not self.source_root.is_dir():
            raise SandboxError('The source snapshot directory does not exist', code="sandbox_unavailable")
        self._allow_container_write(self.scratch)
        self._active: set[str] = set()

    @staticmethod
    def _allow_container_write(path: Path) -> None:
        # Root-run services still execute generated code as nobody. Only the
        # explicit writable directories are transferred, never control records.
        if os.getuid() == 0:
            os.chown(path, 65534, 65534)

    def _limit(self, name: str, default: int, *, maximum: int) -> int:
        value = self.settings.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SandboxError(f'Sandbox setting {name} must be numeric', code="invalid_settings")
        if value <= 0 or value > maximum:
            raise SandboxError(f'Sandbox setting {name} is outside the allowed range', code="invalid_settings")
        return int(value)

    def _pool(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if loop not in _POOLS:
            _POOLS[loop] = asyncio.Semaphore(self._limit("max_concurrent", 2, maximum=32))
        return _POOLS[loop]

    async def _command(self, *args: str, timeout: float = 30) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except OSError as exc:
            raise SandboxError(f'Docker is unavailable: {exc}', code="sandbox_unavailable") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def _image(self, profile: str) -> str:
        if profile not in PROFILES:
            raise SandboxError('Unknown research environment profile', code="profile_denied")
        image = self.images[profile]
        if not isinstance(image, str) or not image or image.startswith("-"):
            raise SandboxError('Invalid research image configuration', code="invalid_settings")
        try:
            code, stdout, stderr = await self._command("image", "inspect", "--format", "{{.Id}}", image)
        except TimeoutError as exc:
            raise SandboxError('Docker image lookup timed out', code="sandbox_unavailable") from exc
        image_id = stdout.strip()
        if code or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise SandboxError(
                f'Research environment {profile} is not installed or Docker is unavailable: {stderr.strip()[:500]}',
                code="sandbox_unavailable",
            )
        return image_id

    def _source_manifest(self) -> list[dict]:
        result = []
        for path in sorted(self.source_root.rglob("*")):
            if path.is_symlink():
                raise SandboxError('The source snapshot contains symbolic links', code="invalid_path")
            if path.is_file():
                result.append({"path": path.relative_to(self.source_root).as_posix(), "sha256": _digest(path)})
        return result

    def get_execution(self, execution_id: str) -> dict | None:
        self._execution_id(execution_id)
        path = self.executions / f"{execution_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SandboxError('The execution record is corrupted', code="invalid_execution") from exc

    def verify_execution(self, execution_id: str) -> dict:
        record = self.get_execution(execution_id)
        if not record or record.get("status") != "completed" or record.get("exit_code") != 0:
            raise SandboxError('The execution has not been committed successfully; its outputs cannot be registered', code="invalid_execution")
        script = _safe(self.task_root, record["script_path"])
        if _digest(script) != record.get("script_sha256"):
            raise SandboxError('The executed script does not match the record', code="invalid_execution")
        for item in record.get("artifacts", []):
            path = _safe(self.task_root, item["path"])
            if not path.is_file() or path.stat().st_nlink != 1 or _digest(path) != item["sha256"]:
                raise SandboxError('Execution outputs have changed', code="invalid_execution")
        for item in record.get("source_manifest", []):
            path = _safe(self.source_root, item["path"])
            if not path.is_file() or _digest(path) != item["sha256"]:
                raise SandboxError('The source version used by the execution has changed', code="invalid_execution")
        for item in record.get("input_artifacts", []):
            path = _safe(self.task_root, item["path"])
            if not path.is_file() or path.stat().st_nlink != 1 or _digest(path) != item["sha256"]:
                raise SandboxError('Preceding outputs used by the execution have changed', code="invalid_execution")
        return record

    def _previous_artifacts(self) -> tuple[list[dict], list[tuple[str, Path]]]:
        inputs = []
        mounts = []
        for path in sorted(self.executions.glob("*.json")):
            record = self.get_execution(path.stem)
            if not record or record.get("status") != "completed" or not record.get("artifacts"):
                continue
            verified = self.verify_execution(path.stem)
            directory = _safe(self.task_root, f"artifacts/{path.stem}")
            if self._artifacts(directory) != verified["artifacts"]:
                raise SandboxError('The preceding-output directory does not match the execution manifest', code="invalid_execution")
            mounts.append((path.stem, directory))
            inputs.extend(dict(item, execution_id=path.stem) for item in verified["artifacts"])
        return inputs, mounts

    @staticmethod
    def _execution_id(execution_id: str) -> None:
        if not isinstance(execution_id, str) or not _ID.fullmatch(execution_id):
            raise SandboxError('Invalid execution ID', code="invalid_execution_id")

    def _base_args(self, name: str, *, network: str = "none", memory_mb: int | None = None) -> list[str]:
        uid = os.getuid() or 65534
        gid = os.getgid() or 65534
        memory = memory_mb or self._limit("memory_mb", 512, maximum=16384)
        cpu = float(self.settings.get("cpu_limit", 1))
        if not 0.1 <= cpu <= 32:
            raise SandboxError('The CPU allowance is outside the permitted range', code="invalid_settings")
        return [
            "run", "--rm", "--pull", "never", "--name", name, "--init",
            "--network", network, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", f"{uid}:{gid}",
            "--cpus", str(cpu), "--memory", f"{memory}m", "--memory-swap", f"{memory}m",
            "--pids-limit", str(self._limit("pids_limit", 64, maximum=1024)),
            "--ulimit", "nofile=256:256", "--ulimit", "fsize=134217728:134217728",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=268435456,mode=1777",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONUNBUFFERED=1", "--env", "OMP_NUM_THREADS=1",
            "--env", "OPENBLAS_NUM_THREADS=1",
        ]

    @staticmethod
    def _mount(path: Path, target: str, *, readonly: bool = True) -> list[str]:
        if "," in str(path):
            raise SandboxError('Mount paths cannot contain commas', code="invalid_path")
        mode = ",readonly" if readonly else ""
        return ["--mount", f"type=bind,src={path},dst={target}{mode}"]

    async def _remove(self, name: str) -> None:
        try:
            await self._command("rm", "--force", name, timeout=20)
        except (SandboxError, TimeoutError):
            pass
        self._active.discard(name)

    async def close(self) -> None:
        await asyncio.gather(*(self._remove(name) for name in tuple(self._active)))

    async def cancel_execution(self, execution_id: str) -> dict | None:
        record = self.get_execution(execution_id)
        if record and record.get("status") in {"running", "queued", "interrupted"}:
            if name := record.get("container_name"):
                await self._remove(name)
            record["status"] = "cancelled"
            _json_write(self.executions / f"{execution_id}.json", record)
        return record

    async def environment(self, profile: str = "base") -> dict:
        return {"image_digest": await self._image(profile), "profile": profile,
                "dependencies": self._dependency_lock(profile)}

    async def _run(self, args: list[str], name: str, timeout: float, *, output_dir: Path | None = None) -> dict:
        limit = self._limit("max_output_bytes", 131072, maximum=16777216)
        started = time.monotonic()
        output = [bytearray(), bytearray()]
        too_much = asyncio.Event()
        total = 0
        self._active.add(name)
        # Finish container creation even when cancellation arrives during Docker's
        # create request. Otherwise `rm` can race with the daemon and miss a
        # container that appears immediately afterwards.
        creation = asyncio.create_task(self._command("create", *args[1:]))
        try:
            code, _, stderr = await asyncio.shield(creation)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(creation)
            finally:
                await asyncio.shield(self._remove(name))
            raise
        if code:
            await self._remove(name)
            return {"status": "failed", "exit_code": code, "stdout": "", "stderr": stderr[:limit],
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker, "start", "--attach", name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except BaseException:
            await asyncio.shield(self._remove(name))
            raise

        async def drain(stream: asyncio.StreamReader, index: int) -> None:
            nonlocal total
            while block := await stream.read(16384):
                remaining = max(0, limit - total)
                output[index].extend(block[:remaining])
                total += len(block)
                if total > limit:
                    too_much.set()

        readers = [asyncio.create_task(drain(process.stdout, 0)), asyncio.create_task(drain(process.stderr, 1))]
        waiter = asyncio.create_task(process.wait())
        excessive = asyncio.create_task(too_much.wait())

        async def check_disk() -> None:
            maximum = self._limit("max_total_artifact_bytes", 134217728, maximum=2147483648)
            scratch_max = self._limit("max_scratch_bytes", 268435456, maximum=2147483648)
            count_max = self._limit("max_artifacts", 100, maximum=10000)
            while True:
                if output_dir is not None:
                    for root, ceiling, count_limit in ((output_dir, maximum, count_max), (self.scratch, scratch_max, 10000)):
                        total_size = 0
                        count = 0
                        entries = 0
                        for path in root.rglob("*"):
                            try:
                                entries += 1
                                if entries > max(10000, count_limit) or len(path.relative_to(root).parts) > 32:
                                    return
                                if path.is_symlink():
                                    return
                                if path.is_file():
                                    total_size += path.stat().st_size
                                    count += 1
                                if total_size > ceiling or count > count_limit:
                                    return
                            except FileNotFoundError:
                                continue
                await asyncio.sleep(0.1)

        disk_monitor = asyncio.create_task(check_disk())
        status = "completed"
        try:
            remaining = max(0.001, timeout - (time.monotonic() - started))
            done, _ = await asyncio.wait({waiter, excessive, disk_monitor}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if excessive in done and too_much.is_set():
                status = "output_limit"
            elif disk_monitor in done:
                status = "artifact_limit"
            elif waiter not in done:
                status = "timed_out"
            if status != "completed":
                await self._remove(name)
                if process.returncode is None:
                    process.kill()
            await process.wait()
            await asyncio.gather(*readers)
            if too_much.is_set():
                status = "output_limit"
            if status == "completed" and process.returncode:
                status = "failed"
        except asyncio.CancelledError:
            await asyncio.shield(self._remove(name))
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        finally:
            excessive.cancel()
            for task in readers + [waiter, excessive, disk_monitor]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, waiter, excessive, disk_monitor, return_exceptions=True)
            await asyncio.shield(self._remove(name))
        return {
            "status": status, "exit_code": process.returncode,
            "stdout": output[0].decode(errors="replace"), "stderr": output[1].decode(errors="replace"),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    def _artifacts(self, output_dir: Path) -> list[dict]:
        maximum = self._limit("max_artifact_bytes", 33554432, maximum=1073741824)
        total_max = self._limit("max_total_artifact_bytes", 134217728, maximum=2147483648)
        count_max = self._limit("max_artifacts", 100, maximum=10000)
        items = []
        size = 0
        entries = 0
        for path in output_dir.rglob("*"):
            entries += 1
            if entries > 10000 or len(path.relative_to(output_dir).parts) > 32:
                raise SandboxError('Execution artifact directory depth or count exceeds the limit', code="artifact_limit")
            if path.is_symlink():
                raise SandboxError('Execution artifacts contain symbolic links', code="invalid_artifact")
            if path.is_dir():
                continue
            info = path.stat()
            if not path.is_file() or info.st_nlink != 1:
                raise SandboxError('Execution artifacts must be independent regular files', code="invalid_artifact")
            if info.st_size > maximum or size + info.st_size > total_max or len(items) >= count_max:
                raise SandboxError('Execution artifacts exceed size or count limits', code="artifact_limit")
            size += info.st_size
            items.append({"path": path.relative_to(self.task_root).as_posix(), "size": info.st_size, "sha256": _digest(path)})
        return sorted(items, key=lambda item: item["path"])

    def _dependency_lock(self, profile: str) -> list[dict]:
        path = self.control / f"dependencies-{profile}.json"
        if not path.exists():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        return value["dependencies"]

    def _cache_root(self) -> Path:
        configured = self.settings.get("dependency_cache_root")
        path = Path(configured).expanduser().resolve() if configured else self.control / "dependency_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _cache_key(image_digest: str, dependencies: list[dict]) -> str:
        payload = json.dumps({"image": image_digest, "dependencies": dependencies}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    async def execute(self, code: str, *, execution_id: str, profile: str = "base", timeout_seconds: float | None = None) -> dict:
        self._execution_id(execution_id)
        lock = FileLock(str(self.executions / f"{execution_id}.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise SandboxError('This execution is still running', code="execution_busy") from exc
        try:
            return await self._execute(code, execution_id=execution_id, profile=profile, timeout_seconds=timeout_seconds)
        finally:
            lock.release()

    async def _execute(self, code: str, *, execution_id: str, profile: str = "base", timeout_seconds: float | None = None) -> dict:
        self._execution_id(execution_id)
        if not isinstance(code, str) or not code.strip() or len(code.encode()) > 262144:
            raise SandboxError('The Python script is empty or exceeds 256 KiB', code="invalid_script")
        script_hash = hashlib.sha256(code.encode()).hexdigest()
        existing = self.get_execution(execution_id)
        if existing:
            if existing.get("script_sha256") != script_hash or existing.get("profile") != profile:
                raise SandboxError('The execution ID is already used by another request', code="execution_conflict")
            if existing.get("status") == "completed":
                return self.verify_execution(execution_id)
            if existing.get("status") in {"running", "queued"}:
                # Remove a surviving container before marking an interrupted attempt.
                if name := existing.get("container_name"):
                    await self._remove(name)
                existing["status"] = "interrupted"
                _json_write(self.executions / f"{execution_id}.json", existing)
            return existing
        max_timeout = self._limit("max_timeout_seconds", 300, maximum=3600)
        timeout = timeout_seconds if timeout_seconds is not None else self._limit("timeout_seconds", 60, maximum=max_timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= max_timeout:
            raise SandboxError('The execution timeout allowance is outside the permitted range', code="invalid_timeout")
        image_id = await self._image(profile)
        input_artifacts, previous_mounts = self._previous_artifacts()
        dependencies = self._dependency_lock(profile)
        dependency_dir = None
        if dependencies:
            dependency_dir = self._cache_root() / self._cache_key(image_id, dependencies) / "site-packages"
            if not dependency_dir.is_dir() or not (dependency_dir.parent / "complete.json").is_file():
                raise SandboxError('The dependency cache does not match the current image. Request dependencies again', code="dependency_unavailable")
        script = self.scripts / f"{execution_id}.py"
        script.write_text(code, encoding="utf-8")
        output_dir = self.output_root / execution_id
        name = f"folio-research-{uuid.uuid4().hex}"
        record = {
            "execution_id": execution_id, "status": "queued", "profile": profile,
            "image_digest": image_id, "script_sha256": script_hash,
            "script_path": script.relative_to(self.task_root).as_posix(),
            "source_manifest": self._source_manifest(), "dependencies": dependencies,
            "input_artifacts": input_artifacts,
            "container_name": name, "artifacts": [], "output_files": [],
            "stdout": "", "stderr": "", "exit_code": None, "duration_ms": 0,
        }
        record_path = self.executions / f"{execution_id}.json"
        if output_dir.exists():
            record.update(status="interrupted", stderr='An uncommitted execution directory was found. Retry with a new execution ID')
            _json_write(record_path, record)
            return record
        _json_write(record_path, record)
        output_dir.mkdir(exist_ok=False)
        self._allow_container_write(output_dir)
        args = self._base_args(name)
        args += self._mount(self.source_root, "/sources") + self._mount(self.context, "/context")
        args += self._mount(script, "/research_script.py")
        args += self._mount(self.scratch, "/workspace/scratch", readonly=False)
        args += self._mount(output_dir, "/workspace/artifacts", readonly=False)
        for previous_id, directory in previous_mounts:
            args += self._mount(directory, f"/previous_artifacts/{previous_id}")
        if not previous_mounts:
            empty = self.control / "empty"
            empty.mkdir(exist_ok=True)
            args += self._mount(empty, "/previous_artifacts")
        if dependency_dir:
            args += self._mount(dependency_dir, "/opt/dependencies") + ["--env", "PYTHONPATH=/opt/dependencies"]
        args += ["--env", "FOLIO_SOURCES=/sources", "--env", "FOLIO_CONTEXT=/context",
                 "--env", "FOLIO_ARTIFACTS=/workspace/artifacts", "--env", "FOLIO_SCRATCH=/workspace/scratch",
                 "--env", "FOLIO_OUTPUT_DIR=/workspace/artifacts",
                 "--env", "FOLIO_PREVIOUS_ARTIFACTS=/previous_artifacts",
                 "--workdir", "/workspace/scratch", "--entrypoint", "python", image_id, "/research_script.py"]
        try:
            async with self._pool():
                record["status"] = "running"
                _json_write(record_path, record)
                record.update(await self._run(args, name, float(timeout), output_dir=output_dir))
                current = self.get_execution(execution_id)
                if current and current.get("status") == "cancelled":
                    record["status"] = "cancelled"
            if record["status"] == "completed":
                try:
                    record["artifacts"] = self._artifacts(output_dir)
                    record["output_files"] = record["artifacts"]
                except SandboxError as exc:
                    record.update(status="invalid_artifacts", error_code=exc.code, stderr=str(exc))
        except asyncio.CancelledError:
            await asyncio.shield(self._remove(name))
            record["status"] = "cancelled"
            _json_write(record_path, record)
            raise
        except (OSError, SandboxError) as exc:
            await self._remove(name)
            record.update(status="failed", stderr=str(exc), error_code=getattr(exc, "code", "execution_failed"))
        _json_write(record_path, record)
        return record

    def _approved(self, package: str, version: str, seen: dict[str, str] | None = None) -> list[dict]:
        if not isinstance(package, str) or not _PACKAGE.fullmatch(package) or not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise SandboxError('Dependencies must specify package names and exact versions; URLs, paths, and version ranges are not allowed', code="dependency_denied")
        package = re.sub(r"[-_.]+", "-", package).lower()
        seen = seen if seen is not None else {}
        if package in seen:
            if seen[package] != version:
                raise SandboxError('Dependency versions conflict', code="dependency_denied")
            return []
        seen[package] = version
        allowed = self.settings.get("dependency_allowlist", DEFAULT_DEPENDENCIES)
        entry = allowed.get(package, {}).get(version)
        if not isinstance(entry, dict):
            raise SandboxError(f'The dependency is not approved by operations: {package}=={version}', code="dependency_denied")
        hashes = entry.get("sha256", [])
        if not isinstance(hashes, list) or not hashes or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
            raise SandboxError('The dependency allowlist lacks a valid SHA-256 lock', code="dependency_denied")
        result = [{"package": package, "version": version, "sha256": sorted(set(hashes))}]
        for dependency, required in entry.get("dependencies", {}).items():
            result.extend(self._approved(dependency, required, seen))
        return result

    async def request_dependency(self, package: str, version: str, reason: str, *, profile: str = "base") -> dict:
        lock = FileLock(str(self.control / "dependencies.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise SandboxError('The dependency request is still being processed', code="dependency_busy") from exc
        try:
            return await self._request_dependency(package, version, reason, profile=profile)
        finally:
            lock.release()

    async def _request_dependency(self, package: str, version: str, reason: str, *, profile: str = "base") -> dict:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise SandboxError('A dependency request must explain its purpose', code="dependency_denied")
        approved = self._approved(package, version)
        image_id = await self._image(profile)
        existing = {item["package"]: item for item in self._dependency_lock(profile)}
        for item in approved:
            if item["package"] in existing and existing[item["package"]]["version"] != item["version"]:
                raise SandboxError('This task is already locked to another dependency version', code="dependency_denied")
            existing[item["package"]] = item
        dependencies = sorted(existing.values(), key=lambda item: item["package"])
        index = self.settings.get("trusted_index_url", "https://pypi.org/simple")
        if not isinstance(index, str):
            raise SandboxError('The dependency source must be an HTTPS index', code="invalid_settings")
        parsed = urlsplit(index)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SandboxError('The dependency source must be an HTTPS index configured by operations', code="invalid_settings")
        key = self._cache_key(image_id, dependencies)
        cache_root = self._cache_root()
        cache = cache_root / key
        lock = FileLock(str(cache_root / f"{key}.lock"))
        # Poll without blocking the event loop or leaving a worker holding a lock
        # after cancellation. A different task may be installing this same lock.
        deadline = time.monotonic() + 190
        while True:
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                if time.monotonic() >= deadline:
                    raise SandboxError('Waiting for the dependency cache timed out', code="dependency_busy") from None
                await asyncio.sleep(0.1)
        try:
            if not (cache / "complete.json").is_file():
                staging = cache_root / f".{key}.{uuid.uuid4().hex}"
                staging.mkdir()
                self._allow_container_write(staging)
                requirements = "\n".join(
                    f"{item['package']}=={item['version']} " + " ".join(f"--hash=sha256:{value}" for value in item["sha256"])
                    for item in dependencies
                ) + "\n"
                (staging / "requirements.txt").write_text(requirements, encoding="utf-8")
                name = f"folio-research-deps-{uuid.uuid4().hex}"
                args = self._base_args(name, network=str(self.settings.get("dependency_network", "bridge")))
                args += self._mount(staging, "/install", readonly=False)
                args += ["--workdir", "/install", "--entrypoint", "python", image_id, "-c", _INSTALL_SCRIPT, index]
                try:
                    async with self._pool():
                        result = await self._run(args, name, 180)
                    if result["status"] != "completed":
                        raise SandboxError(f"Controlled dependency installation failed: {result['stderr'][-1500:]}", code="dependency_install_failed")
                    if not (staging / "site-packages").is_dir():
                        raise SandboxError('Dependency installation did not produce a complete environment', code="dependency_install_failed")
                    _json_write(staging / "complete.json", {"image_digest": image_id, "dependencies": dependencies})
                    if cache.exists():
                        shutil.rmtree(cache)
                    staging.replace(cache)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            result = {"status": "approved", "profile": profile, "image_digest": image_id,
                      "dependencies": dependencies, "cache_key": key, "reason": reason.strip()}
            _json_write(self.control / f"dependencies-{profile}.json", result)
            return result
        finally:
            lock.release()

    async def convert(self, operation: str, input_relative: str, *, execution_id: str, profile: str | None = None, pages: list[int] | None = None) -> dict:
        from folio.research.sandbox_conversions import conversion_script

        source = _safe(self.source_root, input_relative)
        if not source.is_file():
            raise SandboxError('Conversion input must be an original source file', code="invalid_path")
        script, expected_profile = conversion_script(operation, input_relative, pages=pages)
        if profile is not None and profile != expected_profile:
            raise SandboxError('The conversion operation does not match the environment profile', code="profile_denied")
        return await self.execute(script, execution_id=execution_id, profile=expected_profile,
                                  timeout_seconds=min(300, self._limit("max_timeout_seconds", 300, maximum=3600)))


_INSTALL_SCRIPT = r"""
import subprocess, sys
base = [sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check', '--no-input', '--no-cache-dir']
subprocess.run(base + ['download', '--only-binary=:all:', '--no-deps', '--require-hashes',
    '--index-url', sys.argv[1], '--dest', '/install/wheels', '-r', '/install/requirements.txt'], check=True)
subprocess.run(base + ['install', '--no-index', '--find-links', '/install/wheels',
    '--only-binary=:all:', '--no-deps', '--require-hashes', '--no-compile',
    '--target', '/install/site-packages', '-r', '/install/requirements.txt'], check=True)
"""
