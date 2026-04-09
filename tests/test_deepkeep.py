from __future__ import annotations

import os
import shutil
import socket
import sys
import tarfile
import gzip
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
                f"catalog_path: {tmp_path / 'catalog.sqlite'}",
                "pack_size_mb: 1",
                "age_pass_entry: backups/deepkeep",
                f"work_root: {tmp_path / '.work'}",
                "backend:",
                "  type: local",
                f"  root: {storage}",
            ]
        )
    )
    return source, storage, config


def db_rows(config: Path, sql: str):
    loaded = deepkeep.load_config(config)
    db = deepkeep.connect_db(loaded, persist=False)
    try:
        return db.execute(sql).fetchall()
    finally:
        deepkeep.close_db(db)


def cli_args(config: Path, *args: str) -> list[str]:
    return ["--config", str(config), *args]


def test_load_config_requires_age_pass_entry(tmp_path: Path) -> None:
    config = tmp_path / "deepkeep.yaml"
    config.write_text(
        "\n".join(
            [
                f"catalog_path: {tmp_path / 'catalog.sqlite'}",
                "pack_size_mb: 1",
                f"work_root: {tmp_path / '.work'}",
                "backend:",
                "  type: local",
                f"  root: {tmp_path / 'storage'}",
            ]
        )
    )
    with pytest.raises(deepkeep.DeepKeepError, match="age_pass_entry"):
        deepkeep.load_config(config)


def test_load_config_requires_single_backend_mapping(tmp_path: Path) -> None:
    config = tmp_path / "deepkeep.yaml"
    config.write_text(
        "\n".join(
            [
                f"catalog_path: {tmp_path / 'catalog.sqlite'}",
                "pack_size_mb: 1",
                "age_pass_entry: backups/deepkeep",
                f"work_root: {tmp_path / '.work'}",
                "backend: local",
            ]
        )
    )
    with pytest.raises(deepkeep.DeepKeepError, match="config.backend"):
        deepkeep.load_config(config)


def test_load_config_expands_user_and_env_paths(tmp_path: Path, monkeypatch) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    local_root = tmp_path / "storage"
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("DEEPKEEP_TEST_ROOT", str(local_root))
    config = tmp_path / "deepkeep.yaml"
    config.write_text(
        "\n".join(
            [
                "catalog_path: ~/catalog.sqlite",
                "pack_size_mb: 1",
                "age_pass_entry: backups/deepkeep",
                "work_root: $DEEPKEEP_TEST_ROOT/work",
                "backend:",
                "  type: local",
                "  root: $DEEPKEEP_TEST_ROOT/archive",
            ]
        )
    )

    loaded = deepkeep.load_config(config)

    assert loaded["catalog_path"] == str(home_dir / "catalog.sqlite")
    assert loaded["work_root"] == str(local_root / "work")
    assert loaded["backend"]["root"] == str(local_root / "archive")


def test_should_write_catalog_snapshot_weekly_policy() -> None:
    now = "2026-04-07T12:00:00Z"
    assert deepkeep.should_write_catalog_snapshot(now, []) is True
    assert deepkeep.should_write_catalog_snapshot(
        now,
        ["catalog/snapshots/catalog-20260401T120000Z.sqlite.gz.age"],
    ) is False
    assert deepkeep.should_write_catalog_snapshot(
        now,
        ["catalog/snapshots/catalog-20260331T115959Z.sqlite.gz.age"],
    ) is True
    assert deepkeep.should_write_catalog_snapshot(
        now,
        ["catalog/snapshots/not-a-timestamp.sqlite.gz.age"],
    ) is True


