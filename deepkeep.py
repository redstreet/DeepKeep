#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import click
import yaml
from rich.console import Console
from rich.table import Table

console = Console()
ISO = "%Y-%m-%dT%H:%M:%SZ"
PACK_MIN_MB = 512


class DeepKeepError(RuntimeError):
    pass


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


def utc_now() -> str:
    return datetime.now(UTC).strftime(ISO)


def parse_utc(value: str) -> datetime:
    return datetime.strptime(value, ISO).replace(tzinfo=UTC)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        input=input_text,
        capture_output=True,
        check=check,
    )


def require_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise DeepKeepError(f"required executable not found on PATH: {name}")


def load_config(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise DeepKeepError("config must be a YAML mapping")
    data.setdefault("backend", "local")
    data.setdefault("pack_size_mb", PACK_MIN_MB)
    data.setdefault("catalog_path", str(path.with_suffix(".sqlite")))
    data.setdefault("work_root", str(path.parent / ".deepkeep-work"))
    if "gpg_pass_entry" not in data:
        raise DeepKeepError("config must define gpg_pass_entry")
    if data["backend"] == "local":
        local = data.setdefault("local", {})
        if "root" not in local:
            raise DeepKeepError("config.local.root is required for local backend")
    elif data["backend"] == "s3":
        s3 = data.setdefault("s3", {})
        for key in ("bucket", "prefix"):
            if key not in s3:
                raise DeepKeepError(f"config.s3.{key} is required for s3 backend")
        s3.setdefault("storage_class", "DEEP_ARCHIVE")
    else:
        raise DeepKeepError("backend must be 'local' or 's3'")
    return data


def load_passphrase(config: dict[str, object]) -> str:
    require_tool("pass")
    entry = str(config["gpg_pass_entry"])
    proc = run(["pass", "show", entry])
    value = proc.stdout.splitlines()[0].strip() if proc.stdout else ""
    if not value:
        raise DeepKeepError(f"pass entry is empty: {entry}")
    return value


def encrypt_file(src: Path, dest: Path, config: dict[str, object]) -> None:
    require_tool("gpg")
    passphrase = load_passphrase(config)
    proc = run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase-fd",
            "0",
            "--symmetric",
            "--cipher-algo",
            "AES256",
            "--compress-algo",
            "none",
            "--output",
            str(dest),
            str(src),
        ],
        input_text=f"{passphrase}\n",
        check=False,
    )
    if proc.returncode != 0:
        raise DeepKeepError(proc.stderr.strip() or "gpg encryption failed")


