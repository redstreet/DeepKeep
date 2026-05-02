# Backup System Architecture (v1)

## Goals

- A personal backup system to backup photos and videos
- Extremely low-cost archival storage (AWS S3 Glacier Deep Archive)
- Simple, robust, resumable backups
- File-level deduplication
- Human-recoverable without central index
- Minimal dependencies and long-term readability (50+ years)
- Fully testable locally without AWS

---

## Non-Goals

- Block-level deduplication
- Real-time sync
- Multi-writer concurrency
- Complex distributed systems

---

## High-Level Design

The system consists of:

1. **Python CLI (Click)** — orchestrates everything
2. **SQLite catalog** — fast dedupe + metadata + run history
3. **Pack files (tar)** — immutable archive units
4. **Storage backend abstraction** — local or S3
5. **Embedded manifests** — ensure recovery without DB

---

## Core Principles

- **Append-only data model**
- **Immutable packs**
- **Catalog is rebuildable**
- **Data is always readable without tooling**
- **Storage backend is interchangeable**

---

## Data Flow

### Backup Run

1. Scan input directory
2. Compute SHA-256 for each file
3. Query SQLite:
   - If exists → skip (dedupe)
   - If new → stage for packing
4. Build pack (~100–500 MB target)
5. Write `MANIFEST.json`
6. Tar → (optional zstd) → encrypt
7. Upload to backend
8. Update SQLite
9. Snapshot SQLite → backend

---

## Storage Layout (Backend-agnostic)

```
packs/YYYY/MM/pack-<id>.tar[.zst].age
manifests/YYYY/MM/pack-<id>.json
catalog/latest.sqlite.age
catalog/snapshots/catalog-<timestamp>.sqlite.age
```

---

## Pack Format

Each pack is a standard tar archive:

```
MANIFEST.json
files/<relative paths...>
```

### Compression

- Default: OFF
- Optional: `zstd`

### Encryption

- `age` (preferred)
- probably want to use one or two long passwords instead of key files to reduce
  dependencies
  - passwords will be stored in `pass`

### Pipeline

- No compression:
  ```
  tar → age
  ```
- With compression:
  ```
  tar → zstd → age
  ```

---

## Manifest (inside tar)

Each pack includes a self-describing manifest.

### Example

```json
{
  "format_version": 1,
  "created_at": "2026-04-06T22:15:00Z",
  "files": [
    {
      "member_path": "files/2021/IMG_1234.JPG",
      "original_path": "Phone/IMG_1234.JPG",
      "size": 6348291,
      "sha256": "abc...",
    }
  ]
}
```

### Purpose

- Enables recovery without SQLite
- Enables index rebuild
- Human-readable

---

## SQLite Schema

### files (dedupe)

```sql
CREATE TABLE files (
    sha256 TEXT PRIMARY KEY,
    size INTEGER,
    pack_id TEXT,
    tar_path TEXT
);
```

---

### packs

```sql
CREATE TABLE packs (
    pack_id TEXT PRIMARY KEY,
    object_key TEXT,
    created_at TEXT,
    compression TEXT,
    encryption TEXT,
    uploaded INTEGER,
    run_id TEXT
);
```

---

### file_paths (optional)

```sql
CREATE TABLE file_paths (
    path TEXT PRIMARY KEY,
    sha256 TEXT,
    mtime TEXT
);
```

---

### backup_runs

```sql
CREATE TABLE backup_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT,
    completed_at TEXT,
    machine TEXT,
    source_path TEXT,
    files_scanned INTEGER,
    files_new INTEGER,
    files_deduped INTEGER,
    bytes_new INTEGER,
    bytes_total_scanned INTEGER,
    packs_created INTEGER,
    status TEXT,
    notes TEXT
);
```

---

## Backup Run Lifecycle

States:

```
NEW → BUILDING → SEALED → ENCRYPTED → UPLOADED → COMMITTED
```

### Resume Logic

On startup:

- If tar exists → resume build
- If encrypted exists → upload
- If uploaded but not committed → finalize DB

---

## Storage Backend Interface

```python
class StorageBackend:
    def put_object(self, key: str, path: str): ...
    def get_object(self, key: str, dest_path: str): ...
    def exists(self, key: str) -> bool: ...
    def list_objects(self, prefix: str): ...
```

---

## Backends

### LocalBackend (primary for dev)

- Maps keys → filesystem paths
- Used for full local testing

### S3Backend

- Uses AWS CLI
- Storage class: `DEEP_ARCHIVE`

---

## Configuration

```yaml
backend: local | s3

local_root: /path/to/storage

bucket: my-backups
storage_class: DEEP_ARCHIVE

compression: off | zstd
pack_size_mb: 100-500
```

---

## Deduplication Strategy

- File-level (SHA-256)
- No block-level dedupe
- Same content stored once globally

---

## Recovery Model

### Normal

- Use SQLite → locate pack → restore → extract file

### Without SQLite

1. Restore pack from backend
2. Decrypt + extract tar
3. Read `MANIFEST.json`
4. Recover files

### Rebuild Index

- Scan packs
- Read manifests
- Recreate SQLite

---

## Guarantees

- Data always recoverable from packs alone
- Metadata redundancy:
  - SQLite
  - external manifests
  - embedded manifests
- No proprietary formats

---

## CLI (Click)

Suggested commands:

```
backup run <path>
backup status
backup history
backup restore <file>
```

---

## Dependencies

Required:

- Python 3
- sqlite3 (builtin)
- tar
- age
- aws CLI (for S3 backend)

Optional:

- zstd

---

## Design Tradeoffs

### Why SQLite

- Fast dedupe lookups
- Simple, single-file DB
- Strong consistency guarantees

### Why tar

- Universal, long-term readable
- No custom format risk

### Why no block dedupe

- Simpler
- Good enough for photos/videos

---

## Future Extensions (Optional)

- EXIF extraction for search
- integrity verification mode
- parallel uploads
- MinIO backend

---

## Summary

This system is:

- **Simple** (few moving parts)
- **Robust** (recoverable without index)
- **Cheap** (~$1/TB/month)
- **Portable** (standard formats only)
- **Testable** (local backend)
