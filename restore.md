# Restoring From S3 Glacier Deep Archive

## Short version

If your DeepKeep packs are in **S3 Glacier Deep Archive**, the cheapest retrieval option is **Bulk**.

DeepKeep **does not currently let you choose the Glacier retrieval tier or restore duration**. Today, when it requests a restore itself, it uses:

- `Tier=Standard`
- `Days=7`

So if you want the **lowest-cost** Deep Archive restore, use the **AWS CLI** first to request a **Bulk** restore, wait until AWS says the object is ready, and then run `deepkeep.py restore ...`.

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

### 2. Request a Bulk restore

Run:

```bash
aws s3api restore-object \
  --bucket YOUR_BUCKET \
  --key YOUR_PREFIX/packs/YYYY/MM/pack-XXXXXXXXXXXX.tar.age \
  --restore-request '{"Days":7,"GlacierJobParameters":{"Tier":"Bulk"}}'
```

Notes:

- `Days` is how long the temporary restored copy stays available in S3
- `Tier=Bulk` is the lowest-cost choice
- Use a small number of days unless you know you need longer

### 3. Check whether AWS is done

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

### 4. Run the DeepKeep restore

Once AWS says the pack is ready, run the normal DeepKeep restore command:

```bash
python deepkeep.py --config deepkeep.yaml restore --dest restored SOME/PREFIX
```

If multiple packs are involved, repeat the AWS restore request for each required pack before running DeepKeep again.

## If you let DeepKeep request the restore

If you run:

```bash
python deepkeep.py --config deepkeep.yaml restore --dest restored SOME/PREFIX
```

and the pack is still archived, DeepKeep will request a restore and stop. Today it requests:

- `Days=7`
- `Tier=Standard`

Typical output:

```text
restored: 0
packs pending glacier restore: 1
```

Then you wait and run the **same restore command again later**.

## Should I delete the restored copy?

Usually **no**.

For Glacier Deep Archive restores, AWS removes the temporary restored copy automatically after the number of days you requested. You normally:

1. request restore
2. wait
3. download with DeepKeep
4. let the temporary copy expire

Only make a permanent copy if you intentionally want the object to remain in a non-archival class.