def decrypt_file(src: Path, dest: Path, config: dict[str, object]) -> None:
    require_tool("gpg")
    passphrase = load_passphrase(config)
    proc = run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--passphrase-fd",
            "0",
            "--output",
            str(dest),
            "--decrypt",
            str(src),
        ],
        input_text=f"{passphrase}\n",
        check=False,
    )
    if proc.returncode != 0:
        raise DeepKeepError(proc.stderr.strip() or "gpg decryption failed")


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
        CREATE INDEX IF NOT EXISTS idx_run_files_run_id ON run_files(run_id);
        CREATE INDEX IF NOT EXISTS idx_run_files_pack_id ON run_files(pack_id);
        """
    )
    return db


def upsert_file_path(db: sqlite3.Connection, entry: Entry) -> None:
    db.execute(
        "INSERT OR REPLACE INTO file_paths(path, sha256, mtime) VALUES (?, ?, ?)",
        (entry.rel_path, entry.sha256, entry.mtime),
    )


def has_hash(db: sqlite3.Connection, sha256: str) -> bool:
    return db.execute("SELECT 1 FROM files WHERE sha256 = ?", (sha256,)).fetchone() is not None


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


def write_tar(tar_path: Path, manifest: dict[str, object], entries: list[Entry]) -> None:
    with tarfile.open(tar_path, "w") as tf:
        data = json.dumps(manifest, indent=2, sort_keys=True).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(data)
        info.mtime = int(datetime.now(UTC).timestamp())
        tf.addfile(info, io.BytesIO(data))
        for entry in entries:
            tf.add(entry.source, arcname=entry.member_path, recursive=False)


def pack_object_key(pack_id: str, created_at: str) -> str:
    dt = datetime.strptime(created_at, ISO)
    return f"packs/{dt:%Y/%m}/pack-{pack_id}.tar.gpg"


def catalog_object_keys(ts: str) -> tuple[str, str]:
    return "catalog/latest.sqlite.gpg", f"catalog/snapshots/catalog-{ts.replace(':', '').replace('-', '')}.sqlite.gpg"


def read_stage(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def write_stage(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


class LocalBackend:
    def __init__(self, root: Path) -> None:
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
    def __init__(self, cfg: dict[str, object]) -> None:
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
        proc = run(["aws", "s3", "ls", uri, "--recursive"])
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


def get_backend(config: dict[str, object]) -> LocalBackend | S3Backend:
    if config["backend"] == "local":
        return LocalBackend(Path(config["local"]["root"]))
    return S3Backend(config["s3"])


def stage_root(config: dict[str, object]) -> Path:
    root = Path(str(config["work_root"]))
    root.mkdir(parents=True, exist_ok=True)
    return root


def snapshot_catalog(config: dict[str, object], backend: LocalBackend | S3Backend) -> None:
    catalog = Path(str(config["catalog_path"]))
    if not catalog.exists():
        return
    ts = utc_now()
    with tempfile.TemporaryDirectory() as tmp:
        enc = Path(tmp) / "catalog.sqlite.gpg"
        encrypt_file(catalog, enc, config)
        latest_key, snap_key = catalog_object_keys(ts)
        backend.put_object(latest_key, str(enc))
        backend.put_object(snap_key, str(enc))


def commit_pack(db: sqlite3.Connection, stage: dict[str, object]) -> None:
    db.execute(
        "INSERT OR REPLACE INTO packs(pack_id, object_key, created_at, compression, encryption, uploaded, run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            stage["pack_id"],
            stage["object_key"],
            stage["created_at"],
            "off",
            "gpg",
            1,
            stage["run_id"],
        ),
    )
    for item in stage["entries"]:
        db.execute(
            "INSERT OR IGNORE INTO files(sha256, size, pack_id, tar_path) VALUES (?, ?, ?, ?)",
            (item["sha256"], item["size"], stage["pack_id"], item["member_path"]),
        )
        db.execute(
            "INSERT OR REPLACE INTO file_paths(path, sha256, mtime) VALUES (?, ?, ?)",
            (item["original_path"], item["sha256"], item.get("mtime")),
        )
        db.execute(
            "INSERT OR REPLACE INTO run_files(run_id, path, sha256, size, mtime, pack_id) VALUES (?, ?, ?, ?, ?, ?)",
            (stage["run_id"], item["original_path"], item["sha256"], item["size"], item.get("mtime"), stage["pack_id"]),
        )
    db.commit()


def resume_pending(config: dict[str, object], db: sqlite3.Connection, backend: LocalBackend | S3Backend) -> int:
    committed = 0
    for state_path in sorted(stage_root(config).glob("packs/*/state.json")):
        stage = read_stage(state_path)
        enc = Path(stage["enc_path"])
        if stage["status"] == "ENCRYPTED":
            backend.put_object(stage["object_key"], str(enc))
            stage["status"] = "UPLOADED"
            write_stage(state_path, stage)
        if stage["status"] == "UPLOADED":
            commit_pack(db, stage)
            stage["status"] = "COMMITTED"
            write_stage(state_path, stage)
            shutil.rmtree(state_path.parent)
            committed += 1
    return committed


def new_pack_state(config: dict[str, object], run_id: str) -> tuple[dict[str, object], Path]:
    pack_id = uuid.uuid4().hex[:12]
    created_at = utc_now()
    object_key = pack_object_key(pack_id, created_at)
    pack_dir = stage_root(config) / "packs" / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "pack_id": pack_id,
        "run_id": run_id,
        "created_at": created_at,
        "object_key": object_key,
        "status": "BUILDING",
        "tar_path": str(pack_dir / "pack.tar"),
        "enc_path": str(pack_dir / "pack.tar.gpg"),
        "entries": [],
    }
    return state, pack_dir


def seal_pack(config: dict[str, object], db: sqlite3.Connection, backend: LocalBackend | S3Backend, state: dict[str, object]) -> None:
    entries = [
        Entry(
            source=Path(item["source"]),
            rel_path=item["original_path"],
            member_path=item["member_path"],
            size=int(item["size"]),
            sha256=item["sha256"],
            mtime=item["mtime"],
        )
        for item in state["entries"]
    ]
    manifest = make_manifest(entries, str(state["created_at"]))
    tar_path = Path(str(state["tar_path"]))
    enc_path = Path(str(state["enc_path"]))
    write_tar(tar_path, manifest, entries)
    write_stage(tar_path.parent / "state.json", state)
    encrypt_file(tar_path, enc_path, config)
    state["status"] = "ENCRYPTED"
    write_stage(tar_path.parent / "state.json", state)
    backend.put_object(str(state["object_key"]), str(enc_path))
    state["status"] = "UPLOADED"
    write_stage(tar_path.parent / "state.json", state)
    commit_pack(db, state)
    shutil.rmtree(tar_path.parent)


def backup_source(config: dict[str, object], source: Path, dry_run: bool = False) -> dict[str, int]:
    db = connect_db(config)
    backend = get_backend(config)
    resume_pending(config, db, backend)
    run_id = uuid.uuid4().hex[:12]
    started = utc_now()
    db.execute(
        "INSERT INTO backup_runs(run_id, started_at, machine, source_path, status) VALUES (?, ?, ?, ?, ?)",
        (run_id, started, socket.gethostname(), str(source), "RUNNING"),
    )
    db.commit()
    stats = {"files_scanned": 0, "files_new": 0, "files_deduped": 0, "bytes_new": 0, "bytes_total_scanned": 0, "packs_created": 0}
    target = int(config["pack_size_mb"]) * 1024 * 1024
    state, _ = new_pack_state(config, run_id)
    current_size = 0
    run_hashes: set[str] = set()
    for path in iter_files(source):
        entry = build_entry(source, path)
        stats["files_scanned"] += 1
        stats["bytes_total_scanned"] += entry.size
        upsert_file_path(db, entry)
        if entry.sha256 in run_hashes or has_hash(db, entry.sha256):
            stats["files_deduped"] += 1
            continue
        stats["files_new"] += 1
        stats["bytes_new"] += entry.size
        run_hashes.add(entry.sha256)
        state["entries"].append(
            {
                "source": str(entry.source),
                "original_path": entry.rel_path,
                "member_path": entry.member_path,
                "size": entry.size,
                "sha256": entry.sha256,
                "mtime": entry.mtime,
            }
        )
        current_size += entry.size
        if current_size >= target:
            stats["packs_created"] += 1
            if not dry_run:
                seal_pack(config, db, backend, state)
            state, _ = new_pack_state(config, run_id)
            current_size = 0
    if state["entries"]:
        stats["packs_created"] += 1
        if not dry_run:
            seal_pack(config, db, backend, state)
    if not dry_run:
        snapshot_catalog(config, backend)
    db.execute(
        """
        UPDATE backup_runs
        SET completed_at = ?, files_scanned = ?, files_new = ?, files_deduped = ?, bytes_new = ?,
            bytes_total_scanned = ?, packs_created = ?, status = ?
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
            "DRY_RUN" if dry_run else "COMPLETED",
            run_id,
        ),
    )
    db.commit()
    db.close()
    return stats


