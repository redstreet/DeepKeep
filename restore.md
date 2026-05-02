# Restoring From S3 Glacier Deep Archive

## Short version

If your DeepKeep packs are in **S3 Glacier Deep Archive**, the cheapest retrieval option is **Bulk**.

DeepKeep now defaults to:

- `Tier=Bulk`
- `Days=5`

These defaults are configurable in your YAML file.

## What AWS does

For Deep Archive, `restore-object` does **not** permanently move the object out of Glacier. AWS creates a **temporary restored copy in the same bucket** for the number of days you request.

- The original object stays in `DEEP_ARCHIVE`
- The temporary restored copy expires automatically
- If you want a permanent S3 Standard copy, you must copy it explicitly yourself

AWS timing for Deep Archive:

- `Standard`: typically **within 12 hours**
- `Bulk`: typically **within 48 hours**

`Bulk` is the **lowest-cost** option for Deep Archive.

Sources:

- https://docs.aws.amazon.com/AmazonS3/latest/userguide/restoring-objects.html
- https://docs.aws.amazon.com/cli/latest/reference/s3api/restore-object.html
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/restoring-objects-retrieval-options.html

## Lowest-cost restore workflow

### 1. Find the S3 object key

DeepKeep stores packs under keys like:

```text
packs/YYYY/MM/pack-<pack_id>.tar.age
```

You can find the pack ID and object key with:

```bash
python deepkeep.py --config deepkeep.yaml catalog packs
```

or for a specific file:

```bash
python deepkeep.py --config deepkeep.yaml catalog file PATH/TO/FILE
```

### 2. Configure DeepKeep for lowest-cost restore

Use this in your S3 backend config:

```yaml
backend:
  type: s3
  bucket: my-bucket
  prefix: deepkeep
  storage_class: DEEP_ARCHIVE
  glacier_restore_tier: Bulk
  glacier_restore_days: 5
```

Notes:

- `glacier_restore_tier: Bulk` is the lowest-cost Deep Archive option
- `glacier_restore_days` controls how long the temporary restored copy stays available in S3
- `5` is the current DeepKeep default

### 3. Start the DeepKeep restore

Run the normal restore command:

```bash
python deepkeep.py --config deepkeep.yaml restore --dest restored SOME/PREFIX
```

If the required pack is still archived, DeepKeep will request the restore and stop.

Typical output:

```text
restored: 0
packs pending glacier restore: 1
```

### 4. Check whether AWS is done

Run:

```bash
aws s3api head-object \
  --bucket YOUR_BUCKET \
  --key YOUR_PREFIX/packs/YYYY/MM/pack-XXXXXXXXXXXX.tar.age
```

Look at the `Restore` field:

- `ongoing-request="true"`: still restoring
- `ongoing-request="false"`: ready

If the object is ready, you may also see an expiry date for the temporary restored copy.

### 5. Run the same DeepKeep restore command again

Once AWS says the pack is ready, run the **same restore command again**:

```bash
python deepkeep.py --config deepkeep.yaml restore --dest restored SOME/PREFIX
```

If multiple packs are involved, AWS must finish restoring all of the required packs before DeepKeep can download them.

## Current defaults and overrides

If you do not set anything in config, DeepKeep currently uses:

- `glacier_restore_tier: Bulk`
- `glacier_restore_days: 5`

You can change them in config, for example:

```yaml
backend:
  type: s3
  bucket: my-bucket
  prefix: deepkeep
  storage_class: DEEP_ARCHIVE
  glacier_restore_tier: Standard
  glacier_restore_days: 2
```

DeepKeep passes those values to S3 `restore-object`.

## Should I delete the restored copy?

Usually **no**.

For Glacier Deep Archive restores, AWS removes the temporary restored copy automatically after the number of days you requested. You normally:

1. request restore
2. wait
3. download with DeepKeep
4. let the temporary copy expire

Only make a permanent copy if you intentionally want the object to remain in a non-archival class.
