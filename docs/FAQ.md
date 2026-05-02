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

<details><summary>When restoring, are files from AWS pack files are they copied and staged locally first? My concern is what would happen if close to the end of the restore the restore process got killed. Would it download everything again?</summary>

Yes.

Current restore flow is:

1. create a temporary working directory
2. for each needed pack:
    - download the encrypted .age pack from AWS into that temp dir
    - decrypt it locally into a temporary .tar
    - extract/restore files from that tar
3. when the restore command exits, that temp working directory is discarded

So AWS pack files are staged locally first, but only in a temp area.

What happens if the restore is killed near the end:

- files already restored to your destination stay there
- temporary downloaded/decrypted pack files are not preserved as a resumable cache
- rerunning the same restore command will download the needed packs again

Important nuance:

- if you rerun without --force, DeepKeep will skip destination files that already exist
- but it still currently downloads/decrypts each matching pack before discovering many of those files can be skipped

So the honest answer is:

- no, restore is not resumable at the pack-download level
- yes, a rerun can redownload packs from AWS
- yes, already restored destination files are generally kept and skipped
- but DeepKeep may still redownload packs containing those files on the rerun

If your concern is bandwidth/time after interruption, that concern is valid. The current restore path is idempotent for destination files, but not
cache-resumable for downloaded packs.

</details>