def fetch_pack(config: dict[str, object], backend: LocalBackend | S3Backend, object_key: str, workdir: Path) -> Path:
    enc_path = workdir / Path(object_key).name
    tar_path = workdir / enc_path.name.removesuffix(".gpg")
    backend.get_object(object_key, str(enc_path))
    decrypt_file(enc_path, tar_path, config)
    return tar_path


def read_manifest_from_tar(tar_path: Path) -> dict[str, object]:
    with tarfile.open(tar_path) as tf:
        with tf.extractfile("MANIFEST.json") as fh:
            if fh is None:
                raise DeepKeepError("MANIFEST.json missing from pack")
            return json.load(fh)


def restore_prefixes(config: dict[str, object], prefixes: tuple[str, ...], dest: Path, force: bool = False, restore_all: bool = False) -> tuple[int, int]:
    db = connect_db(config)
    rows = db.execute(
        """
        SELECT fp.path, fp.mtime, fp.sha256, f.pack_id, f.tar_path, p.object_key
        FROM file_paths fp
        JOIN files f ON f.sha256 = fp.sha256
        JOIN packs p ON p.pack_id = f.pack_id
        ORDER BY fp.path
        """
    ).fetchall()
    matches = rows if restore_all else [row for row in rows if any(row["path"].startswith(prefix) for prefix in prefixes)]
    if not matches:
        return 0, 0
    by_pack: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in matches:
        by_pack[row["pack_id"]].append(row)
    backend = get_backend(config)
    restored = 0
    pending = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for rows_in_pack in by_pack.values():
            object_key = rows_in_pack[0]["object_key"]
            status = backend.request_restore(object_key) if config["backend"] == "s3" else "ready"
            if status != "ready":
                pending += 1
                continue
            tar_path = fetch_pack(config, backend, object_key, tmpdir)
            with tarfile.open(tar_path) as tf:
                for row in rows_in_pack:
                    rel = row["path"]
                    target = dest / rel
                    if target.exists() and not force:
                        continue
                    member = tf.extractfile(row["tar_path"])
                    if member is None:
                        raise DeepKeepError(f"missing member in pack: {row['tar_path']}")
                    data = member.read()
                    digest = hashlib.sha256(data).hexdigest()
                    if digest != row["sha256"]:
                        raise DeepKeepError(f"hash mismatch while restoring {rel}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    if row["mtime"]:
                        ts = parse_utc(str(row["mtime"])).timestamp()
                        os.utime(target, (ts, ts))
                    restored += 1
    return restored, pending


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
    db.commit()
    count = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for key in backend.list_objects("packs"):
            tar_path = fetch_pack(config, backend, key, tmpdir)
            manifest = read_manifest_from_tar(tar_path)
            name = Path(key).name
            pack_id = name.split("pack-")[-1].split(".tar.gpg")[0]
            created_at = str(manifest.get("created_at", utc_now()))
            db.execute(
                "INSERT OR REPLACE INTO packs(pack_id, object_key, created_at, compression, encryption, uploaded, run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pack_id, key, created_at, "off", "gpg", 1, "rebuild"),
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
        db.commit()
    return count


def catalog_summary(config: dict[str, object]) -> sqlite3.Row:
    db = connect_db(config)
    row = db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM backup_runs) AS run_count,
            (SELECT COUNT(*) FROM file_paths) AS path_count,
            (SELECT COUNT(*) FROM files) AS unique_file_count,
            (SELECT COUNT(*) FROM packs) AS pack_count,
            COALESCE((SELECT SUM(f.size) FROM file_paths fp JOIN files f ON f.sha256 = fp.sha256), 0) AS logical_bytes,
            COALESCE((SELECT SUM(size) FROM files), 0) AS unique_bytes
        """
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
        SELECT fp.path, f.size, fp.mtime, fp.sha256, f.pack_id, f.tar_path, rf.run_id
        FROM file_paths fp
        JOIN files f ON f.sha256 = fp.sha256
        LEFT JOIN run_files rf ON rf.path = fp.path AND rf.sha256 = fp.sha256
        WHERE fp.path = ?
        """,
        (path,),
    ).fetchone()
    db.close()
    return row


