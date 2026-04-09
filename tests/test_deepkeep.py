from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import sys
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
def repo(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "source"
    storage = tmp_path / "storage"
    localcopy = tmp_path / "localcopy"
    source.mkdir()
    storage.mkdir()
    localcopy.mkdir()
    config = tmp_path / "deepkeep.yaml"
    config.write_text(
        "\n".join(
            [
                "default_backend: glacier",
                f"catalog_path: {tmp_path / 'catalog.sqlite'}",
                "pack_size_mb: 1",
                "age_pass_entry: backups/deepkeep",
                f"work_root: {tmp_path / '.work'}",
                "backends:",
                "  glacier:",
                "    type: local",
                f"    root: {storage}",
                "  localcopy:",
                "    type: local",
                f"    root: {localcopy}",
            ]
        )
    )
    return source, storage, localcopy, config


def db_rows(config: Path, sql: str):
    db = sqlite3.connect(str(config.parent / "catalog.sqlite"))
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_load_config_requires_age_pass_entry(tmp_path: Path) -> None:
    config = tmp_path / "deepkeep.yaml"
    config.write_text(
        "\n".join(
            [
                "backend: local",
                f"catalog_path: {tmp_path / 'catalog.sqlite'}",
                "pack_size_mb: 1",
                f"work_root: {tmp_path / '.work'}",
                "local:",
                f"  root: {tmp_path / 'storage'}",
            ]
        )
    )
    with pytest.raises(deepkeep.DeepKeepError, match="age_pass_entry"):
        deepkeep.load_config(config)


def test_load_config_requires_named_backends(tmp_path: Path) -> None:
    config = tmp_path / "deepkeep.yaml"
    config.write_text(
        "\n".join(
            [
                "backend: local",
                f"catalog_path: {tmp_path / 'catalog.sqlite'}",
                "pack_size_mb: 1",
                "age_pass_entry: backups/deepkeep",
                f"work_root: {tmp_path / '.work'}",
                "local:",
                f"  root: {tmp_path / 'storage'}",
            ]
        )
    )
    with pytest.raises(deepkeep.DeepKeepError, match="config.backends"):
        deepkeep.load_config(config)


def test_should_write_catalog_snapshot_weekly_policy() -> None:
    now = "2026-04-07T12:00:00Z"
    assert deepkeep.should_write_catalog_snapshot(now, []) is True
    assert deepkeep.should_write_catalog_snapshot(
        now,
        ["catalog/snapshots/catalog-20260401T120000Z.sqlite.age"],
    ) is False
    assert deepkeep.should_write_catalog_snapshot(
        now,
        ["catalog/snapshots/catalog-20260331T115959Z.sqlite.age"],
    ) is True
    assert deepkeep.should_write_catalog_snapshot(
        now,
        ["catalog/snapshots/not-a-timestamp.sqlite.age"],
    ) is True


def test_snapshot_catalog_writes_latest_every_run_but_weekly_snapshots(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    catalog = tmp_path / "catalog.sqlite"
    catalog.write_text("catalog")
    config = {
        "catalog_path": str(catalog),
        "work_root": str(tmp_path / ".work"),
        "default_backend": "default",
        "backends": {"default": {"type": "local", "root": str(storage)}},
        "age_pass_entry": "backups/deepkeep",
    }
    backend = deepkeep.LocalBackend("default", storage)
    monkeypatch.setattr(deepkeep, "encrypt_file", lambda src, dest, cfg: shutil.copy2(src, dest))
    monkeypatch.setattr(deepkeep, "utc_now", lambda: "2026-04-07T12:00:00Z")

    deepkeep.snapshot_catalog(config, backend)
    latest = storage / "catalog" / "latest.sqlite.age"
    snapshots = sorted((storage / "catalog" / "snapshots").glob("*.age"))
    assert latest.exists()
    assert len(snapshots) == 1

    deepkeep.snapshot_catalog(config, backend)
    snapshots = sorted((storage / "catalog" / "snapshots").glob("*.age"))
    assert latest.exists()
    assert len(snapshots) == 1


def test_s3_list_objects_returns_empty_for_missing_prefix(monkeypatch) -> None:
    monkeypatch.setattr(deepkeep, "require_tool", lambda name: None)
    backend = deepkeep.S3Backend(
        "glacier",
        {"type": "s3", "bucket": "deepkeeptest", "prefix": "dkt", "storage_class": "DEEP_ARCHIVE"},
    )

    def fake_run(args, **kwargs):
        return deepkeep.subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="NoSuchKey")

    monkeypatch.setattr(deepkeep, "run", fake_run)
    assert backend.list_objects("catalog/snapshots") == []


def test_backup_restore_verify_and_rebuild(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, storage, _, config = repo
    (source / "a").mkdir()
    (source / "a" / "one.txt").write_text("one")
    (source / "a" / "two.txt").write_text("two")
    (source / "dup.txt").write_text("one")

    result = CliRunner().invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    pack_files = sorted(storage.rglob("*.age"))
    assert any("pack-" in path.name for path in pack_files)
    assert any("catalog" in path.as_posix() for path in pack_files)
    assert len(db_rows(config, "SELECT * FROM files")) == 2
    assert len(db_rows(config, "SELECT * FROM file_paths")) == 3
    assert len(db_rows(config, "SELECT * FROM run_files")) == 2
    assert len(db_rows(config, "SELECT * FROM path_versions")) == 3

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
    result = CliRunner().invoke(deepkeep.cli, ["rebuild-catalog", "--config", str(config), "--backend", "glacier"])
    assert result.exit_code == 0, result.output
    assert len(db_rows(config, "SELECT * FROM files")) == 2
    assert len(db_rows(config, "SELECT * FROM file_paths")) == 2
    assert len(db_rows(config, "SELECT * FROM run_files")) == 0
    assert len(db_rows(config, "SELECT * FROM path_versions")) == 0


def test_dedupe_second_backup_creates_no_new_pack(fake_crypto, repo: tuple[Path, Path, Path, Path]) -> None:
    source, storage, _, config = repo
    (source / "x.txt").write_text("same")
    runner = CliRunner()
    assert runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)]).exit_code == 0
    first_count = len(list(storage.rglob("pack-*.age")))
    assert runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)]).exit_code == 0
    assert len(list(storage.rglob("pack-*.age"))) == first_count
    assert len(db_rows(config, "SELECT * FROM run_files")) == 1
    assert len(db_rows(config, "SELECT * FROM path_versions")) == 1


