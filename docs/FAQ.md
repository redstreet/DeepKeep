<details><summary>How do I see a list of deduplicated files?</summary>

```
  gzip -dc /path/to/catalog.sqlite.gz > /tmp/deepkeep-catalog.sqlite
  sqlite3 /tmp/deepkeep-catalog.sqlite '
  SELECT fp.sha256, COUNT(*) AS n, GROUP_CONCAT(fp.path, char(10))
  FROM file_paths fp
  GROUP BY fp.sha256
  HAVING COUNT(*) > 1
  ORDER BY n DESC;
  '
```
</details>

<details><summary>If I create a backup of a thousand files and then if I change the bucket name and then try to backup those thousand files again, maybe with some
  ten new files, will deduplication occur properly?</summary>

No.

With the current code, if you change the bucket name in the same config/catalog and run backup again, DeepKeep should fail before backing up.

Why:

- the catalog is bound to the exact backend config on the first real backup
- bucket name is part of that backend identity
- changing the bucket means “different backend”
- DeepKeep rejects that to avoid splitting one catalog across multiple backends

So in your example:

- first backup: 1000 files to bucket A
- change config to bucket B
- second backup: DeepKeep should error out before doing dedupe or upload

If you instead create a new catalog for bucket B:

- dedupe will only work against what that new catalog knows
- so the original 1000 files from bucket A will not dedupe via that new catalog
- only duplicates within the current run itself would dedupe

So the short answer is:

- same catalog + changed bucket: backup should be blocked
- new catalog + new bucket: cross-bucket dedupe will not happen
</details>