def print_no_rows(label: str, plaintext: bool) -> None:
    if plaintext:
        click.echo(f"INFO\t{label}\tnone")
    else:
        console.print(f"{label}: none")


def render_summary(summary: sqlite3.Row, plaintext: bool) -> None:
    if plaintext:
        click.echo(
            "SUMMARY\t"
            f"{summary['run_count']}\t{summary['path_count']}\t{summary['unique_file_count']}\t"
            f"{summary['pack_count']}\t{summary['logical_bytes']}\t{summary['unique_bytes']}"
        )
        return
    table = Table(title="Catalog Summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Runs", str(summary["run_count"]))
    table.add_row("Archived paths", str(summary["path_count"]))
    table.add_row("Unique blobs", str(summary["unique_file_count"]))
    table.add_row("Packs", str(summary["pack_count"]))
    table.add_row("Logical bytes", str(summary["logical_bytes"]))
    table.add_row("Unique bytes", str(summary["unique_bytes"]))
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
            str(row["files_new"]),
            str(row["files_deduped"]),
            str(row["packs_created"]),
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
        table.add_row(row["path"], str(row["size"]), row["mtime"] or "", row["pack_id"], row["run_id"] or "")
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
            str(row["file_count"]),
            str(row["total_bytes"]),
            row["object_key"],
        )
    console.print(table)


