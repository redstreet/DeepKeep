from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

import deepkeep


@pytest.fixture()
def fake_crypto(monkeypatch):
    monkeypatch.setattr(deepkeep, "encrypt_file", lambda src, dest, config: shutil.copy2(src, dest))
    monkeypatch.setattr(deepkeep, "decrypt_file", lambda src, dest, config: shutil.copy2(src, dest))


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    storage = tmp_path / "storage"
    source.mkdir()
    storage.mkdir()
    config = tmp_path / "deepkeep.yaml"
    config.write_text(
        "\n".join(
            [
                "backend: local",
                f"catalog_path: {tmp_path / 'catalog.sqlite'}",
                "pack_size_mb: 1",
                "gpg_pass_entry: backups/deepkeep",
                f"work_root: {tmp_path / '.work'}",
                "local:",
                f"  root: {storage}",
            ]
        )
    )
    return source, storage, config


def db_rows(config: Path, sql: str):
    db = sqlite3.connect(str(config.parent / "catalog.sqlite"))
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_backup_restore_verify_and_rebuild(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, storage, config = repo
    (source / "a").mkdir()
    (source / "a" / "one.txt").write_text("one")
    (source / "a" / "two.txt").write_text("two")
    (source / "dup.txt").write_text("one")

    result = CliRunner().invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    pack_files = sorted(storage.rglob("*.gpg"))
    assert any("pack-" in path.name for path in pack_files)
    assert any("catalog" in path.as_posix() for path in pack_files)
    assert len(db_rows(config, "SELECT * FROM files")) == 2
    assert len(db_rows(config, "SELECT * FROM file_paths")) == 3

    pack_path = next(path for path in pack_files if "pack-" in path.name)
    with tarfile.open(pack_path) as tf:
        names = tf.getnames()
    assert names == ["MANIFEST.json", "files/a/one.txt", "files/a/two.txt"]

    dest = tmp_path / "restore"
    result = CliRunner().invoke(
        deepkeep.cli,
        ["restore", "--config", str(config), "--dest", str(dest), "a/"],
    )
    assert result.exit_code == 0, result.output
    assert (dest / "a" / "one.txt").read_text() == "one"
    assert (dest / "a" / "two.txt").read_text() == "two"
    assert not (dest / "dup.txt").exists()

    pack_id = db_rows(config, "SELECT pack_id FROM packs")[0][0]
    result = CliRunner().invoke(deepkeep.cli, ["verify-pack", "--config", str(config), pack_id])
    assert result.exit_code == 0, result.output

    catalog = config.parent / "catalog.sqlite"
    catalog.unlink()
    result = CliRunner().invoke(deepkeep.cli, ["rebuild-catalog", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert len(db_rows(config, "SELECT * FROM files")) == 2
    assert len(db_rows(config, "SELECT * FROM file_paths")) == 2


def test_dedupe_second_backup_creates_no_new_pack(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, storage, config = repo
    (source / "x.txt").write_text("same")
    runner = CliRunner()
    assert runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)]).exit_code == 0
    first_count = len(list(storage.rglob("pack-*.gpg")))
    assert runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)]).exit_code == 0
    assert len(list(storage.rglob("pack-*.gpg"))) == first_count


def test_oversize_file_becomes_single_pack(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, storage, config = repo
    (source / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    result = CliRunner().invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output
    packs = list(storage.rglob("pack-*.gpg"))
    assert len(packs) == 1


def test_restore_skips_existing_without_force(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    (source / "keep.txt").write_text("fresh")
    runner = CliRunner()
    assert runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)]).exit_code == 0
    dest = tmp_path / "restore"
    dest.mkdir()
    (dest / "keep.txt").write_text("old")
    result = runner.invoke(deepkeep.cli, ["restore", "--config", str(config), "--dest", str(dest), "keep"])
    assert result.exit_code == 0, result.output
    assert (dest / "keep.txt").read_text() == "old"
    result = runner.invoke(deepkeep.cli, ["restore", "--config", str(config), "--dest", str(dest), "--force", "keep"])
    assert result.exit_code == 0, result.output
    assert (dest / "keep.txt").read_text() == "fresh"