def test_snapshot_catalog_writes_latest_every_run_but_weekly_snapshots(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    catalog = tmp_path / "catalog.sqlite"
    catalog.write_text("catalog")
    config = {
        "catalog_path": str(catalog),
        "work_root": str(tmp_path / ".work"),
        "backend": {"type": "local", "root": str(storage)},
        "age_pass_entry": "backups/deepkeep",
    }
    backend = deepkeep.LocalBackend("local", storage)
    monkeypatch.setattr(deepkeep, "encrypt_file", lambda src, dest, cfg: shutil.copy2(src, dest))
    monkeypatch.setattr(deepkeep, "utc_now", lambda: "2026-04-07T12:00:00Z")

    deepkeep.snapshot_catalog(config, backend)
    latest = storage / "catalog" / "latest.sqlite.gz.age"
    snapshots = sorted((storage / "catalog" / "snapshots").glob("*.age"))
    assert latest.exists()
    assert len(snapshots) == 1

    deepkeep.snapshot_catalog(config, backend)
    snapshots = sorted((storage / "catalog" / "snapshots").glob("*.age"))
    assert latest.exists()
    assert len(snapshots) == 1


def test_upload_catalog_command_runs_quick_check_and_uploads(fake_crypto, repo: tuple[Path, Path, Path], monkeypatch) -> None:
    source, storage, config = repo
    (source / "one.txt").write_text("one")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    seen = {"quick_check": 0}
    original = deepkeep.quick_validate_catalog

    def wrapped_quick_check(db) -> None:
        seen["quick_check"] += 1
        original(db)

    monkeypatch.setattr(deepkeep, "quick_validate_catalog", wrapped_quick_check)
    result = runner.invoke(deepkeep.cli, cli_args(config, "upload-catalog"))
    assert result.exit_code == 0, result.output
    assert seen["quick_check"] == 1
    assert "Started:" in result.output
    assert "Completed:" in result.output
    assert "catalog upload" in result.output
    assert (storage / "catalog" / "latest.sqlite.gz.age").exists()


def test_backup_writes_local_catalog_as_gzip(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _storage, config = repo
    (source / "one.txt").write_text("one")
    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    catalog = config.parent / "catalog.sqlite"
    with gzip.open(catalog, "rb") as fh:
        header = fh.read(16)
    assert header.startswith(b"SQLite format 3")


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


def test_s3_restore_status_is_ready_for_standard_storage(monkeypatch) -> None:
    monkeypatch.setattr(deepkeep, "require_tool", lambda name: None)
    backend = deepkeep.S3Backend(
        "glacier",
        {"type": "s3", "bucket": "deepkeeptest", "prefix": "dkt", "storage_class": "DEEP_ARCHIVE"},
    )

    def fake_run(args, **kwargs):
        return deepkeep.subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout='{"StorageClass":"STANDARD"}',
            stderr="",
        )

    monkeypatch.setattr(deepkeep, "run", fake_run)
    assert backend.restore_status("packs/x.tar.age") == "ready"
    assert backend.request_restore("packs/x.tar.age") == "ready"


def test_s3_restore_status_uses_restore_header_for_archive_storage(monkeypatch) -> None:
    monkeypatch.setattr(deepkeep, "require_tool", lambda name: None)
    backend = deepkeep.S3Backend(
        "glacier",
        {"type": "s3", "bucket": "deepkeeptest", "prefix": "dkt", "storage_class": "DEEP_ARCHIVE"},
    )

    def fake_run(args, **kwargs):
        return deepkeep.subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout='{"StorageClass":"DEEP_ARCHIVE","Restore":"ongoing-request=\\"true\\""}',
            stderr="",
        )

    monkeypatch.setattr(deepkeep, "run", fake_run)
    assert backend.restore_status("packs/x.tar.age") == "pending"