def render_file_detail(row: sqlite3.Row | None, plaintext: bool) -> None:
    if row is None:
        print_no_rows("File detail", plaintext)
        return
    if plaintext:
        click.echo(
            f"FILE_DETAIL\t{row['path']}\t{row['size']}\t{row['mtime'] or ''}\t{row['sha256']}\t{row['pack_id']}\t{row['tar_path']}\t{row['run_id'] or ''}"
        )
        return
    table = Table(title="File Detail")
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    for key, value in (
        ("Path", row["path"]),
        ("Size", row["size"]),
        ("Modified", row["mtime"] or ""),
        ("SHA256", row["sha256"]),
        ("Pack", row["pack_id"]),
        ("Tar Path", row["tar_path"]),
        ("Run", row["run_id"] or ""),
    ):
        table.add_row(key, str(value))
    console.print(table)


def option_config(fn):
    return click.option("--config", "config_path", type=click.Path(path_type=Path), default=Path("deepkeep.yaml"), show_default=True)(fn)


@click.group()
def cli() -> None:
    """Low-cost archival backups for local storage and S3 Glacier."""


@cli.command()
@option_config
@click.option("--dry-run", is_flag=True, help="Scan and plan packs without writing or uploading them.")
@click.argument("source", type=click.Path(exists=True, file_okay=False, path_type=Path))
def backup(config_path: Path, dry_run: bool, source: Path) -> None:
    """Back up SOURCE in sorted directory order."""
    config = load_config(config_path)
    stats = backup_source(config, source.resolve(), dry_run=dry_run)
    table = Table(title="Backup Summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for key, value in stats.items():
        table.add_row(key, str(value))
    console.print(table)


@cli.command("restore")
@option_config
@click.option("--dest", type=click.Path(path_type=Path), required=True)
@click.option("--all", "restore_all", is_flag=True, help="Restore the entire catalog.")
@click.option("--force", is_flag=True, help="Overwrite files already present at the destination.")
@click.argument("prefixes", nargs=-1)
def restore_cmd(config_path: Path, dest: Path, restore_all: bool, force: bool, prefixes: tuple[str, ...]) -> None:
    """Restore archived files whose original paths match PREFIXES."""
    if restore_all and prefixes:
        raise click.UsageError("use either PREFIXES or --all, not both")
    if not restore_all and not prefixes:
        raise click.UsageError("provide at least one path prefix, or use --all")
    config = load_config(config_path)
    restored, pending = restore_prefixes(config, prefixes, dest.resolve(), force=force, restore_all=restore_all)
    console.print(f"restored: {restored}")
    if pending:
        console.print(f"packs pending glacier restore: {pending}")


@cli.command("verify-pack")
@option_config
@click.argument("pack_id")
def verify_pack_cmd(config_path: Path, pack_id: str) -> None:
    """Verify MANIFEST.json entries against pack contents."""
    config = load_config(config_path)
    issues = verify_pack(config, pack_id)
    if issues:
        for issue in issues:
            console.print(f"[red]{issue}[/red]")
        raise SystemExit(1)
    console.print("pack verified")


@cli.command("rebuild-catalog")
@option_config
def rebuild_catalog_cmd(config_path: Path) -> None:
    """Rebuild SQLite from embedded manifests."""
    config = load_config(config_path)
    count = rebuild_catalog(config)
    console.print(f"rebuilt catalog from {count} pack(s)")


@cli.group("catalog", invoke_without_command=True)
@option_config
@click.option("--plaintext", is_flag=True, help="Print line-oriented text instead of rich tables.")
@click.pass_context
def catalog_group(ctx: click.Context, config_path: Path, plaintext: bool) -> None:
    """Explore catalog summaries, runs, packs, and files."""
    config = load_config(config_path)
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
            ("Files scanned", run_row["files_scanned"]),
            ("Files new", run_row["files_new"]),
            ("Files deduped", run_row["files_deduped"]),
            ("Bytes new", run_row["bytes_new"]),
            ("Bytes scanned", run_row["bytes_total_scanned"]),
            ("Packs created", run_row["packs_created"]),
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


if __name__ == "__main__":
    cli()