def test_restore_reapplies_original_mtime(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    original = source / "stamp.txt"
    original.write_text("ts")
    ts = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
    os.utime(original, (ts, ts))

    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, ["restore", "--config", str(config), "--dest", str(dest), "stamp"])
    assert result.exit_code == 0, result.output
    restored = dest / "stamp.txt"
    assert restored.exists()
    assert int(restored.stat().st_mtime) == int(ts)


def test_verify_detects_corruption(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, storage, config = repo
    (source / "bad.txt").write_text("good")
    runner = CliRunner()
    assert runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)]).exit_code == 0
    pack_path = next(storage.rglob("pack-*.gpg"))
    with tarfile.open(pack_path, "a") as tf:
        payload = b"evil"
        info = tarfile.TarInfo("files/bad.txt")
        info.size = len(payload)
        tf.addfile(info, fileobj=deepkeep.io.BytesIO(payload))
    pack_id = db_rows(config, "SELECT pack_id FROM packs")[0][0]
    result = runner.invoke(deepkeep.cli, ["verify-pack", "--config", str(config), pack_id])
    assert result.exit_code == 1


def test_resume_pending_upload(fake_crypto, repo: tuple[Path, Path, Path], monkeypatch) -> None:
    source, storage, config_path = repo
    (source / "resume.txt").write_text("resume")
    config = deepkeep.load_config(config_path)
    db = deepkeep.connect_db(config)
    backend = deepkeep.get_backend(config)
    run_id = "run123"
    state, pack_dir = deepkeep.new_pack_state(config, run_id)
    entry = deepkeep.build_entry(source, source / "resume.txt")
    state["entries"] = [
        {
            "source": str(entry.source),
            "original_path": entry.rel_path,
            "member_path": entry.member_path,
            "size": entry.size,
            "sha256": entry.sha256,
            "mtime": entry.mtime,
        }
    ]

    calls = {"put": 0}

    def flaky_put(key: str, path: str) -> None:
        calls["put"] += 1
        if calls["put"] == 1:
            raise RuntimeError("boom")
        shutil.copy2(path, storage / key)

    monkeypatch.setattr(backend, "put_object", flaky_put)
    with pytest.raises(RuntimeError):
        deepkeep.seal_pack(config, db, backend, state)
    assert (pack_dir / "state.json").exists()
    stage = deepkeep.read_stage(pack_dir / "state.json")
    assert stage["status"] == "ENCRYPTED"

    def copy_ok(key: str, path: str) -> None:
        dest = (storage / key).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    monkeypatch.setattr(backend, "put_object", copy_ok)
    committed = deepkeep.resume_pending(config, db, backend)
    assert committed == 1
    rows = db.execute("SELECT pack_id FROM packs").fetchall()
    assert len(rows) == 1
    db.close()


def test_catalog_command_lists_files_and_backup_runs(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    (source / "a.txt").write_text("alpha")
    (source / "b.txt").write_text("beta")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(deepkeep.cli, ["catalog", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "Catalog Files" in result.output
    assert "Backup Runs" in result.output
    assert "a.txt" in result.output
    assert "b.txt" in result.output
    assert "COMPLETED" in result.output
    assert str(source) in result.output


def test_catalog_plaintext_is_pipe_friendly(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    (source / "plain.txt").write_text("hello")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(deepkeep.cli, ["catalog", "--config", str(config), "--plaintext"])
    assert result.exit_code == 0, result.output
    assert "Catalog Files" not in result.output
    assert "Backup Runs" not in result.output
    assert "FILE\tplain.txt\t" in result.output
    assert "RUN\t" in result.output
    assert str(source) in result.output