def test_oversize_file_becomes_single_pack(fake_crypto, repo: tuple[Path, Path, Path, Path]) -> None:
    source, storage, _, config = repo
    (source / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    result = CliRunner().invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output
    packs = list(storage.rglob("pack-*.age"))
    assert len(packs) == 1


def test_restore_skips_existing_without_force(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, _, _, config = repo
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


def test_restore_reapplies_original_mtime(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, _, _, config = repo
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


def test_restore_deduped_alias_path(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, _, _, config = repo
    (source / "first.txt").write_text("same-bytes")
    (source / "alias.txt").write_text("same-bytes")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, ["restore", "--config", str(config), "--dest", str(dest), "alias"])
    assert result.exit_code == 0, result.output
    assert (dest / "alias.txt").read_text() == "same-bytes"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="hardlink behavior is enabled by default on Linux only")
def test_restore_duplicates_as_hardlinks_on_linux(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, _, _, config = repo
    (source / "first.txt").write_text("same-bytes")
    (source / "alias.txt").write_text("same-bytes")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, ["restore", "--config", str(config), "--dest", str(dest), "--all"])
    assert result.exit_code == 0, result.output
    first = dest / "alias.txt"
    second = dest / "first.txt"
    assert first.read_text() == "same-bytes"
    assert second.read_text() == "same-bytes"
    assert first.stat().st_ino == second.stat().st_ino


def test_restore_duplicates_as_full_copies_with_no_hardlinks(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, _, _, config = repo
    (source / "first.txt").write_text("same-bytes")
    (source / "alias.txt").write_text("same-bytes")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(
        deepkeep.cli,
        ["restore", "--config", str(config), "--dest", str(dest), "--all", "--no-hardlinks"],
    )
    assert result.exit_code == 0, result.output
    first = dest / "alias.txt"
    second = dest / "first.txt"
    assert first.read_text() == "same-bytes"
    assert second.read_text() == "same-bytes"
    assert first.stat().st_ino != second.stat().st_ino


def test_restore_duplicates_as_pointer_files_on_windows(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path, monkeypatch) -> None:
    source, _, _, config = repo
    (source / "first.txt").write_text("same-bytes")
    (source / "alias.txt").write_text("same-bytes")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    monkeypatch.setattr(deepkeep, "is_windows_platform", lambda: True)
    monkeypatch.setattr(deepkeep, "is_linux_platform", lambda: False)
    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, ["restore", "--config", str(config), "--dest", str(dest), "--all"])
    assert result.exit_code == 0, result.output
    assert (dest / "alias.txt").read_text() == "same-bytes"
    pointer = (dest / "first.txt").read_text()
    assert "deepkeep duplicate placeholder" in pointer
    assert "original: alias.txt" in pointer
    assert "sha256:" in pointer


def test_restore_all_restores_everything(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, _, _, config = repo
    (source / "a").mkdir()
    (source / "a" / "one.txt").write_text("one")
    (source / "two.txt").write_text("two")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, ["restore", "--config", str(config), "--dest", str(dest), "--all"])
    assert result.exit_code == 0, result.output
    assert (dest / "a" / "one.txt").read_text() == "one"
    assert (dest / "two.txt").read_text() == "two"


def test_restore_requires_prefix_or_all(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, _, _, config = repo
    (source / "only.txt").write_text("one")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, ["restore", "--config", str(config), "--dest", str(dest)])
    assert result.exit_code != 0
    assert "provide at least one path prefix, or use --all" in result.output


def test_restore_rejects_all_with_prefixes(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, _, _, config = repo
    (source / "only.txt").write_text("one")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(
        deepkeep.cli,
        ["restore", "--config", str(config), "--dest", str(dest), "--all", "only"],
    )
    assert result.exit_code != 0
    assert "use either PREFIXES or --all, not both" in result.output


def test_verify_detects_corruption(fake_crypto, repo: tuple[Path, Path, Path, Path]) -> None:
    source, storage, _, config = repo
    (source / "bad.txt").write_text("good")
    runner = CliRunner()
    assert runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)]).exit_code == 0
    pack_path = next(storage.rglob("pack-*.age"))
    with tarfile.open(pack_path, "a") as tf:
        payload = b"evil"
        info = tarfile.TarInfo("files/bad.txt")
        info.size = len(payload)
        tf.addfile(info, fileobj=deepkeep.io.BytesIO(payload))
    pack_id = db_rows(config, "SELECT pack_id FROM packs")[0][0]
    result = runner.invoke(deepkeep.cli, ["verify-pack", "--config", str(config), pack_id])
    assert result.exit_code == 1