def test_backup_dry_run_is_non_mutating_and_human_readable(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, storage, config = repo
    (source / "alpha.txt").write_text("alpha")
    (source / "beta.txt").write_text("beta")

    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "backup", "--dry-run", str(source)))
    assert result.exit_code == 0, result.output
    assert "Mode" in result.output
    assert "dry run" in result.output
    assert "Files to back up" in result.output
    assert "New data" in result.output
    assert "B" in result.output

    assert db_rows(config, "SELECT * FROM backup_runs") == []
    assert db_rows(config, "SELECT * FROM file_paths") == []
    assert db_rows(config, "SELECT * FROM run_files") == []
    assert db_rows(config, "SELECT * FROM path_versions") == []
    assert db_rows(config, "SELECT * FROM packs") == []
    assert list(storage.rglob("*")) == []
    assert not (config.parent / ".work").exists()


def test_backup_dry_run_matches_real_backup_stats(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    (source / "a.txt").write_text("one")
    (source / "b.txt").write_text("two")
    runner = CliRunner()

    dry = runner.invoke(deepkeep.cli, cli_args(config, "backup", "--dry-run", str(source)))
    assert dry.exit_code == 0, dry.output
    real = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert real.exit_code == 0, real.output

    assert "Files to back up" in dry.output
    assert "2" in dry.output
    row = db_rows(config, "SELECT files_new, packs_created FROM backup_runs ORDER BY started_at DESC")[0]
    assert row[0] == 2
    assert row[1] == 1


def test_dry_run_does_not_bind_backend_identity(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    (source / "a.txt").write_text("one")
    runner = CliRunner()

    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", "--dry-run", str(source)))
    assert result.exit_code == 0, result.output

    loaded = deepkeep.load_config(config)
    db = deepkeep.connect_db(loaded)
    row = db.execute("SELECT value FROM settings WHERE key = 'catalog_backend_identity'").fetchone()
    deepkeep.close_db(db)
    assert row is None


def test_backup_rejects_changed_backend_config(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, storage, config = repo
    (source / "one.txt").write_text("one")
    runner = CliRunner()

    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    other_storage = config.parent / "other-storage"
    other_storage.mkdir()
    config.write_text(
        "\n".join(
            [
                f"catalog_path: {config.parent / 'catalog.sqlite'}",
                "pack_size_mb: 1",
                "age_pass_entry: backups/deepkeep",
                f"work_root: {config.parent / '.work'}",
                "backend:",
                "  type: local",
                f"  root: {other_storage}",
            ]
        )
    )

    (source / "two.txt").write_text("two")
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code != 0
    assert "catalog is bound to a different backend" in result.output
    assert str(storage) in result.output
    assert str(other_storage) in result.output


def test_run_formats_called_process_error(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise deepkeep.subprocess.CalledProcessError(
            returncode=1,
            cmd=["aws", "s3", "ls", "s3://bucket/prefix"],
            output="objects",
            stderr="AccessDenied",
        )

    monkeypatch.setattr(deepkeep.subprocess, "run", boom)
    with pytest.raises(deepkeep.DeepKeepError) as exc:
        deepkeep.run(["aws", "s3", "ls", "s3://bucket/prefix"])
    message = str(exc.value)
    assert "command failed with exit code 1" in message
    assert "command: aws s3 ls s3://bucket/prefix" in message
    assert "stdout:\nobjects" in message
    assert "stderr:\nAccessDenied" in message


def test_backup_restore_verify_and_rebuild(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, storage, config = repo
    (source / "a").mkdir()
    (source / "a" / "one.txt").write_text("one")
    (source / "a" / "two.txt").write_text("two")
    (source / "dup.txt").write_text("one")

    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output
    assert "pack 1/1" in result.output
    assert " b " in result.output
    assert " e " in result.output
    assert " u " in result.output
    assert "Packs used" in result.output

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
        cli_args(config, "restore", "--dest", str(dest), "a/"),
    )
    assert result.exit_code == 0, result.output
    assert "check     finished" in result.output
    assert "download  finished" in result.output
    assert "decrypt   finished" in result.output
    assert "restore   finished" in result.output
    assert (dest / "a" / "one.txt").read_text() == "one"
    assert (dest / "a" / "two.txt").read_text() == "two"
    assert not (dest / "dup.txt").exists()

    pack_id = db_rows(config, "SELECT pack_id FROM packs")[0][0]
    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "verify-pack", pack_id))
    assert result.exit_code == 0, result.output

    catalog = config.parent / "catalog.sqlite"
    catalog.unlink()
    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "rebuild-catalog"))
    assert result.exit_code == 0, result.output
    assert len(db_rows(config, "SELECT * FROM files")) == 2
    assert len(db_rows(config, "SELECT * FROM file_paths")) == 2
    assert len(db_rows(config, "SELECT * FROM run_files")) == 0
    assert len(db_rows(config, "SELECT * FROM path_versions")) == 0


