# DeepKeep

DeepKeep is a single-file archival backup CLI for photos and videos.

It:

- scans a source directory in deterministic order
- deduplicates at the file level by SHA-256
- packs new files into tar archives
- encrypts them with `age` using `age-plugin-batchpass`
- stores them in either a local filesystem backend or S3
- keeps a local SQLite catalog for fast browsing and restore

## Prerequisites

Required tools:

- Python 3.12+
- `pass`
- `age`
- `age-plugin-batchpass`
- AWS CLI if you use the S3 backend

Install Python dependencies:

```bash
python3 -m pip install click PyYAML rich
```

Or use the shared virtualenv in this repo setup:

```bash
~/.venv/ai/bin/python deepkeep.py --help
```

For command config selection:

- use `--config /path/to/deepkeep.yaml` on the main command, or
- set `DEEPKEEP_CONFIG=/path/to/deepkeep.yaml`

If neither is set, DeepKeep defaults to `deepkeep.yaml` in the current directory.

## Encryption Setup

DeepKeep uses `pass` plus `age-plugin-batchpass` for non-interactive passphrase encryption.

Create a pass entry:

```bash
pass insert backups/deepkeep
```

Your YAML config should reference that entry:

```yaml
age_pass_entry: backups/deepkeep
```

DeepKeep reads the first line of that pass entry and sets `AGE_PASSPHRASE_FD` internally for the `age` subprocess. You do not need to export `AGE_PASSPHRASE_FD` yourself.

## Backend Selection

DeepKeep uses exactly one backend in the YAML config.

```yaml
backend:
  type: s3
  bucket: my-backups
  prefix: deepkeep
  storage_class: DEEP_ARCHIVE
```

The first real backup binds the catalog to that exact backend config. If you later change the backend block for the same catalog, `backup` will fail instead of writing to a different target.

## Local Backend Example

```yaml
catalog_path: /path/to/catalog.sqlite.gz
pack_size_mb: 512
age_pass_entry: backups/deepkeep
work_root: /path/to/work

backend:
  type: local
  root: /path/to/local-storage
```

## S3 Backend Example

```yaml
catalog_path: /path/to/catalog.sqlite.gz
pack_size_mb: 512
age_pass_entry: backups/deepkeep
work_root: /path/to/work

backend:
  type: s3
  bucket: my-backups
  prefix: deepkeep
  storage_class: DEEP_ARCHIVE
```

Field notes:

- `catalog_path`: path to the local catalog; DeepKeep normally stores it gzip-compressed, but plain SQLite is also accepted after an interrupted run
- `pack_size_mb`: minimum pack target size
- `age_pass_entry`: `pass` entry holding the encryption passphrase
- `work_root`: local staging directory for in-progress packs
- `backend.type`: `local` or `s3`
- `root`: local backend storage root
- `bucket`: S3 bucket name
- `prefix`: object prefix inside the bucket
- `storage_class`: S3 storage class used for uploads

## AWS Authentication

DeepKeep does not implement separate S3 authentication logic. It shells out to the AWS CLI, so it uses whatever credentials the AWS CLI is already using.

Any normal AWS CLI auth setup works, for example:

- `aws configure`
- `~/.aws/credentials`
- `~/.aws/config`
- environment variables like `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_PROFILE`
- IAM role credentials on an AWS machine

Before running DeepKeep against S3, verify AWS CLI auth:

```bash
aws sts get-caller-identity
```

If that works in your shell, DeepKeep should be able to use the same credentials.

## Must I Create The Bucket First?

Yes.

DeepKeep does not create buckets. The current S3 backend only performs object operations such as upload, download, listing, and object metadata lookups.

Create the bucket ahead of time.

Example for `us-east-1`:

```bash
aws s3api create-bucket --bucket my-backups --region us-east-1
```

Example for another region like `us-west-2`:

```bash
aws s3api create-bucket \
  --bucket my-backups \
  --region us-west-2 \
  --create-bucket-configuration LocationConstraint=us-west-2
```

## First S3 Backup Checklist

1. Confirm AWS auth:

```bash
aws sts get-caller-identity
```

2. Confirm the bucket exists:

```bash
aws s3 ls
```

3. Create a config file, for example `deepkeep.yaml`:

```yaml
catalog_path: /absolute/path/to/catalog.sqlite.gz
pack_size_mb: 512
age_pass_entry: backups/deepkeep
work_root: /absolute/path/to/work

backend:
  type: s3
  bucket: my-backups
  prefix: deepkeep
  storage_class: STANDARD
```

4. Run a backup:

```bash
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml backup /path/to/source
```

5. Inspect uploaded objects:

```bash
aws s3 ls s3://my-backups/deepkeep/ --recursive
```

## Recommended First S3 Test

For your first live S3 run, use:

```yaml
storage_class: STANDARD
```

Once upload/list/download behavior is working, switch to:

```yaml
storage_class: DEEP_ARCHIVE
```

This avoids Glacier restore complexity while you are still validating basic connectivity and object layout.

## Current Storage Layout

DeepKeep stores objects like this:

```text
packs/YYYY/MM/pack-<id>.tar.age
catalog/latest.sqlite.gz.age
catalog/snapshots/catalog-<timestamp>.sqlite.gz.age
```

Notes:

- `catalog/latest.sqlite.gz.age` is uploaded on every successful backup
- historical catalog snapshots are uploaded at most once per week
- catalog uploads are compressed with gzip before age encryption
- under the current implementation, catalog objects use the same backend and storage class as pack objects

## Commands

Backup:

```bash
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml backup /path/to/source
```

Restore a prefix:

```bash
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml restore --dest /restore/path photos/2024/
```

Restore everything:

```bash
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml restore --dest /restore/path --all
```

Disable Linux hardlink optimization during restore:

```bash
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml restore --dest /restore/path --all --no-hardlinks
```

Verify a pack:

```bash
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml verify-pack <pack_id>
```

Rebuild the local catalog from stored packs:

```bash
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml rebuild-catalog
```

Browse the catalog:

```bash
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml catalog
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml catalog runs
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml catalog files --prefix photos/
~/.venv/ai/bin/python deepkeep.py --config deepkeep.yaml catalog --plaintext
```

## Current Limitations

- The S3 backend currently targets real AWS via the AWS CLI.
- Custom S3 endpoints such as MinIO or LocalStack are not yet supported.
- The bucket must already exist.
- If you use `DEEP_ARCHIVE`, catalog snapshots are also uploaded with that same storage class under the current implementation.
- Glacier restore behavior exists, but first-time S3 bring-up is easier with `STANDARD`.