def test_resume_pending_upload(fake_crypto, repo: tuple[Path, Path, Path, Path], monkeypatch) -> None:
    source, storage, _, config_path = repo
    (source / "resume.txt").write_text("resume")
    config = deepkeep.load_config(config_path)
    db = deepkeep.connect_db(config)
    backend = deepkeep.get_default_backend(config)
    run_id = "run123"
    state, pack_dir = deepkeep.new_pack_state(config, run_id, backend.backend_name)
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
    run_rows = db.execute("SELECT path, pack_id FROM run_files").fetchall()
    assert len(run_rows) == 1
    assert run_rows[0][0] == "resume.txt"
    db.close()


def test_catalog_default_lists_summary_and_runs(fake_crypto, repo: tuple[Path, Path, Path, Path]) -> None:
    source, _, _, config = repo
    (source / "a.txt").write_text("alpha")
    (source / "b.txt").write_text("beta")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(deepkeep.cli, ["catalog", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "Catalog Summary" in result.output
    assert "Backup Runs" in result.output
    assert "COMPLETED" in result.output
    assert socket.gethostname() in result.output
    assert "Run sources:" not in result.output


def test_catalog_plaintext_is_pipe_friendly(fake_crypto, repo: tuple[Path, Path, Path, Path]) -> None:
    source, _, _, config = repo
    (source / "plain.txt").write_text("hello")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(deepkeep.cli, ["catalog", "--config", str(config), "--plaintext"])
    assert result.exit_code == 0, result.output
    assert "Catalog Summary" not in result.output
    assert "Backup Runs" not in result.output
    assert "SUMMARY\t" in result.output
    assert "RUN\t" in result.output
    assert socket.gethostname() in result.output
    assert str(source) in result.output


def test_catalog_run_shows_files_for_specific_backup(fake_crypto, repo: tuple[Path, Path, Path, Path]) -> None:
    source, _, _, config = repo
    (source / "one.txt").write_text("one")
    runner = CliRunner()
    first = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert first.exit_code == 0, first.output
    first_run = db_rows(config, "SELECT run_id FROM backup_runs ORDER BY started_at DESC")[0][0]

    (source / "two.txt").write_text("two")
    second = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert second.exit_code == 0, second.output

    result = runner.invoke(deepkeep.cli, ["catalog", "--config", str(config), "run", first_run])
    assert result.exit_code == 0, result.output
    assert "Run Detail" in result.output
    assert "Run Files" in result.output
    assert socket.gethostname() in result.output
    assert "one.txt" in result.output
    assert "two.txt" not in result.output


def test_catalog_files_supports_run_filter_and_plaintext(fake_crypto, repo: tuple[Path, Path, Path, Path]) -> None:
    source, _, _, config = repo
    (source / "keep-a.txt").write_text("a")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output
    run_id = db_rows(config, "SELECT run_id FROM backup_runs ORDER BY started_at DESC")[0][0]

    (source / "keep-b.txt").write_text("b")
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        deepkeep.cli,
        ["catalog", "--config", str(config), "files", "--run-id", run_id, "--plaintext"],
    )
    assert result.exit_code == 0, result.output
    assert "FILE\tkeep-a.txt\t" in result.output
    assert "keep-b.txt" not in result.output


def test_catalog_packs_and_file_detail(fake_crypto, repo: tuple[Path, Path, Path, Path]) -> None:
    source, _, _, config = repo
    (source / "detail.txt").write_text("detail")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(deepkeep.cli, ["catalog", "--config", str(config), "packs", "--plaintext"])
    assert result.exit_code == 0, result.output
    assert "PACK\t" in result.output
    assert "\tglacier\t" in result.output

    result = runner.invoke(deepkeep.cli, ["catalog", "--config", str(config), "file", "detail.txt", "--plaintext"])
    assert result.exit_code == 0, result.output
    assert "FILE_DETAIL\tdetail.txt\t" in result.output
    assert "\tglacier\t" in result.output


def test_restore_can_restore_older_version_as_of_run(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, _, _, config = repo
    target = source / "versioned.txt"
    target.write_text("old")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output
    first_run = db_rows(config, "SELECT run_id FROM backup_runs ORDER BY started_at ASC")[0][0]

    target.write_text("new")
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    latest_dest = tmp_path / "latest"
    result = runner.invoke(deepkeep.cli, ["restore", "--config", str(config), "--dest", str(latest_dest), "versioned"])
    assert result.exit_code == 0, result.output
    assert (latest_dest / "versioned.txt").read_text() == "new"

    old_dest = tmp_path / "old"
    result = runner.invoke(
        deepkeep.cli,
        ["restore", "--config", str(config), "--dest", str(old_dest), "--as-of-run", first_run, "versioned"],
    )
    assert result.exit_code == 0, result.output
    assert (old_dest / "versioned.txt").read_text() == "old"


def test_catalog_file_history_lists_versions(fake_crypto, repo: tuple[Path, Path, Path, Path]) -> None:
    source, _, _, config = repo
    target = source / "history.txt"
    target.write_text("v1")
    runner = CliRunner()
    assert runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)]).exit_code == 0
    target.write_text("v2")
    assert runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)]).exit_code == 0

    result = runner.invoke(deepkeep.cli, ["catalog", "--config", str(config), "file", "history.txt", "--plaintext"])
    assert result.exit_code == 0, result.output
    assert result.output.count("FILE_DETAIL\thistory.txt\t") == 1
    assert result.output.rstrip().endswith("\t2")

    result = runner.invoke(deepkeep.cli, ["catalog", "--config", str(config), "file-history", "history.txt", "--plaintext"])
    assert result.exit_code == 0, result.output
    assert result.output.count("FILE_VERSION\thistory.txt\t") == 2


def test_restore_as_of_run_requires_known_run(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, _, _, config = repo
    (source / "only.txt").write_text("one")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(
        deepkeep.cli,
        ["restore", "--config", str(config), "--dest", str(dest), "--as-of-run", "missing-run", "only"],
    )
    assert result.exit_code != 0
    assert "unknown run_id" in str(result.exception)


def test_restore_can_override_backend_source(fake_crypto, repo: tuple[Path, Path, Path, Path], tmp_path: Path) -> None:
    source, storage, localcopy, config = repo
    (source / "override.txt").write_text("from-glacier-layout")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, ["backup", "--config", str(config), str(source)])
    assert result.exit_code == 0, result.output

    shutil.copytree(storage / "packs", localcopy / "packs", dirs_exist_ok=True)
    dest = tmp_path / "restore"
    result = runner.invoke(
        deepkeep.cli,
        ["restore", "--config", str(config), "--backend", "localcopy", "--dest", str(dest), "--all"],
    )
    assert result.exit_code == 0, result.output
    assert (dest / "override.txt").read_text() == "from-glacier-layout"

    pack_backend = db_rows(config, "SELECT backend_name FROM packs")[0][0]
    assert pack_backend == "glacier"