def test_dedupe_second_backup_creates_no_new_pack(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, storage, config = repo
    (source / "x.txt").write_text("same")
    runner = CliRunner()
    assert runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source))).exit_code == 0
    first_count = len(list(storage.rglob("pack-*.age")))
    assert runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source))).exit_code == 0
    assert len(list(storage.rglob("pack-*.age"))) == first_count
    assert len(db_rows(config, "SELECT * FROM run_files")) == 1
    assert len(db_rows(config, "SELECT * FROM path_versions")) == 1


def test_oversize_file_becomes_single_pack(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, storage, config = repo
    (source / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output
    packs = list(storage.rglob("pack-*.age"))
    assert len(packs) == 1


def test_restore_skips_existing_without_force(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    (source / "keep.txt").write_text("fresh")
    runner = CliRunner()
    assert runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source))).exit_code == 0
    dest = tmp_path / "restore"
    dest.mkdir()
    (dest / "keep.txt").write_text("old")
    result = runner.invoke(deepkeep.cli, cli_args(config, "restore", "--dest", str(dest), "keep"))
    assert result.exit_code == 0, result.output
    assert (dest / "keep.txt").read_text() == "old"
    result = runner.invoke(deepkeep.cli, cli_args(config, "restore", "--dest", str(dest), "--force", "keep"))
    assert result.exit_code == 0, result.output
    assert (dest / "keep.txt").read_text() == "fresh"


def test_restore_reapplies_original_mtime(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    original = source / "stamp.txt"
    original.write_text("ts")
    ts = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
    os.utime(original, (ts, ts))

    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, cli_args(config, "restore", "--dest", str(dest), "stamp"))
    assert result.exit_code == 0, result.output
    restored = dest / "stamp.txt"
    assert restored.exists()
    assert int(restored.stat().st_mtime) == int(ts)


def test_restore_deduped_alias_path(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    (source / "first.txt").write_text("same-bytes")
    (source / "alias.txt").write_text("same-bytes")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, cli_args(config, "restore", "--dest", str(dest), "alias"))
    assert result.exit_code == 0, result.output
    assert (dest / "alias.txt").read_text() == "same-bytes"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="hardlink behavior is enabled by default on Linux only")
def test_restore_duplicates_as_hardlinks_on_linux(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    (source / "first.txt").write_text("same-bytes")
    (source / "alias.txt").write_text("same-bytes")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, cli_args(config, "restore", "--dest", str(dest), "--all"))
    assert result.exit_code == 0, result.output
    first = dest / "alias.txt"
    second = dest / "first.txt"
    assert first.read_text() == "same-bytes"
    assert second.read_text() == "same-bytes"
    assert first.stat().st_ino == second.stat().st_ino


