#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Protocol

import click
import yaml
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console()
ISO = "%Y-%m-%dT%H:%M:%SZ"
PACK_MIN_MB = 512
SNAPSHOT_NAME = "catalog/snapshots/catalog-"
WEEK_SECONDS = 7 * 24 * 60 * 60


class DeepKeepError(RuntimeError):
    pass


class DeepKeepCLI(click.Group):
    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except DeepKeepError as exc:
            raise click.ClickException(str(exc)) from exc


class StorageBackend(Protocol):
    def put_object(self, key: str, path: str) -> None: ...
    def get_object(self, key: str, dest_path: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def list_objects(self, prefix: str) -> list[str]: ...
    def restore_status(self, key: str) -> str: ...
    def request_restore(self, key: str) -> str: ...


@dataclass
class Entry:
    source: Path
    rel_path: str
    member_path: str
    size: int
    sha256: str
    mtime: str

    def manifest(self) -> dict[str, object]:
        return {
            "member_path": self.member_path,
            "original_path": self.rel_path,
            "size": self.size,
            "sha256": self.sha256,
            "mtime": self.mtime,
        }


@dataclass
class StagedPack:
    pack_id: str
    run_id: str
    created_at: str
    object_key: str
    status: str
    tar_path: str
    enc_path: str
    entries: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "pack_id": self.pack_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "object_key": self.object_key,
            "status": self.status,
            "tar_path": self.tar_path,
            "enc_path": self.enc_path,
            "entries": self.entries,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> StagedPack:
        return cls(
            pack_id=str(data["pack_id"]),
            run_id=str(data["run_id"]),
            created_at=str(data["created_at"]),
            object_key=str(data["object_key"]),
            status=str(data["status"]),
            tar_path=str(data["tar_path"]),
            enc_path=str(data["enc_path"]),
            entries=list(data.get("entries", [])),
        )

    def tar_file(self) -> Path:
        return Path(self.tar_path)

    def enc_file(self) -> Path:
        return Path(self.enc_path)

    def state_file(self) -> Path:
        return self.tar_file().parent / "state.json"

    def entry_objects(self) -> list[Entry]:
        return [
            Entry(
                source=Path(str(item["source"])),
                rel_path=str(item["original_path"]),
                member_path=str(item["member_path"]),
                size=int(item["size"]),
                sha256=str(item["sha256"]),
                mtime=str(item["mtime"]),
            )
            for item in self.entries
        ]

    def append_entry(self, entry: Entry) -> None:
        self.entries.append(
            {
                "source": str(entry.source),
                "original_path": entry.rel_path,
                "member_path": entry.member_path,
                "size": entry.size,
                "sha256": entry.sha256,
                "mtime": entry.mtime,
            }
        )


class ProgressReader:
    def __init__(self, fh, callback):
        self.fh = fh
        self.callback = callback

    def read(self, size=-1):
        chunk = self.fh.read(size)
        if chunk:
            self.callback(len(chunk))
        return chunk

    def close(self):
        return self.fh.close()


class PackProgress:
    def __init__(self, pack_label: str, *, compact_backup: bool = False) -> None:
        self.pack_label = pack_label
        self.compact_backup = compact_backup
        self.live_enabled = console.is_terminal
        self.backup_stage_durations: dict[str, str] = {}

    def _finish(self, stage: str, started_at: float, ok: bool = True) -> None:
        duration = self._format_duration(time.monotonic() - started_at)
        if self.compact_backup and ok and stage in {"build", "encrypt", "upload"}:
            self.backup_stage_durations[stage] = duration
            if stage == "upload":
                console.print(
                    f"pack {self.pack_label:<8} "
                    f"b {self.backup_stage_durations.get('build', '--:--')} "
                    f"e {self.backup_stage_durations.get('encrypt', '--:--')} "
                    f"u {self.backup_stage_durations.get('upload', '--:--')}"
                )
            return
        outcome = "finished" if ok else "failed"
        console.print(f"pack {self.pack_label:<8} {stage:<9} {outcome:<8} {duration}")

    def _format_duration(self, seconds: float) -> str:
        total = int(seconds)
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def build(self, total_bytes: int):
        if not self.live_enabled:
            return _NullBarProgress(self, "build")
        progress = Progress(
            TextColumn(f"pack {self.pack_label}"),
            TextColumn("building"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        task_id = progress.add_task("building", total=max(total_bytes, 1), completed=0)
        return _BarProgress(self, "build", progress, task_id)

    def restore(self, total_bytes: int):
        if not self.live_enabled:
            return _NullBarProgress(self, "restore")
        progress = Progress(
            TextColumn(f"pack {self.pack_label}"),
            TextColumn("restoring"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        task_id = progress.add_task("restoring", total=max(total_bytes, 1), completed=0)
        return _BarProgress(self, "restore", progress, task_id)

    @contextmanager
    def stage(self, stage: str):
        started_at = time.monotonic()
        if self.live_enabled:
            progress = Progress(
                SpinnerColumn(),
                TextColumn(f"pack {self.pack_label}"),
                TextColumn(stage),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            )
            task_id = progress.add_task(stage, total=None)
            with progress:
                try:
                    yield
                except Exception:
                    self._finish(stage, started_at, ok=False)
                    raise
        else:
            try:
                yield
            except Exception:
                self._finish(stage, started_at, ok=False)
                raise
        self._finish(stage, started_at, ok=True)


class _NullBarProgress:
    def __init__(self, owner: PackProgress, stage: str) -> None:
        self.owner = owner
        self.stage = stage
        self.started_at = 0.0

    def __enter__(self):
        self.started_at = time.monotonic()
        return self

    def advance(self, amount: int) -> None:
        return

    def __exit__(self, exc_type, exc, tb):
        self.owner._finish(self.stage, self.started_at, ok=exc is None)
        return False


class _BarProgress:
    def __init__(self, owner: PackProgress, stage: str, progress: Progress, task_id: int) -> None:
        self.owner = owner
        self.stage = stage
        self.progress = progress
        self.task_id = task_id
        self.started_at = 0.0

    def __enter__(self):
        self.started_at = time.monotonic()
        self.progress.start()
        return self

    def advance(self, amount: int) -> None:
        self.progress.advance(self.task_id, amount)

    def __exit__(self, exc_type, exc, tb):
        self.progress.stop()
        self.owner._finish(self.stage, self.started_at, ok=exc is None)
        return False


class ScanProgress:
    def __init__(self, total_bytes: int, phase: str = "source scan") -> None:
        self.total_bytes = total_bytes
        self.phase = phase
        self.live_enabled = console.is_terminal
        self.progress = None
        self.task_id = None

    def __enter__(self):
        if self.live_enabled:
            self.progress = Progress(
                TextColumn("overall"),
                TextColumn(self.phase),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            )
            self.task_id = self.progress.add_task(self.phase, total=max(self.total_bytes, 1), completed=0)
            self.progress.start()
        return self

    def advance(self, amount: int) -> None:
        if self.progress is not None and self.task_id is not None:
            self.progress.advance(self.task_id, amount)

    def suspend(self) -> None:
        if self.progress is not None:
            self.progress.stop()

    def resume(self) -> None:
        if self.progress is not None:
            self.progress.start()

    def __exit__(self, exc_type, exc, tb):
        if self.progress is not None:
            self.progress.stop()
        return False


def utc_now() -> str:
    return datetime.now(UTC).strftime(ISO)


def parse_utc(value: str) -> datetime:
    return datetime.strptime(value, ISO).replace(tzinfo=UTC)


def parse_snapshot_key(key: str) -> datetime | None:
    prefix = SNAPSHOT_NAME
    suffix = ".sqlite.age"
    if not key.startswith(prefix) or not key.endswith(suffix):
        return None
    raw = key[len(prefix) : -len(suffix)]
    if len(raw) != 16 or not raw.endswith("Z"):
        return None
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def should_write_catalog_snapshot(now: str, snapshot_keys: list[str]) -> bool:
    current = parse_utc(now)
    latest = max((parsed for key in snapshot_keys if (parsed := parse_snapshot_key(key)) is not None), default=None)
    if latest is None:
        return True
    return (current - latest).total_seconds() >= WEEK_SECONDS


def is_linux_platform() -> bool:
    return sys.platform.startswith("linux")


def is_windows_platform() -> bool:
    return os.name == "nt"


def apply_mtime(path: Path, mtime: str | None) -> None:
    if not mtime:
        return
    ts = parse_utc(str(mtime)).timestamp()
    os.utime(path, (ts, ts))


def remove_existing_target(path: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()


def write_pointer_file(target: Path, canonical_rel: str, canonical_target: Path, sha256: str, mtime: str | None) -> None:
    target.write_text(
        "\n".join(
            [
                "deepkeep duplicate placeholder",
                f"original: {canonical_rel}",
                f"resolved_to: {canonical_target}",
                f"sha256: {sha256}",
            ]
        )
        + "\n"
    )
    apply_mtime(target, mtime)


def write_full_copy(target: Path, canonical_target: Path, mtime: str | None) -> None:
    shutil.copyfile(canonical_target, target)
    apply_mtime(target, mtime)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            text=True,
            input=input_text,
            capture_output=True,
            check=check,
            env=env,
            pass_fds=pass_fds,
        )
    except subprocess.CalledProcessError as exc:
        raise DeepKeepError(format_subprocess_error(exc)) from exc


def format_subprocess_error(exc: subprocess.CalledProcessError) -> str:
    lines = [
        f"command failed with exit code {exc.returncode}",
        f"command: {shlex.join(str(part) for part in exc.cmd)}",
    ]
    stdout = (exc.stdout or "").strip()
    stderr = (exc.stderr or "").strip()
    if stdout:
        lines.append("stdout:")
        lines.append(stdout)
    if stderr:
        lines.append("stderr:")
        lines.append(stderr)
    return "\n".join(lines)


def wrap_error(summary: str, exc: Exception) -> DeepKeepError:
    details = str(exc).strip()
    if not details:
        return DeepKeepError(summary)
    return DeepKeepError(f"{summary}\n{details}")


def error_note(exc: Exception) -> str:
    message = str(exc).strip()
    return message.splitlines()[0] if message else "backup failed"


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{value} B"


def format_local_timestamp(moment: datetime | None = None) -> str:
    value = datetime.now().astimezone() if moment is None else moment.astimezone()
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def format_duration(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_int(value: int) -> str:
    return f"{value:,}"


def require_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise DeepKeepError(f"required executable not found on PATH: {name}")


def expand_config_path(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def load_config(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise DeepKeepError("config must be a YAML mapping")
    data.setdefault("pack_size_mb", PACK_MIN_MB)
    data.setdefault("catalog_path", str(path.with_suffix(".sqlite")))
    data.setdefault("work_root", str(path.parent / ".deepkeep-work"))
    data["catalog_path"] = expand_config_path(str(data["catalog_path"]))
    data["work_root"] = expand_config_path(str(data["work_root"]))
    if "age_pass_entry" not in data:
        raise DeepKeepError("config must define age_pass_entry")
    cfg = data.get("backend")
    if not isinstance(cfg, dict):
        raise DeepKeepError("config.backend must be a mapping")
    backend_type = cfg.get("type")
    if backend_type == "local":
        if "root" not in cfg:
            raise DeepKeepError("config.backend.root is required for local backend")
        cfg["root"] = expand_config_path(str(cfg["root"]))
    elif backend_type == "s3":
        for key in ("bucket", "prefix"):
            if key not in cfg:
                raise DeepKeepError(f"config.backend.{key} is required for s3 backend")
        cfg.setdefault("storage_class", "DEEP_ARCHIVE")
    else:
        raise DeepKeepError("config.backend.type must be 'local' or 's3'")
    return data


def load_passphrase(config: dict[str, object]) -> str:
    require_tool("pass")
    entry = str(config["age_pass_entry"])
    proc = run(["pass", "show", entry])
    value = proc.stdout.splitlines()[0].strip() if proc.stdout else ""
    if not value:
        raise DeepKeepError(f"pass entry is empty: {entry}")
    return value


def run_age_with_batchpass(args: list[str], passphrase: str) -> subprocess.CompletedProcess[str]:
    require_tool("age")
    require_tool("age-plugin-batchpass")
    rfd, wfd = os.pipe()
    try:
        os.write(wfd, f"{passphrase}\n".encode())
        os.close(wfd)
        env = os.environ.copy()
        env["AGE_PASSPHRASE_FD"] = str(rfd)
        return run(args, check=False, env=env, pass_fds=(rfd,))
    finally:
        try:
            os.close(rfd)
        except OSError:
            pass


def encrypt_file(src: Path, dest: Path, config: dict[str, object]) -> None:
    passphrase = load_passphrase(config)
    proc = run_age_with_batchpass(["age", "--encrypt", "-j", "batchpass", "-o", str(dest), str(src)], passphrase)
    if proc.returncode != 0:
        raise DeepKeepError(proc.stderr.strip() or "age encryption failed")


def decrypt_file(src: Path, dest: Path, config: dict[str, object]) -> None:
    passphrase = load_passphrase(config)
    proc = run_age_with_batchpass(["age", "--decrypt", "-j", "batchpass", "-o", str(dest), str(src)], passphrase)
    if proc.returncode != 0:
        raise DeepKeepError(proc.stderr.strip() or "age decryption failed")


def connect_db(config: dict[str, object]) -> sqlite3.Connection:
    catalog_path = Path(str(config["catalog_path"]))
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(catalog_path))
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            sha256 TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            pack_id TEXT NOT NULL,
            tar_path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS packs (
            pack_id TEXT PRIMARY KEY,
            object_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            compression TEXT NOT NULL,
            encryption TEXT NOT NULL,
            uploaded INTEGER NOT NULL,
            run_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS file_paths (
            path TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL,
            mtime TEXT
        );
        CREATE TABLE IF NOT EXISTS backup_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            machine TEXT,
            source_path TEXT NOT NULL,
            files_scanned INTEGER NOT NULL DEFAULT 0,
            files_new INTEGER NOT NULL DEFAULT 0,
            files_deduped INTEGER NOT NULL DEFAULT 0,
            bytes_new INTEGER NOT NULL DEFAULT 0,
            bytes_total_scanned INTEGER NOT NULL DEFAULT 0,
            packs_created INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS run_files (
            run_id TEXT NOT NULL,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime TEXT,
            pack_id TEXT,
            PRIMARY KEY (run_id, path)
        );
        CREATE TABLE IF NOT EXISTS path_versions (
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            mtime TEXT,
            run_id TEXT NOT NULL,
            pack_id TEXT,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (run_id, path)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_run_files_run_id ON run_files(run_id);
        CREATE INDEX IF NOT EXISTS idx_run_files_pack_id ON run_files(pack_id);
        CREATE INDEX IF NOT EXISTS idx_path_versions_path_recorded ON path_versions(path, recorded_at);
        """
    )
    db.commit()
    return db


def sqlite_check_issues(db: sqlite3.Connection, pragma: str) -> list[str]:
    rows = db.execute(f"PRAGMA {pragma}").fetchall()
    issues = [str(row[0]) for row in rows if row and str(row[0]) != "ok"]
    return issues


def quick_validate_catalog(db: sqlite3.Connection) -> None:
    issues = sqlite_check_issues(db, "quick_check")
    if issues:
        raise DeepKeepError(f"catalog quick check failed\n" + "\n".join(issues))


def catalog_reference_issues(db: sqlite3.Connection) -> list[str]:
    queries = [
        (
            "file_paths reference missing files rows",
            "SELECT COUNT(*) FROM file_paths fp LEFT JOIN files f ON f.sha256 = fp.sha256 WHERE f.sha256 IS NULL",
        ),
        (
            "files reference missing packs rows",
            "SELECT COUNT(*) FROM files f LEFT JOIN packs p ON p.pack_id = f.pack_id WHERE p.pack_id IS NULL",
        ),
        (
            "run_files reference missing backup_runs rows",
            "SELECT COUNT(*) FROM run_files rf LEFT JOIN backup_runs br ON br.run_id = rf.run_id WHERE br.run_id IS NULL",
        ),
        (
            "run_files reference missing files rows",
            "SELECT COUNT(*) FROM run_files rf LEFT JOIN files f ON f.sha256 = rf.sha256 WHERE f.sha256 IS NULL",
        ),
        (
            "run_files reference missing packs rows",
            "SELECT COUNT(*) FROM run_files rf LEFT JOIN packs p ON p.pack_id = rf.pack_id WHERE rf.pack_id IS NOT NULL AND p.pack_id IS NULL",
        ),
        (
            "path_versions reference missing backup_runs rows",
            "SELECT COUNT(*) FROM path_versions pv LEFT JOIN backup_runs br ON br.run_id = pv.run_id WHERE br.run_id IS NULL",
        ),
        (
            "path_versions reference missing files rows",
            "SELECT COUNT(*) FROM path_versions pv LEFT JOIN files f ON f.sha256 = pv.sha256 WHERE f.sha256 IS NULL",
        ),
        (
            "path_versions reference missing packs rows",
            "SELECT COUNT(*) FROM path_versions pv LEFT JOIN packs p ON p.pack_id = pv.pack_id WHERE pv.pack_id IS NOT NULL AND p.pack_id IS NULL",
        ),
    ]
    issues: list[str] = []
    for label, query in queries:
        count = int(db.execute(query).fetchone()[0])
        if count:
            issues.append(f"{label}: {count}")
    return issues


def verify_catalog(config: dict[str, object]) -> list[str]:
    db = connect_db(config)
    try:
        return sqlite_check_issues(db, "integrity_check") + catalog_reference_issues(db)
    finally:
        db.close()


def catalog_backend_label(config: dict[str, object]) -> str:
    db = connect_db(config)
    try:
        row = db.execute("SELECT value FROM settings WHERE key = ?", ("catalog_backend_label",)).fetchone()
        return current_backend_label(config) if row is None else str(row["value"])
    finally:
        db.close()


def backend_identity(config: dict[str, object]) -> str:
    return json.dumps(config["backend"], sort_keys=True, separators=(",", ":"))


def configured_backend_label(config: dict[str, object]) -> str:
    cfg = config["backend"]
    if cfg["type"] == "local":
        return f"local:{cfg['root']}"
    return f"s3:{cfg['bucket']}/{cfg['prefix']}"


def stored_backend_identity(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", ("catalog_backend_identity",)).fetchone()
    return None if row is None else str(row["value"])


def bind_or_validate_backend_identity(db: sqlite3.Connection, config: dict[str, object]) -> None:
    current = backend_identity(config)
    stored = stored_backend_identity(db)
    if stored is None:
        db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            ("catalog_backend_identity", current),
        )
        db.commit()
        return
    if stored != current:
        row = db.execute("SELECT value FROM settings WHERE key = ?", ("catalog_backend_label",)).fetchone()
        stored_label = row["value"] if row is not None else "configured backend"
        raise DeepKeepError(
            f"catalog is bound to a different backend\n"
            f"catalog backend: {stored_label}\n"
            f"configured backend: {configured_backend_label(config)}"
        )


def store_backend_label(db: sqlite3.Connection, config: dict[str, object]) -> None:
    db.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        ("catalog_backend_label", configured_backend_label(config)),
    )
    db.commit()


def upsert_file_path(db: sqlite3.Connection, entry: Entry) -> None:
    db.execute(
        "INSERT OR REPLACE INTO file_paths(path, sha256, mtime) VALUES (?, ?, ?)",
        (entry.rel_path, entry.sha256, entry.mtime),
    )


def current_path_row(db: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return db.execute("SELECT sha256, mtime FROM file_paths WHERE path = ?", (path,)).fetchone()


def has_hash(db: sqlite3.Connection, sha256: str) -> bool:
    return db.execute("SELECT 1 FROM files WHERE sha256 = ?", (sha256,)).fetchone() is not None


def lookup_file_blob(db: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return db.execute("SELECT pack_id, size FROM files WHERE sha256 = ?", (sha256,)).fetchone()


def should_record_path_version(previous: sqlite3.Row | None, entry: Entry) -> bool:
    return previous is None or previous["sha256"] != entry.sha256


def record_path_version(
    db: sqlite3.Connection,
    *,
    path: str,
    sha256: str,
    mtime: str | None,
    run_id: str,
    recorded_at: str,
    pack_id: str | None,
) -> None:
    db.execute(
        "INSERT OR REPLACE INTO path_versions(path, sha256, mtime, run_id, pack_id, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
        (path, sha256, mtime, run_id, pack_id, recorded_at),
    )


def iter_files(source: Path) -> Iterable[Path]:
    for path in sorted(source.iterdir(), key=lambda item: item.name):
        if path.is_dir():
            yield from iter_files(path)
        elif path.is_file():
            yield path


def build_entry(source_root: Path, path: Path) -> Entry:
    rel = path.relative_to(source_root).as_posix()
    stat = path.stat()
    return Entry(
        source=path,
        rel_path=rel,
        member_path=f"files/{rel}",
        size=stat.st_size,
        sha256=sha256_file(path),
        mtime=datetime.fromtimestamp(stat.st_mtime, UTC).strftime(ISO),
    )


def make_manifest(entries: list[Entry], created_at: str) -> dict[str, object]:
    return {
        "format_version": 1,
        "created_at": created_at,
        "files": [entry.manifest() for entry in entries],
    }


def write_tar(tar_path: Path, manifest: dict[str, object], entries: list[Entry], on_progress=None) -> None:
    with tarfile.open(tar_path, "w") as tf:
        data = json.dumps(manifest, indent=2, sort_keys=True).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(data)
        info.mtime = int(datetime.now(UTC).timestamp())
        tf.addfile(info, io.BytesIO(data))
        for entry in entries:
            with entry.source.open("rb") as fh:
                tarinfo = tf.gettarinfo(str(entry.source), arcname=entry.member_path)
                tf.addfile(tarinfo, fileobj=ProgressReader(fh, on_progress or (lambda _: None)))


def pack_object_key(pack_id: str, created_at: str) -> str:
    dt = datetime.strptime(created_at, ISO)
    return f"packs/{dt:%Y/%m}/pack-{pack_id}.tar.age"


def catalog_object_keys(ts: str) -> tuple[str, str]:
    return "catalog/latest.sqlite.age", f"catalog/snapshots/catalog-{ts.replace(':', '').replace('-', '')}.sqlite.age"


def read_stage(path: Path) -> StagedPack:
    return StagedPack.from_dict(json.loads(path.read_text()))


def write_stage(path: Path, data: StagedPack) -> None:
    path.write_text(json.dumps(data.as_dict(), indent=2, sort_keys=True))


class LocalBackend:
    def __init__(self, backend_name: str, root: Path) -> None:
        self.backend_name = backend_name
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key

    def put_object(self, key: str, path: str) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    def get_object(self, key: str, dest_path: str) -> None:
        src = self._path(key)
        if not src.exists():
            raise DeepKeepError(f"object not found: {key}")
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_path)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list_objects(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if base.is_file():
            return [prefix]
        if not base.exists():
            return []
        return [p.relative_to(self.root).as_posix() for p in sorted(base.rglob("*")) if p.is_file()]

    def restore_status(self, key: str) -> str:
        return "ready" if self.exists(key) else "missing"

    def request_restore(self, key: str) -> str:
        return self.restore_status(key)


class S3Backend:
    def __init__(self, backend_name: str, cfg: dict[str, object]) -> None:
        self.backend_name = backend_name
        self.bucket = str(cfg["bucket"])
        self.prefix = str(cfg["prefix"]).strip("/")
        self.storage_class = str(cfg.get("storage_class", "DEEP_ARCHIVE"))
        require_tool("aws")

    def _uri(self, key: str) -> str:
        full = f"{self.prefix}/{key}".strip("/")
        return f"s3://{self.bucket}/{full}"

    def put_object(self, key: str, path: str) -> None:
        run(
            [
                "aws",
                "s3",
                "cp",
                path,
                self._uri(key),
                "--storage-class",
                self.storage_class,
            ]
        )

    def get_object(self, key: str, dest_path: str) -> None:
        run(["aws", "s3", "cp", self._uri(key), dest_path])

    def exists(self, key: str) -> bool:
        proc = run(["aws", "s3", "ls", self._uri(key)], check=False)
        return proc.returncode == 0

    def list_objects(self, prefix: str) -> list[str]:
        uri = self._uri(prefix)
        proc = run(["aws", "s3", "ls", uri, "--recursive"], check=False)
        if proc.returncode != 0:
            return []
        keys = []
        base = f"{self.prefix}/".strip("/")
        for line in proc.stdout.splitlines():
            parts = line.split()
            if parts:
                key = parts[-1]
                if key.startswith(base):
                    key = key[len(base):].lstrip("/")
                keys.append(key)
        return keys

    def restore_status(self, key: str) -> str:
        proc = run(["aws", "s3api", "head-object", "--bucket", self.bucket, "--key", f"{self.prefix}/{key}"])
        meta = json.loads(proc.stdout or "{}")
        storage_class = str(meta.get("StorageClass", "STANDARD") or "STANDARD")
        if storage_class not in {"GLACIER", "DEEP_ARCHIVE", "GLACIER_IR"}:
            return "ready"
        hdr = str(meta.get("Restore", ""))
        if 'ongoing-request="false"' in hdr:
            return "ready"
        if 'ongoing-request="true"' in hdr:
            return "pending"
        return "cold"

    def request_restore(self, key: str) -> str:
        status = self.restore_status(key)
        if status in {"ready", "pending"}:
            return status
        run(
            [
                "aws",
                "s3api",
                "restore-object",
                "--bucket",
                self.bucket,
                "--key",
                f"{self.prefix}/{key}",
                "--restore-request",
                '{"Days":7,"GlacierJobParameters":{"Tier":"Standard"}}',
            ],
            check=False,
        )
        return "pending"


def get_backend(config: dict[str, object]) -> StorageBackend:
    cfg = config["backend"]
    if cfg["type"] == "local":
        return LocalBackend("local", Path(cfg["root"]))
    return S3Backend("s3", cfg)


def current_backend_label(config: dict[str, object]) -> str:
    return configured_backend_label(config)


def stage_root(config: dict[str, object]) -> Path:
    root = Path(str(config["work_root"]))
    root.mkdir(parents=True, exist_ok=True)
    return root


def pending_stages(config: dict[str, object]) -> list[StagedPack]:
    return [read_stage(path) for path in sorted(stage_root(config).glob("packs/*/state.json"))]


def resumable_stage_hashes(config: dict[str, object]) -> set[str]:
    hashes: set[str] = set()
    for stage in pending_stages(config):
        if stage.status not in {"ENCRYPTED", "UPLOADED"}:
            continue
        for item in stage.entries:
            hashes.add(str(item["sha256"]))
    return hashes


def update_backup_run(
    db: sqlite3.Connection,
    run_id: str,
    stats: dict[str, int],
    *,
    status: str,
    notes: str | None = None,
) -> None:
    db.execute(
        """
        UPDATE backup_runs
        SET completed_at = ?, files_scanned = ?, files_new = ?, files_deduped = ?, bytes_new = ?,
            bytes_total_scanned = ?, packs_created = ?, status = ?, notes = ?
        WHERE run_id = ?
        """,
        (
            utc_now(),
            stats["files_scanned"],
            stats["files_new"],
            stats["files_deduped"],
            stats["bytes_new"],
            stats["bytes_total_scanned"],
            stats["packs_created"],
            status,
            notes,
            run_id,
        ),
    )
    db.commit()


def run_has_committed_packs(db: sqlite3.Connection, run_id: str) -> bool:
    row = db.execute("SELECT 1 FROM packs WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
    return row is not None


def fail_stale_runs(db: sqlite3.Connection) -> None:
    for row in db.execute("SELECT run_id FROM backup_runs WHERE status = ?", ("RUNNING",)).fetchall():
        status = "PARTIAL" if run_has_committed_packs(db, row["run_id"]) else "FAILED"
        db.execute(
            """
            UPDATE backup_runs
            SET completed_at = COALESCE(completed_at, ?), status = ?, notes = COALESCE(notes, ?)
            WHERE run_id = ?
            """,
            (utc_now(), status, "marked incomplete after a later backup detected an unfinished run", row["run_id"]),
        )
    db.commit()


def snapshot_catalog(config: dict[str, object], backend: StorageBackend) -> None:
    catalog = Path(str(config["catalog_path"]))
    if not catalog.exists():
        return
    ts = utc_now()
    with tempfile.TemporaryDirectory() as tmp:
        enc = Path(tmp) / "catalog.sqlite.age"
        try:
            encrypt_file(catalog, enc, config)
        except Exception as exc:
            raise wrap_error("catalog snapshot failed", exc) from exc
        latest_key, snap_key = catalog_object_keys(ts)
        try:
            backend.put_object(latest_key, str(enc))
            if should_write_catalog_snapshot(ts, backend.list_objects("catalog/snapshots")):
                backend.put_object(snap_key, str(enc))
        except Exception as exc:
            raise wrap_error("catalog snapshot failed", exc) from exc


def commit_pack(db: sqlite3.Connection, stage: StagedPack) -> None:
    db.execute(
        "INSERT OR REPLACE INTO packs(pack_id, object_key, created_at, compression, encryption, uploaded, run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            stage.pack_id,
            stage.object_key,
            stage.created_at,
            "off",
            "age",
            1,
            stage.run_id,
        ),
    )
    for item in stage.entries:
        db.execute(
            "INSERT OR IGNORE INTO files(sha256, size, pack_id, tar_path) VALUES (?, ?, ?, ?)",
            (item["sha256"], item["size"], stage.pack_id, item["member_path"]),
        )
        db.execute(
            "INSERT OR REPLACE INTO file_paths(path, sha256, mtime) VALUES (?, ?, ?)",
            (item["original_path"], item["sha256"], item.get("mtime")),
        )
        db.execute(
            "INSERT OR REPLACE INTO run_files(run_id, path, sha256, size, mtime, pack_id) VALUES (?, ?, ?, ?, ?, ?)",
            (stage.run_id, item["original_path"], item["sha256"], item["size"], item.get("mtime"), stage.pack_id),
        )
        record_path_version(
            db,
            path=item["original_path"],
            sha256=item["sha256"],
            mtime=item.get("mtime"),
            run_id=stage.run_id,
            recorded_at=stage.created_at,
            pack_id=stage.pack_id,
        )
    db.commit()


def resume_pending(config: dict[str, object], db: sqlite3.Connection, backend: StorageBackend) -> int:
    committed = 0
    for state_path in sorted(stage_root(config).glob("packs/*/state.json")):
        stage = read_stage(state_path)
        enc = stage.enc_file()
        progress = PackProgress(stage.pack_id)
        if stage.status == "ENCRYPTED":
            with progress.stage("upload"):
                backend.put_object(stage.object_key, str(enc))
            stage.status = "UPLOADED"
            write_stage(state_path, stage)
        if stage.status == "UPLOADED":
            with progress.stage("commit"):
                commit_pack(db, stage)
            stage.status = "COMMITTED"
            write_stage(state_path, stage)
            shutil.rmtree(state_path.parent)
            committed += 1
    return committed


def new_pack_state(config: dict[str, object], run_id: str) -> tuple[StagedPack, Path]:
    pack_id = uuid.uuid4().hex[:12]
    created_at = utc_now()
    object_key = pack_object_key(pack_id, created_at)
    pack_dir = stage_root(config) / "packs" / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    state = StagedPack(
        pack_id=pack_id,
        run_id=run_id,
        created_at=created_at,
        object_key=object_key,
        status="BUILDING",
        tar_path=str(pack_dir / "pack.tar"),
        enc_path=str(pack_dir / "pack.tar.age"),
        entries=[],
    )
    return state, pack_dir


def seal_pack(config: dict[str, object], db: sqlite3.Connection, backend: StorageBackend, state: StagedPack, pack_label: str) -> None:
    entries = state.entry_objects()
    manifest = make_manifest(entries, state.created_at)
    tar_path = state.tar_file()
    enc_path = state.enc_file()
    progress = PackProgress(pack_label, compact_backup=True)
    try:
        with progress.build(sum(entry.size for entry in entries)) as build_progress:
            write_tar(tar_path, manifest, entries, on_progress=build_progress.advance)
    except Exception as exc:
        raise wrap_error("pack build failed", exc) from exc
    write_stage(state.state_file(), state)
    try:
        with progress.stage("encrypt"):
            encrypt_file(tar_path, enc_path, config)
    except Exception as exc:
        raise wrap_error("pack encryption failed", exc) from exc
    state.status = "ENCRYPTED"
    write_stage(state.state_file(), state)
    try:
        with progress.stage("upload"):
            backend.put_object(state.object_key, str(enc_path))
    except Exception as exc:
        raise wrap_error("pack upload failed", exc) from exc
    state.status = "UPLOADED"
    write_stage(state.state_file(), state)
    try:
        commit_pack(db, state)
    except Exception as exc:
        raise wrap_error("pack commit failed", exc) from exc
    shutil.rmtree(tar_path.parent)


def backup_source(config: dict[str, object], source: Path, dry_run: bool = False) -> dict[str, int]:
    db = connect_db(config)
    if dry_run:
        planned = plan_backup(db, config, source)
        db.close()
        return planned
    prescan = pre_scan_source(config, source)

    backend = get_backend(config)
    bind_or_validate_backend_identity(db, config)
    store_backend_label(db, config)
    unfinished = pending_stages(config)
    pack_total = prescan["packs_estimated"]
    if unfinished:
        console.print("Found unfinished backup state. Running a dedupe scan for an exact remaining pack estimate.")
        planned = plan_backup(
            db,
            config,
            source,
            existing_hashes=resumable_stage_hashes(config),
            progress_total=prescan["bytes_total_scanned"],
        )
        pack_total = planned["packs_created"]
    resume_pending(config, db, backend)
    fail_stale_runs(db)
    run_id, started = start_backup_run(db, source)
    stats = empty_backup_stats()
    target = int(config["pack_size_mb"]) * 1024 * 1024
    state, _ = new_pack_state(config, run_id)
    current_size = 0
    run_hashes: set[str] = set()
    next_pack_number = 1
    try:
        with ScanProgress(prescan["bytes_total_scanned"]) as scan_progress:
            for path in iter_files(source):
                entry = build_entry(source, path)
                scan_progress.advance(entry.size)
                previous = current_path_row(db, entry.rel_path)
                stats["files_scanned"] += 1
                stats["bytes_total_scanned"] += entry.size
                upsert_file_path(db, entry)
                if entry.sha256 in run_hashes or has_hash(db, entry.sha256):
                    handle_deduped_entry(db, run_id, started, stats, previous, entry)
                    continue
                current_size += append_new_entry(state, entry, stats, run_hashes)
                if current_size >= target:
                    state, next_pack_number = finalize_stage_pack(
                        config,
                        db,
                        backend,
                        state,
                        stats,
                        scan_progress,
                        next_pack_number,
                        pack_total,
                    )
                    current_size = 0
            if state.entries:
                scan_progress.suspend()
                try:
                    stats["packs_created"] += 1
                    seal_pack(config, db, backend, state, f"{next_pack_number}/{pack_total}")
                finally:
                    scan_progress.resume()
        quick_validate_catalog(db)
        snapshot_catalog(config, backend)
        update_backup_run(db, run_id, stats, status="COMPLETED")
        return stats
    except Exception as exc:
        status = "PARTIAL" if run_has_committed_packs(db, run_id) else "FAILED"
        update_backup_run(db, run_id, stats, status=status, notes=error_note(exc))
        raise
    finally:
        db.close()


def plan_backup(
    db: sqlite3.Connection,
    config: dict[str, object],
    source: Path,
    *,
    existing_hashes: set[str] | None = None,
    progress_total: int | None = None,
    progress_phase: str = "dedupe scan",
) -> dict[str, int]:
    stats = {"files_scanned": 0, "files_new": 0, "files_deduped": 0, "bytes_new": 0, "bytes_total_scanned": 0, "packs_created": 0}
    target = int(config["pack_size_mb"]) * 1024 * 1024
    current_size = 0
    run_hashes: set[str] = set(existing_hashes or set())
    if progress_total is None:
        for path in iter_files(source):
            entry = build_entry(source, path)
            stats["files_scanned"] += 1
            stats["bytes_total_scanned"] += entry.size
            if entry.sha256 in run_hashes or has_hash(db, entry.sha256):
                stats["files_deduped"] += 1
                continue
            stats["files_new"] += 1
            stats["bytes_new"] += entry.size
            run_hashes.add(entry.sha256)
            current_size += entry.size
            if current_size >= target:
                stats["packs_created"] += 1
                current_size = 0
    else:
        with ScanProgress(progress_total, phase=progress_phase) as scan_progress:
            for path in iter_files(source):
                entry = build_entry(source, path)
                scan_progress.advance(entry.size)
                stats["files_scanned"] += 1
                stats["bytes_total_scanned"] += entry.size
                if entry.sha256 in run_hashes or has_hash(db, entry.sha256):
                    stats["files_deduped"] += 1
                    continue
                stats["files_new"] += 1
                stats["bytes_new"] += entry.size
                run_hashes.add(entry.sha256)
                current_size += entry.size
                if current_size >= target:
                    stats["packs_created"] += 1
                    current_size = 0
    if current_size > 0:
        stats["packs_created"] += 1
    return stats


def pre_scan_source(config: dict[str, object], source: Path) -> dict[str, int]:
    total_files = 0
    total_bytes = 0
    for path in iter_files(source):
        stat = path.stat()
        total_files += 1
        total_bytes += stat.st_size
    target = int(config["pack_size_mb"]) * 1024 * 1024
    packs_estimated = max(1, math.ceil(total_bytes / target)) if total_bytes else 0
    return {"files_scanned": total_files, "bytes_total_scanned": total_bytes, "packs_estimated": packs_estimated}


def empty_backup_stats() -> dict[str, int]:
    return {"files_scanned": 0, "files_new": 0, "files_deduped": 0, "bytes_new": 0, "bytes_total_scanned": 0, "packs_created": 0}


def start_backup_run(db: sqlite3.Connection, source: Path) -> tuple[str, str]:
    run_id = uuid.uuid4().hex[:12]
    started = utc_now()
    db.execute(
        "INSERT INTO backup_runs(run_id, started_at, machine, source_path, status) VALUES (?, ?, ?, ?, ?)",
        (run_id, started, socket.gethostname(), str(source), "RUNNING"),
    )
    db.commit()
    return run_id, started


def handle_deduped_entry(
    db: sqlite3.Connection,
    run_id: str,
    started: str,
    stats: dict[str, int],
    previous: sqlite3.Row | None,
    entry: Entry,
) -> None:
    stats["files_deduped"] += 1
    if should_record_path_version(previous, entry):
        blob = lookup_file_blob(db, entry.sha256)
        record_path_version(
            db,
            path=entry.rel_path,
            sha256=entry.sha256,
            mtime=entry.mtime,
            run_id=run_id,
            recorded_at=started,
            pack_id=None if blob is None else blob["pack_id"],
        )


def append_new_entry(state: StagedPack, entry: Entry, stats: dict[str, int], run_hashes: set[str]) -> int:
    stats["files_new"] += 1
    stats["bytes_new"] += entry.size
    run_hashes.add(entry.sha256)
    state.append_entry(entry)
    return entry.size


def finalize_stage_pack(
    config: dict[str, object],
    db: sqlite3.Connection,
    backend: StorageBackend,
    state: StagedPack,
    stats: dict[str, int],
    scan_progress: ScanProgress,
    pack_number: int,
    pack_total: int,
) -> tuple[StagedPack, int]:
    stats["packs_created"] += 1
    scan_progress.suspend()
    try:
        seal_pack(config, db, backend, state, f"{pack_number}/{pack_total}")
    finally:
        scan_progress.resume()
    return new_pack_state(config, state.run_id)[0], pack_number + 1


def fetch_pack(config: dict[str, object], backend: StorageBackend, object_key: str, workdir: Path, progress: PackProgress | None = None) -> Path:
    enc_path = workdir / Path(object_key).name
    tar_path = workdir / enc_path.name.removesuffix(".age")
    if progress is None:
        backend.get_object(object_key, str(enc_path))
        decrypt_file(enc_path, tar_path, config)
        return tar_path
    with progress.stage("download"):
        backend.get_object(object_key, str(enc_path))
    with progress.stage("decrypt"):
        decrypt_file(enc_path, tar_path, config)
    return tar_path


def read_manifest_from_tar(tar_path: Path) -> dict[str, object]:
    with tarfile.open(tar_path) as tf:
        with tf.extractfile("MANIFEST.json") as fh:
            if fh is None:
                raise DeepKeepError("MANIFEST.json missing from pack")
            return json.load(fh)


def select_restore_rows(
    db: sqlite3.Connection,
    prefixes: tuple[str, ...],
    restore_all: bool,
    as_of_run: str | None,
) -> list[sqlite3.Row]:
    if as_of_run is None:
        rows = db.execute(
            """
            SELECT fp.path, fp.mtime, fp.sha256, f.size, f.pack_id, f.tar_path, p.object_key
            FROM file_paths fp
            JOIN files f ON f.sha256 = fp.sha256
            JOIN packs p ON p.pack_id = f.pack_id
            ORDER BY fp.path
            """
        ).fetchall()
    else:
        target_run = db.execute("SELECT started_at FROM backup_runs WHERE run_id = ?", (as_of_run,)).fetchone()
        if target_run is None:
            raise DeepKeepError(f"unknown run_id: {as_of_run}")
        rows = db.execute(
            """
            SELECT pv.path, pv.mtime, pv.sha256, f.size, f.pack_id, f.tar_path, p.object_key
            FROM path_versions pv
            JOIN backup_runs br ON br.run_id = pv.run_id
            JOIN files f ON f.sha256 = pv.sha256
            JOIN packs p ON p.pack_id = f.pack_id
            WHERE br.started_at = (
                SELECT MAX(br2.started_at)
                FROM path_versions pv2
                JOIN backup_runs br2 ON br2.run_id = pv2.run_id
                WHERE pv2.path = pv.path AND br2.started_at <= ?
            )
            ORDER BY pv.path
            """,
            (target_run["started_at"],),
        ).fetchall()
    if restore_all:
        return rows
    return [row for row in rows if any(row["path"].startswith(prefix) for prefix in prefixes)]


def group_restore_rows(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    by_pack: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_pack[row["pack_id"]].append(row)
    return by_pack


def restore_pack_ready(backend: StorageBackend, object_key: str, progress: PackProgress) -> str:
    with progress.stage("check"):
        status = backend.restore_status(object_key)
    if status == "cold":
        with progress.stage("requesting restore"):
            return backend.request_restore(object_key)
    return status


def restore_one_target(
    tf: tarfile.TarFile,
    row: sqlite3.Row,
    target: Path,
    *,
    force: bool,
    no_hardlinks: bool,
    canonical_by_hash: dict[str, tuple[Path, str]],
) -> int:
    if target.exists() and not force:
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if force:
        remove_existing_target(target)
    canonical = canonical_by_hash.get(row["sha256"])
    if canonical is None:
        member = tf.extractfile(row["tar_path"])
        if member is None:
            raise DeepKeepError(f"missing member in pack: {row['tar_path']}")
        data = member.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != row["sha256"]:
            raise DeepKeepError(f"hash mismatch while restoring {row['path']}")
        target.write_bytes(data)
        apply_mtime(target, row["mtime"])
        canonical_by_hash[row["sha256"]] = (target, row["path"])
        return len(data)
    canonical_target, canonical_rel = canonical
    logical_size = int(row["size"])
    if is_windows_platform():
        write_pointer_file(target, canonical_rel, canonical_target, row["sha256"], row["mtime"])
    elif is_linux_platform() and not no_hardlinks:
        try:
            os.link(canonical_target, target)
        except OSError:
            write_full_copy(target, canonical_target, row["mtime"])
    else:
        write_full_copy(target, canonical_target, row["mtime"])
    return logical_size


def restore_rows_from_pack(
    tf: tarfile.TarFile,
    rows_in_pack: list[sqlite3.Row],
    dest: Path,
    *,
    force: bool,
    no_hardlinks: bool,
    pack_progress: PackProgress,
    canonical_by_hash: dict[str, tuple[Path, str]],
) -> int:
    restored = 0
    with pack_progress.restore(sum(int(row["size"]) for row in rows_in_pack)) as restore_progress:
        for row in rows_in_pack:
            restored_bytes = restore_one_target(
                tf,
                row,
                dest / row["path"],
                force=force,
                no_hardlinks=no_hardlinks,
                canonical_by_hash=canonical_by_hash,
            )
            if restored_bytes == 0:
                continue
            restore_progress.advance(restored_bytes)
            restored += 1
    return restored


def restore_prefixes(
    config: dict[str, object],
    prefixes: tuple[str, ...],
    dest: Path,
    force: bool = False,
    restore_all: bool = False,
    no_hardlinks: bool = False,
    as_of_run: str | None = None,
) -> tuple[int, int]:
    db = connect_db(config)
    try:
        matches = select_restore_rows(db, prefixes, restore_all, as_of_run)
        if not matches:
            return 0, 0
        by_pack = group_restore_rows(matches)
        restored = 0
        pending = 0
        canonical_by_hash: dict[str, tuple[Path, str]] = {}
        backend = get_backend(config)
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            for rows_in_pack in by_pack.values():
                object_key = rows_in_pack[0]["object_key"]
                pack_progress = PackProgress(rows_in_pack[0]["pack_id"])
                status = restore_pack_ready(backend, object_key, pack_progress)
                if status != "ready":
                    pending += 1
                    continue
                tar_path = fetch_pack(config, backend, object_key, tmpdir, progress=pack_progress)
                with tarfile.open(tar_path) as tf:
                    restored += restore_rows_from_pack(
                        tf,
                        rows_in_pack,
                        dest,
                        force=force,
                        no_hardlinks=no_hardlinks,
                        pack_progress=pack_progress,
                        canonical_by_hash=canonical_by_hash,
                    )
        return restored, pending
    finally:
        db.close()


def verify_pack(config: dict[str, object], pack_id: str) -> list[str]:
    db = connect_db(config)
    row = db.execute("SELECT object_key FROM packs WHERE pack_id = ?", (pack_id,)).fetchone()
    if row is None:
        raise DeepKeepError(f"unknown pack_id: {pack_id}")
    backend = get_backend(config)
    issues: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = fetch_pack(config, backend, row["object_key"], Path(tmp))
        manifest = read_manifest_from_tar(tar_path)
        with tarfile.open(tar_path) as tf:
            for item in manifest["files"]:
                member = tf.extractfile(item["member_path"])
                if member is None:
                    issues.append(f"missing member {item['member_path']}")
                    continue
                digest = hashlib.sha256(member.read()).hexdigest()
                if digest != item["sha256"]:
                    issues.append(f"hash mismatch for {item['original_path']}")
    return issues


def rebuild_catalog(config: dict[str, object]) -> int:
    db = connect_db(config)
    backend = get_backend(config)
    db.execute("DELETE FROM file_paths")
    db.execute("DELETE FROM files")
    db.execute("DELETE FROM packs")
    db.execute("DELETE FROM path_versions")
    db.commit()
    count = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for key in backend.list_objects("packs"):
            tar_path = fetch_pack(config, backend, key, tmpdir)
            manifest = read_manifest_from_tar(tar_path)
            name = Path(key).name
            pack_id = name.split("pack-")[-1].split(".tar.age")[0]
            created_at = str(manifest.get("created_at", utc_now()))
            db.execute(
                "INSERT OR REPLACE INTO packs(pack_id, object_key, created_at, compression, encryption, uploaded, run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pack_id, key, created_at, "off", "age", 1, "rebuild"),
            )
            for item in manifest["files"]:
                db.execute(
                    "INSERT OR IGNORE INTO files(sha256, size, pack_id, tar_path) VALUES (?, ?, ?, ?)",
                    (item["sha256"], item["size"], pack_id, item["member_path"]),
                )
                db.execute(
                    "INSERT OR REPLACE INTO file_paths(path, sha256, mtime) VALUES (?, ?, ?)",
                    (item["original_path"], item["sha256"], item.get("mtime")),
                )
            count += 1
        db.execute("DELETE FROM run_files")
        db.execute("DELETE FROM path_versions")
        db.commit()
    return count


def catalog_summary(config: dict[str, object]) -> sqlite3.Row:
    db = connect_db(config)
    row = db.execute(
        """
        SELECT
            ? AS backend,
            (SELECT COUNT(*) FROM backup_runs) AS run_count,
            (SELECT COUNT(*) FROM file_paths) AS path_count,
            (SELECT COUNT(*) FROM files) AS unique_file_count,
            (SELECT COUNT(*) FROM packs) AS pack_count,
            COALESCE((SELECT SUM(f.size) FROM file_paths fp JOIN files f ON f.sha256 = fp.sha256), 0) AS logical_bytes,
            COALESCE((SELECT SUM(size) FROM files), 0) AS unique_bytes
        """,
        (catalog_backend_label(config),),
    ).fetchone()
    db.close()
    return row


def catalog_runs(config: dict[str, object]) -> list[sqlite3.Row]:
    db = connect_db(config)
    rows = db.execute(
        """
        SELECT run_id, started_at, completed_at, machine, source_path, files_scanned, files_new, files_deduped,
               bytes_new, bytes_total_scanned, packs_created, status
        FROM backup_runs
        ORDER BY started_at DESC
        """
    ).fetchall()
    db.close()
    return rows


def catalog_run_detail(config: dict[str, object], run_id: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row], list[sqlite3.Row]]:
    db = connect_db(config)
    run_row = db.execute(
        """
        SELECT run_id, started_at, completed_at, machine, source_path, files_scanned, files_new, files_deduped,
               bytes_new, bytes_total_scanned, packs_created, status
        FROM backup_runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    pack_rows = db.execute(
        """
        SELECT p.pack_id, p.created_at, p.object_key, p.run_id, COUNT(f.sha256) AS file_count, COALESCE(SUM(f.size), 0) AS total_bytes
        FROM packs p
        LEFT JOIN files f ON f.pack_id = p.pack_id
        WHERE p.run_id = ?
        GROUP BY p.pack_id, p.created_at, p.object_key, p.run_id
        ORDER BY p.created_at, p.pack_id
        """,
        (run_id,),
    ).fetchall()
    file_rows = db.execute(
        """
        SELECT path, size, mtime, pack_id
        FROM run_files
        WHERE run_id = ?
        ORDER BY path
        """,
        (run_id,),
    ).fetchall()
    db.close()
    return run_row, pack_rows, file_rows


def catalog_files(config: dict[str, object], prefix: str | None = None, pack_id: str | None = None, run_id: str | None = None) -> list[sqlite3.Row]:
    db = connect_db(config)
    sql = [
        """
        SELECT fp.path, f.size, fp.mtime, f.pack_id, rf.run_id
        FROM file_paths fp
        JOIN files f ON f.sha256 = fp.sha256
        LEFT JOIN run_files rf ON rf.path = fp.path AND rf.sha256 = fp.sha256
        """
    ]
    params: list[object] = []
    conditions: list[str] = []
    if prefix:
        conditions.append("fp.path LIKE ?")
        params.append(f"{prefix}%")
    if pack_id:
        conditions.append("f.pack_id = ?")
        params.append(pack_id)
    if run_id:
        conditions.append("rf.run_id = ?")
        params.append(run_id)
    if conditions:
        sql.append("WHERE " + " AND ".join(conditions))
    sql.append("ORDER BY fp.path")
    rows = db.execute("\n".join(sql), params).fetchall()
    db.close()
    return rows


def catalog_packs(config: dict[str, object]) -> list[sqlite3.Row]:
    db = connect_db(config)
    rows = db.execute(
        """
        SELECT p.pack_id, p.created_at, p.object_key, p.run_id, COUNT(f.sha256) AS file_count, COALESCE(SUM(f.size), 0) AS total_bytes
        FROM packs p
        LEFT JOIN files f ON f.pack_id = p.pack_id
        GROUP BY p.pack_id, p.created_at, p.object_key, p.run_id
        ORDER BY p.created_at DESC, p.pack_id DESC
        """
    ).fetchall()
    db.close()
    return rows


def catalog_file_detail(config: dict[str, object], path: str) -> sqlite3.Row | None:
    db = connect_db(config)
    row = db.execute(
        """
        SELECT fp.path, f.size, fp.mtime, fp.sha256, f.pack_id, f.tar_path,
               (SELECT pv.run_id FROM path_versions pv WHERE pv.path = fp.path AND pv.sha256 = fp.sha256 ORDER BY pv.recorded_at DESC LIMIT 1) AS run_id,
               (SELECT COUNT(*) FROM path_versions pv WHERE pv.path = fp.path) AS version_count
        FROM file_paths fp
        JOIN files f ON f.sha256 = fp.sha256
        JOIN packs p ON p.pack_id = f.pack_id
        WHERE fp.path = ?
        """,
        (path,),
    ).fetchone()
    db.close()
    return row


def catalog_file_history(config: dict[str, object], path: str) -> list[sqlite3.Row]:
    db = connect_db(config)
    rows = db.execute(
        """
        SELECT pv.path, f.size, pv.mtime, pv.sha256, f.pack_id, pv.run_id, pv.recorded_at
        FROM path_versions pv
        JOIN files f ON f.sha256 = pv.sha256
        WHERE pv.path = ?
        ORDER BY pv.recorded_at
        """,
        (path,),
    ).fetchall()
    db.close()
    return rows


def print_no_rows(label: str, plaintext: bool) -> None:
    if plaintext:
        click.echo(f"INFO\t{label}\tnone")
    else:
        console.print(f"{label}: none")


def render_summary(summary: sqlite3.Row, plaintext: bool) -> None:
    if plaintext:
        click.echo(
            "SUMMARY\t"
            f"{summary['backend']}\t{summary['run_count']}\t{summary['path_count']}\t{summary['unique_file_count']}\t"
            f"{summary['pack_count']}\t{summary['logical_bytes']}\t{summary['unique_bytes']}"
        )
        return
    table = Table(title="Catalog Summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Backend", str(summary["backend"]))
    table.add_row("Runs", format_int(summary["run_count"]))
    table.add_row("Archived paths", format_int(summary["path_count"]))
    table.add_row("Unique blobs", format_int(summary["unique_file_count"]))
    table.add_row("Packs", format_int(summary["pack_count"]))
    table.add_row("Logical bytes", format_int(summary["logical_bytes"]))
    table.add_row("Unique bytes", format_int(summary["unique_bytes"]))
    console.print(table)


def render_runs(rows: list[sqlite3.Row], plaintext: bool) -> None:
    if plaintext:
        for row in rows:
            click.echo(
                "RUN\t"
                f"{row['run_id']}\t{row['started_at']}\t{row['completed_at'] or ''}\t{row['status']}\t{row['machine'] or ''}\t"
                f"{row['files_scanned']}\t{row['files_new']}\t{row['files_deduped']}\t"
                f"{row['bytes_new']}\t{row['packs_created']}\t{row['source_path']}"
            )
        return
    if not rows:
        print_no_rows("Runs", plaintext=False)
        return
    table = Table(title="Backup Runs")
    table.add_column("Run ID")
    table.add_column("Started")
    table.add_column("Status")
    table.add_column("Machine")
    table.add_column("New", justify="right")
    table.add_column("Deduped", justify="right")
    table.add_column("Packs", justify="right")
    table.add_column("Source", overflow="fold")
    for row in rows:
        table.add_row(
            row["run_id"],
            row["started_at"],
            row["status"],
            row["machine"] or "",
            format_int(row["files_new"]),
            format_int(row["files_deduped"]),
            format_int(row["packs_created"]),
            row["source_path"],
        )
    console.print(table)


def render_files(rows: list[sqlite3.Row], plaintext: bool, title: str = "Catalog Files") -> None:
    if plaintext:
        for row in rows:
            click.echo(f"FILE\t{row['path']}\t{row['size']}\t{row['mtime'] or ''}\t{row['pack_id']}\t{row['run_id'] or ''}")
        return
    if not rows:
        print_no_rows("Files", plaintext=False)
        return
    table = Table(title=title)
    table.add_column("Path", overflow="fold")
    table.add_column("Size", justify="right")
    table.add_column("Modified")
    table.add_column("Pack")
    table.add_column("Run")
    for row in rows:
        table.add_row(row["path"], format_int(row["size"]), row["mtime"] or "", row["pack_id"], row["run_id"] or "")
    console.print(table)


def render_packs(rows: list[sqlite3.Row], plaintext: bool, title: str = "Packs") -> None:
    if plaintext:
        for row in rows:
            click.echo(
                f"PACK\t{row['pack_id']}\t{row['created_at']}\t{row['object_key']}\t{row['run_id']}\t{row['file_count']}\t{row['total_bytes']}"
            )
        return
    if not rows:
        print_no_rows("Packs", plaintext=False)
        return
    table = Table(title=title)
    table.add_column("Pack ID")
    table.add_column("Created")
    table.add_column("Run")
    table.add_column("Files", justify="right")
    table.add_column("Bytes", justify="right")
    table.add_column("Object Key", overflow="fold")
    for row in rows:
        table.add_row(
            row["pack_id"],
            row["created_at"],
            row["run_id"],
            format_int(row["file_count"]),
            format_int(row["total_bytes"]),
            row["object_key"],
        )
    console.print(table)


def render_file_detail(row: sqlite3.Row | None, plaintext: bool) -> None:
    if row is None:
        print_no_rows("File detail", plaintext)
        return
    if plaintext:
        click.echo(
            f"FILE_DETAIL\t{row['path']}\t{row['size']}\t{row['mtime'] or ''}\t{row['sha256']}\t{row['pack_id']}\t{row['tar_path']}\t{row['run_id'] or ''}\t{row['version_count']}"
        )
        return
    table = Table(title="File Detail")
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    for key, value in (
        ("Path", row["path"]),
        ("Size", format_int(row["size"])),
        ("Modified", row["mtime"] or ""),
        ("SHA256", row["sha256"]),
        ("Pack", row["pack_id"]),
        ("Tar Path", row["tar_path"]),
        ("Run", row["run_id"] or ""),
        ("Versions", format_int(row["version_count"])),
    ):
        table.add_row(key, str(value))
    console.print(table)


def render_file_history(rows: list[sqlite3.Row], plaintext: bool) -> None:
    if plaintext:
        for row in rows:
            click.echo(
                f"FILE_VERSION\t{row['path']}\t{row['run_id']}\t{row['recorded_at']}\t{row['sha256']}\t{row['pack_id']}\t{row['size']}\t{row['mtime'] or ''}"
            )
        return
    if not rows:
        print_no_rows("File history", plaintext=False)
        return
    table = Table(title="File History")
    table.add_column("Path", overflow="fold")
    table.add_column("Run")
    table.add_column("Recorded")
    table.add_column("Pack")
    table.add_column("Size", justify="right")
    table.add_column("Modified")
    for row in rows:
        table.add_row(
            row["path"],
            row["run_id"],
            row["recorded_at"],
            row["pack_id"],
            format_int(row["size"]),
            row["mtime"] or "",
        )
    console.print(table)


def cli_config_option(fn):
    return click.option(
        "--config",
        "config_path",
        type=click.Path(path_type=Path),
        envvar="DEEPKEEP_CONFIG",
        default=Path("deepkeep.yaml"),
        show_default=True,
        show_envvar=True,
        help="Path to the DeepKeep YAML config.",
    )(fn)


def current_config(ctx: click.Context) -> dict[str, object]:
    obj = ctx.find_object(dict) or {}
    return obj["config"]


@click.group(cls=DeepKeepCLI)
@cli_config_option
@click.pass_context
def cli(ctx: click.Context, config_path: Path) -> None:
    """Low-cost archival backups for local storage and S3 Glacier."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Scan and plan packs without writing or uploading them.")
@click.argument("source", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.pass_context
def backup(ctx: click.Context, dry_run: bool, source: Path) -> None:
    """Back up SOURCE in sorted directory order."""
    config = current_config(ctx)
    started_at = datetime.now().astimezone()
    console.print(f"Started: {format_local_timestamp(started_at)}")
    stats = backup_source(config, source.resolve(), dry_run=dry_run)
    table = Table(title="Backup Summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    rows = [
        ("Mode", "dry run" if dry_run else "backup"),
        ("Files scanned", str(stats["files_scanned"])),
        ("Files to back up", str(stats["files_new"])),
        ("Files deduped", str(stats["files_deduped"])),
        ("Estimated packs" if dry_run else "Packs used", str(stats["packs_created"])),
        ("New data", format_bytes(stats["bytes_new"])),
        ("New data bytes", str(stats["bytes_new"])),
        ("Scanned data", format_bytes(stats["bytes_total_scanned"])),
        ("Scanned bytes", str(stats["bytes_total_scanned"])),
    ]
    for key, value in rows:
        table.add_row(key, value)
    console.print(table)
    completed_at = datetime.now().astimezone()
    mode_label = "dry run" if dry_run else "backup"
    console.print(f"Completed: {format_local_timestamp(completed_at)}  total time taken for {mode_label}: {format_duration((completed_at - started_at).total_seconds())}")


@cli.command("restore")
@click.option("--dest", type=click.Path(path_type=Path), required=True)
@click.option("--all", "restore_all", is_flag=True, help="Restore the entire catalog.")
@click.option("--as-of-run", help="Restore the latest versions known at or before the given run.")
@click.option("--no-hardlinks", is_flag=True, help="Do not restore duplicate files as hardlinks on Linux.")
@click.option("--force", is_flag=True, help="Overwrite files already present at the destination.")
@click.argument("prefixes", nargs=-1)
@click.pass_context
def restore_cmd(
    ctx: click.Context,
    dest: Path,
    restore_all: bool,
    as_of_run: str | None,
    no_hardlinks: bool,
    force: bool,
    prefixes: tuple[str, ...],
) -> None:
    """Restore archived files whose original paths match PREFIXES."""
    if restore_all and prefixes:
        raise click.UsageError("use either PREFIXES or --all, not both")
    if not restore_all and not prefixes:
        raise click.UsageError("provide at least one path prefix, or use --all")
    config = current_config(ctx)
    started_at = datetime.now().astimezone()
    console.print(f"Started: {format_local_timestamp(started_at)}")
    restored, pending = restore_prefixes(
        config,
        prefixes,
        dest.resolve(),
        force=force,
        restore_all=restore_all,
        no_hardlinks=no_hardlinks,
        as_of_run=as_of_run,
    )
    console.print(f"restored: {restored}")
    if pending:
        console.print(f"packs pending glacier restore: {pending}")
    completed_at = datetime.now().astimezone()
    console.print(f"Completed: {format_local_timestamp(completed_at)}  total time taken for restore: {format_duration((completed_at - started_at).total_seconds())}")


@cli.command("verify-pack")
@click.argument("pack_id")
@click.pass_context
def verify_pack_cmd(ctx: click.Context, pack_id: str) -> None:
    """Verify MANIFEST.json entries against pack contents."""
    config = current_config(ctx)
    issues = verify_pack(config, pack_id)
    if issues:
        for issue in issues:
            console.print(f"[red]{issue}[/red]")
        raise SystemExit(1)
    console.print("pack verified")


@cli.command("verify-catalog")
@click.pass_context
def verify_catalog_cmd(ctx: click.Context) -> None:
    """Run paranoid SQLite and catalog consistency checks."""
    config = current_config(ctx)
    issues = verify_catalog(config)
    if issues:
        for issue in issues:
            console.print(f"[red]{issue}[/red]")
        raise SystemExit(1)
    console.print("catalog verified")


@cli.command("upload-catalog")
@click.pass_context
def upload_catalog_cmd(ctx: click.Context) -> None:
    """Upload the current local catalog snapshot to the configured backend."""
    config = current_config(ctx)
    started_at = datetime.now().astimezone()
    console.print(f"Started: {format_local_timestamp(started_at)}")
    db = connect_db(config)
    try:
        quick_validate_catalog(db)
    finally:
        db.close()
    snapshot_catalog(config, get_backend(config))
    completed_at = datetime.now().astimezone()
    console.print(f"Completed: {format_local_timestamp(completed_at)}  total time taken for catalog upload: {format_duration((completed_at - started_at).total_seconds())}")


@cli.command("rebuild-catalog")
@click.pass_context
def rebuild_catalog_cmd(ctx: click.Context) -> None:
    """Rebuild SQLite from embedded manifests."""
    config = current_config(ctx)
    count = rebuild_catalog(config)
    console.print(f"rebuilt catalog from {count} pack(s)")


@cli.group("catalog", invoke_without_command=True)
@click.option("--plaintext", is_flag=True, help="Print line-oriented text instead of rich tables.")
@click.pass_context
def catalog_group(ctx: click.Context, plaintext: bool) -> None:
    """Explore catalog summaries, runs, packs, and files."""
    config = current_config(ctx)
    ctx.obj = {"config": config, "plaintext": plaintext}
    if ctx.invoked_subcommand is None:
        render_summary(catalog_summary(config), plaintext)
        render_runs(catalog_runs(config), plaintext)


def catalog_context(ctx: click.Context) -> tuple[dict[str, object], bool]:
    obj = ctx.find_object(dict) or {}
    return obj["config"], bool(obj["plaintext"])


@catalog_group.command("summary")
@click.option("--plaintext", is_flag=True, help="Print line-oriented text instead of rich tables.")
@click.pass_context
def catalog_summary_cmd(ctx: click.Context, plaintext: bool) -> None:
    config, default_plaintext = catalog_context(ctx)
    render_summary(catalog_summary(config), plaintext or default_plaintext)


@catalog_group.command("runs")
@click.option("--plaintext", is_flag=True, help="Print line-oriented text instead of rich tables.")
@click.pass_context
def catalog_runs_cmd(ctx: click.Context, plaintext: bool) -> None:
    config, default_plaintext = catalog_context(ctx)
    render_runs(catalog_runs(config), plaintext or default_plaintext)


@catalog_group.command("run")
@click.argument("run_id")
@click.option("--plaintext", is_flag=True, help="Print line-oriented text instead of rich tables.")
@click.pass_context
def catalog_run_cmd(ctx: click.Context, run_id: str, plaintext: bool) -> None:
    config, default_plaintext = catalog_context(ctx)
    use_plaintext = plaintext or default_plaintext
    run_row, pack_rows, file_rows = catalog_run_detail(config, run_id)
    if run_row is None:
        raise click.ClickException(f"unknown run_id: {run_id}")
    if use_plaintext:
        click.echo(
            "RUN_DETAIL\t"
            f"{run_row['run_id']}\t{run_row['started_at']}\t{run_row['completed_at'] or ''}\t{run_row['status']}\t{run_row['machine'] or ''}\t"
            f"{run_row['files_scanned']}\t{run_row['files_new']}\t{run_row['files_deduped']}\t"
            f"{run_row['bytes_new']}\t{run_row['bytes_total_scanned']}\t{run_row['packs_created']}\t{run_row['source_path']}"
        )
    else:
        table = Table(title="Run Detail")
        table.add_column("Field")
        table.add_column("Value", overflow="fold")
        for key, value in (
            ("Run ID", run_row["run_id"]),
            ("Started", run_row["started_at"]),
            ("Completed", run_row["completed_at"] or ""),
            ("Status", run_row["status"]),
            ("Machine", run_row["machine"] or ""),
            ("Source", run_row["source_path"]),
            ("Files scanned", format_int(run_row["files_scanned"])),
            ("Files new", format_int(run_row["files_new"])),
            ("Files deduped", format_int(run_row["files_deduped"])),
            ("Bytes new", format_int(run_row["bytes_new"])),
            ("Bytes scanned", format_int(run_row["bytes_total_scanned"])),
            ("Packs created", format_int(run_row["packs_created"])),
        ):
            table.add_row(key, str(value))
        console.print(table)
    render_packs(pack_rows, use_plaintext, title="Run Packs")
    run_files_rows = [
        {"path": row["path"], "size": row["size"], "mtime": row["mtime"], "pack_id": row["pack_id"], "run_id": run_id}
        for row in file_rows
    ]
    render_files(run_files_rows, use_plaintext, title="Run Files")


@catalog_group.command("files")
@click.option("--prefix", help="Only show archived paths under this prefix.")
@click.option("--pack-id", help="Only show files stored in the given pack.")
@click.option("--run-id", help="Only show files newly archived in the given run.")
@click.option("--plaintext", is_flag=True, help="Print line-oriented text instead of rich tables.")
@click.pass_context
def catalog_files_cmd(ctx: click.Context, prefix: str | None, pack_id: str | None, run_id: str | None, plaintext: bool) -> None:
    config, default_plaintext = catalog_context(ctx)
    render_files(catalog_files(config, prefix=prefix, pack_id=pack_id, run_id=run_id), plaintext or default_plaintext)


@catalog_group.command("packs")
@click.option("--plaintext", is_flag=True, help="Print line-oriented text instead of rich tables.")
@click.pass_context
def catalog_packs_cmd(ctx: click.Context, plaintext: bool) -> None:
    config, default_plaintext = catalog_context(ctx)
    render_packs(catalog_packs(config), plaintext or default_plaintext)


@catalog_group.command("file")
@click.argument("path")
@click.option("--plaintext", is_flag=True, help="Print line-oriented text instead of rich tables.")
@click.pass_context
def catalog_file_cmd(ctx: click.Context, path: str, plaintext: bool) -> None:
    config, default_plaintext = catalog_context(ctx)
    render_file_detail(catalog_file_detail(config, path), plaintext or default_plaintext)


@catalog_group.command("file-history")
@click.argument("path")
@click.option("--plaintext", is_flag=True, help="Print line-oriented text instead of rich tables.")
@click.pass_context
def catalog_file_history_cmd(ctx: click.Context, path: str, plaintext: bool) -> None:
    config, default_plaintext = catalog_context(ctx)
    render_file_history(catalog_file_history(config, path), plaintext or default_plaintext)


if __name__ == "__main__":
    cli()