def test_restore_duplicates_as_full_copies_with_no_hardlinks(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    (source / "first.txt").write_text("same-bytes")
    (source / "alias.txt").write_text("same-bytes")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(
        deepkeep.cli,
        cli_args(config, "restore", "--dest", str(dest), "--all", "--no-hardlinks"),
    )
    assert result.exit_code == 0, result.output
    first = dest / "alias.txt"
    second = dest / "first.txt"
    assert first.read_text() == "same-bytes"
    assert second.read_text() == "same-bytes"
    assert first.stat().st_ino != second.stat().st_ino


def test_restore_duplicates_as_pointer_files_on_windows(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path, monkeypatch) -> None:
    source, _, config = repo
    (source / "first.txt").write_text("same-bytes")
    (source / "alias.txt").write_text("same-bytes")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    monkeypatch.setattr(deepkeep, "is_windows_platform", lambda: True)
    monkeypatch.setattr(deepkeep, "is_linux_platform", lambda: False)
    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, cli_args(config, "restore", "--dest", str(dest), "--all"))
    assert result.exit_code == 0, result.output
    assert (dest / "alias.txt").read_text() == "same-bytes"
    pointer = (dest / "first.txt").read_text()
    assert "deepkeep duplicate placeholder" in pointer
    assert "original: alias.txt" in pointer
    assert "sha256:" in pointer


def test_restore_all_restores_everything(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    (source / "a").mkdir()
    (source / "a" / "one.txt").write_text("one")
    (source / "two.txt").write_text("two")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, cli_args(config, "restore", "--dest", str(dest), "--all"))
    assert result.exit_code == 0, result.output
    assert (dest / "a" / "one.txt").read_text() == "one"
    assert (dest / "two.txt").read_text() == "two"


def test_restore_requires_prefix_or_all(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    (source / "only.txt").write_text("one")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, cli_args(config, "restore", "--dest", str(dest)))
    assert result.exit_code != 0
    assert "provide at least one path prefix, or use --all" in result.output


def test_restore_rejects_all_with_prefixes(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    (source / "only.txt").write_text("one")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(
        deepkeep.cli,
        cli_args(config, "restore", "--dest", str(dest), "--all", "only"),
    )
    assert result.exit_code != 0
    assert "use either PREFIXES or --all, not both" in result.output


def test_verify_detects_corruption(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, storage, config = repo
    (source / "bad.txt").write_text("good")
    runner = CliRunner()
    assert runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source))).exit_code == 0
    pack_path = next(storage.rglob("pack-*.age"))
    with tarfile.open(pack_path, "a") as tf:
        payload = b"evil"
        info = tarfile.TarInfo("files/bad.txt")
        info.size = len(payload)
        tf.addfile(info, fileobj=deepkeep.io.BytesIO(payload))
    pack_id = db_rows(config, "SELECT pack_id FROM packs")[0][0]
    result = runner.invoke(deepkeep.cli, cli_args(config, "verify-pack", pack_id))
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
    state.entries = [
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
        deepkeep.seal_pack(config, db, backend, state, "1/1")
    assert (pack_dir / "state.json").exists()
    stage = deepkeep.read_stage(pack_dir / "state.json")
    assert stage.status == "ENCRYPTED"

    def copy_ok(key: str, path: str) -> None:
        dest = (storage / key).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    seen: list[str] = []

    def capture_print(*args, **kwargs) -> None:
        seen.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr(deepkeep.console, "print", capture_print)
    monkeypatch.setattr(backend, "put_object", copy_ok)
    committed = deepkeep.resume_pending(config, db, backend)
    assert committed == 1
    assert any("upload" in line for line in seen)
    assert any("commit" in line for line in seen)
    rows = db.execute("SELECT pack_id FROM packs").fetchall()
    assert len(rows) == 1
    run_rows = db.execute("SELECT path, pack_id FROM run_files").fetchall()
    assert len(run_rows) == 1
    assert run_rows[0][0] == "resume.txt"
    deepkeep.close_db(db)


def test_backup_rerun_after_failed_upload_reports_resume_scan(fake_crypto, repo: tuple[Path, Path, Path], monkeypatch) -> None:
    source, _, config = repo
    (source / "resume.txt").write_text("resume")
    runner = CliRunner()
    calls = {"put": 0}

    def flaky_put(self, key: str, path: str) -> None:
        calls["put"] += 1
        if calls["put"] == 1:
            raise RuntimeError("boom")
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    monkeypatch.setattr(deepkeep.LocalBackend, "put_object", flaky_put)
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code != 0

    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output
    assert "Found unfinished backup state" in result.output
    assert "upload" in result.output
    assert "commit" in result.output


def test_catalog_default_lists_summary_and_runs(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    (source / "a.txt").write_text("alpha")
    (source / "b.txt").write_text("beta")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    result = runner.invoke(deepkeep.cli, cli_args(config, "catalog"))
    assert result.exit_code == 0, result.output
    assert "Catalog Summary" in result.output
    assert "Backend" in result.output
    assert "Backup Runs" in result.output
    assert "COMPLETED" in result.output
    assert socket.gethostname() in result.output
    assert "Run sources:" not in result.output


def test_catalog_plaintext_is_pipe_friendly(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    (source / "plain.txt").write_text("hello")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    result = runner.invoke(deepkeep.cli, cli_args(config, "catalog", "--plaintext"))
    assert result.exit_code == 0, result.output
    assert "Catalog Summary" not in result.output
    assert "Backup Runs" not in result.output
    assert "SUMMARY\t" in result.output
    assert f"SUMMARY\tlocal:{config.parent / 'storage'}\t" in result.output
    assert "RUN\t" in result.output
    assert socket.gethostname() in result.output
    assert str(source) in result.output


def test_catalog_run_shows_files_for_specific_backup(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    (source / "one.txt").write_text("one")
    runner = CliRunner()
    first = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert first.exit_code == 0, first.output
    first_run = db_rows(config, "SELECT run_id FROM backup_runs ORDER BY started_at DESC")[0][0]

    (source / "two.txt").write_text("two")
    second = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert second.exit_code == 0, second.output

    result = runner.invoke(deepkeep.cli, cli_args(config, "catalog", "run", first_run))
    assert result.exit_code == 0, result.output
    assert "Run Detail" in result.output
    assert "Run Files" in result.output
    assert socket.gethostname() in result.output
    assert "one.txt" in result.output
    assert "two.txt" not in result.output


def test_catalog_files_supports_run_filter_and_plaintext(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    (source / "keep-a.txt").write_text("a")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output
    run_id = db_rows(config, "SELECT run_id FROM backup_runs ORDER BY started_at DESC")[0][0]

    (source / "keep-b.txt").write_text("b")
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        deepkeep.cli,
        cli_args(config, "catalog", "files", "--run-id", run_id, "--plaintext"),
    )
    assert result.exit_code == 0, result.output
    assert "FILE\tkeep-a.txt\t" in result.output
    assert "keep-b.txt" not in result.output


def test_catalog_packs_and_file_detail(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    (source / "detail.txt").write_text("detail")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    result = runner.invoke(deepkeep.cli, cli_args(config, "catalog", "packs", "--plaintext"))
    assert result.exit_code == 0, result.output
    assert "PACK\t" in result.output

    result = runner.invoke(deepkeep.cli, cli_args(config, "catalog", "file", "detail.txt", "--plaintext"))
    assert result.exit_code == 0, result.output
    assert "FILE_DETAIL\tdetail.txt\t" in result.output


def test_restore_can_restore_older_version_as_of_run(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    target = source / "versioned.txt"
    target.write_text("old")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output
    first_run = db_rows(config, "SELECT run_id FROM backup_runs ORDER BY started_at ASC")[0][0]

    target.write_text("new")
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    latest_dest = tmp_path / "latest"
    result = runner.invoke(deepkeep.cli, cli_args(config, "restore", "--dest", str(latest_dest), "versioned"))
    assert result.exit_code == 0, result.output
    assert (latest_dest / "versioned.txt").read_text() == "new"

    old_dest = tmp_path / "old"
    result = runner.invoke(
        deepkeep.cli,
        cli_args(config, "restore", "--dest", str(old_dest), "--as-of-run", first_run, "versioned"),
    )
    assert result.exit_code == 0, result.output
    assert (old_dest / "versioned.txt").read_text() == "old"


def test_catalog_file_history_lists_versions(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    target = source / "history.txt"
    target.write_text("v1")
    runner = CliRunner()
    assert runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source))).exit_code == 0
    target.write_text("v2")
    assert runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source))).exit_code == 0

    result = runner.invoke(deepkeep.cli, cli_args(config, "catalog", "file", "history.txt", "--plaintext"))
    assert result.exit_code == 0, result.output
    assert result.output.count("FILE_DETAIL\thistory.txt\t") == 1
    assert result.output.rstrip().endswith("\t2")

    result = runner.invoke(deepkeep.cli, cli_args(config, "catalog", "file-history", "history.txt", "--plaintext"))
    assert result.exit_code == 0, result.output
    assert result.output.count("FILE_VERSION\thistory.txt\t") == 2


def test_verify_catalog_command_reports_consistency_issues(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    (source / "verify.txt").write_text("hello")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    loaded = deepkeep.load_config(config)
    db = deepkeep.connect_db(loaded)
    db.execute("DELETE FROM packs")
    db.commit()
    deepkeep.close_db(db)

    result = runner.invoke(deepkeep.cli, cli_args(config, "verify-catalog"))
    assert result.exit_code == 1
    assert "files reference missing packs rows" in result.output


def test_restore_as_of_run_requires_known_run(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path) -> None:
    source, _, config = repo
    (source / "only.txt").write_text("one")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    dest = tmp_path / "restore"
    result = runner.invoke(
        deepkeep.cli,
        cli_args(config, "restore", "--dest", str(dest), "--as-of-run", "missing-run", "only"),
    )
    assert result.exit_code != 0
    assert "Error: unknown run_id: missing-run" in result.output


def test_restore_reports_pending_archive_pack(fake_crypto, repo: tuple[Path, Path, Path], tmp_path: Path, monkeypatch) -> None:
    source, _, config = repo
    (source / "cold.txt").write_text("cold")
    runner = CliRunner()
    result = runner.invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output

    statuses = iter(["cold", "pending"])

    def fake_restore_status(self, key: str) -> str:
        return next(statuses)

    monkeypatch.setattr(deepkeep.LocalBackend, "restore_status", fake_restore_status)
    dest = tmp_path / "restore"
    result = runner.invoke(deepkeep.cli, cli_args(config, "restore", "--dest", str(dest), "cold"))
    assert result.exit_code == 0, result.output
    assert "check     finished" in result.output
    assert "requesting restore finished" in result.output
    assert "packs pending glacier restore: 1" in result.output


def test_cli_reports_deepkeep_errors_cleanly(fake_crypto, repo: tuple[Path, Path, Path], monkeypatch) -> None:
    source, _, config = repo
    (source / "file.txt").write_text("x")

    def fail_backup(*args, **kwargs):
        raise deepkeep.DeepKeepError("command failed with exit code 1\nstderr:\nAccessDenied")

    monkeypatch.setattr(deepkeep, "backup_source", fail_backup)
    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code != 0
    assert "Error: command failed with exit code 1" in result.output
    assert "AccessDenied" in result.output
    assert "Traceback" not in result.output


def test_backup_marks_run_failed_when_snapshot_errors(fake_crypto, repo: tuple[Path, Path, Path], monkeypatch) -> None:
    source, _, config = repo
    (source / "fail.txt").write_text("data")
    monkeypatch.setattr(
        deepkeep,
        "snapshot_catalog",
        lambda *args, **kwargs: (_ for _ in ()).throw(deepkeep.DeepKeepError("catalog snapshot failed\nstderr:\nboom")),
    )

    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code != 0
    assert "pack 1/1" in result.output
    assert " u " in result.output
    row = db_rows(config, "SELECT status, notes, files_new, packs_created FROM backup_runs ORDER BY started_at DESC")[0]
    assert row[0] == "PARTIAL"
    assert row[1] == "catalog snapshot failed"
    assert row[2] == 1
    assert row[3] == 1


def test_backup_marks_run_failed_when_quick_check_errors(fake_crypto, repo: tuple[Path, Path, Path], monkeypatch) -> None:
    source, _, config = repo
    (source / "fail.txt").write_text("data")
    monkeypatch.setattr(
        deepkeep,
        "quick_validate_catalog",
        lambda db: (_ for _ in ()).throw(deepkeep.DeepKeepError("catalog quick check failed\ncorruption")),
    )

    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code != 0
    assert "catalog quick check failed" in result.output
    row = db_rows(config, "SELECT status, notes FROM backup_runs ORDER BY started_at DESC")[0]
    assert row[0] == "PARTIAL"
    assert row[1] == "catalog quick check failed"


def test_new_backup_marks_stale_running_rows_failed(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    config_data = deepkeep.load_config(config)
    db = deepkeep.connect_db(config_data)
    db.execute(
        "INSERT INTO backup_runs(run_id, started_at, machine, source_path, status) VALUES (?, ?, ?, ?, ?)",
        ("stale123", "2026-04-09T03:17:38Z", "LIONLAP", "/tmp/source", "RUNNING"),
    )
    db.commit()
    deepkeep.close_db(db)

    (source / "fresh.txt").write_text("fresh")
    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output
    stale = db_rows(config, "SELECT status, notes FROM backup_runs WHERE run_id = 'stale123'")[0]
    assert stale[0] == "FAILED"
    assert "marked incomplete after a later backup detected an unfinished run" in stale[1]


def test_new_backup_marks_stale_running_rows_partial_when_committed_packs_exist(fake_crypto, repo: tuple[Path, Path, Path]) -> None:
    source, _, config = repo
    config_data = deepkeep.load_config(config)
    db = deepkeep.connect_db(config_data)
    db.execute(
        "INSERT INTO backup_runs(run_id, started_at, machine, source_path, status) VALUES (?, ?, ?, ?, ?)",
        ("partial123", "2026-04-09T03:17:38Z", "LIONLAP", "/tmp/source", "RUNNING"),
    )
    db.execute(
        "INSERT INTO packs(pack_id, object_key, created_at, compression, encryption, uploaded, run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("pack123", "packs/2026/04/pack-pack123.tar.age", "2026-04-09T03:18:00Z", "off", "age", 1, "partial123"),
    )
    db.commit()
    deepkeep.close_db(db)

    (source / "fresh.txt").write_text("fresh")
    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code == 0, result.output
    stale = db_rows(config, "SELECT status, notes FROM backup_runs WHERE run_id = 'partial123'")[0]
    assert stale[0] == "PARTIAL"
    assert "marked incomplete after a later backup detected an unfinished run" in stale[1]


def test_backup_reports_failed_stage(fake_crypto, repo: tuple[Path, Path, Path], monkeypatch) -> None:
    source, _, config = repo
    (source / "oops.txt").write_text("oops")
    monkeypatch.setattr(deepkeep, "encrypt_file", lambda src, dest, cfg: (_ for _ in ()).throw(deepkeep.DeepKeepError("nope")))
    result = CliRunner().invoke(deepkeep.cli, cli_args(config, "backup", str(source)))
    assert result.exit_code != 0
    assert "pack 1/1" in result.output
    assert "encrypt" in result.output
    assert "failed" in result.output


def test_cli_uses_deepkeep_config_env_var(fake_crypto, repo: tuple[Path, Path, Path], monkeypatch) -> None:
    source, _, config = repo
    (source / "env.txt").write_text("env")
    monkeypatch.setenv("DEEPKEEP_CONFIG", str(config))

    result = CliRunner().invoke(deepkeep.cli, ["backup", str(source)])
    assert result.exit_code == 0, result.output
    row = db_rows(config, "SELECT files_new FROM backup_runs ORDER BY started_at DESC")[0]
    assert row[0] == 1
