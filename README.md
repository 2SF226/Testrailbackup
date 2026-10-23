# TestRail selected-run backup to local C:\TR\Backup (v1 pilot)

## Scope

Back up **individually selected Test Runs**, including associated test cases, case fields (including steps and expected results), suites, sections, run tests, all accessible result records, test/case/result attachments, and same-host `<img src>` evidence referenced by case rich text. Download failures are recorded in `manifest.json`; `complete: false` prevents restore.

**Important limitations**: This is a pilot, not a certified full-fidelity TestRail disaster-recovery solution. The restore recreates cases, sections, runs, results and most ordinary attachments, but **does not automatically relink embedded rich-text images**, reproduce original TestRail IDs, original authors/timestamps, test plans, statuses/configurations/custom-field definitions, or arbitrary third-party hosted media. TestRail project configurations and case field templates must be prepared in the destination account. Source case attachments and result attachments are attempted; test-level attachments are copied to the corresponding case where possible. Result status IDs are assumed compatible. Never use the restore on a populated destination project.

## Installation on Windows

1. The backup defaults to `C:\TR\Backup`. The script creates the folder automatically if it does not exist. Google Drive is not required.
2. Put these files in your `TestRail-Puller` project folder.
3. Run `py -m pip install -r requirements.txt`.
4. Copy `.env.example` to `.env`, fill in source credentials, and keep `.env` out of Git (already commonly ignored).
5. Verify TestRail API access is enabled and account permissions allow reading attachments.

## Back up selected runs

Use **your actual TestRail project ID**, not the sample below.

```powershell
py testrail_backup_v1.py --project 7 --interactive
```

The script lists available open runs. `*` highlights names containing **Queue Management** or **Retail Entrance Traffic Count**. Enter the exact run IDs you want, separated by commas. It does **not** automatically select every run in either category.

To select IDs directly:

```powershell
py testrail_backup_v1.py --project 7 --runs 123,124
```

To show closed runs too, add `--all-runs --interactive` (in v1, `--all-runs` alone backs up every run, so only use that if intended).

Afterward, check `manifest.json` for `"complete": true`, verify evidence files. A `true` value means no API/download errors were recorded; it is not proof that every embedded media reference was detected.

## Preview restoration

Restore into a **new, empty project** in a **different TestRail account**. Destination custom fields, templates, statuses, and other settings should match the source.

```powershell
py testrail_restore_v1.py --backup "C:\TR\Backup\TestRail_P7_2026-10-21_090000" --dest-project 9
```

No destination credentials or API calls are needed for preview.

## Execute restoration

Fill in destination credentials in `.env`, then run:

```powershell
py testrail_restore_v1.py --backup "C:\TR\Backup\TestRail_P7_2026-10-21_090000" --dest-project 9 --apply --confirm RESTORE
```

**This creates records and cannot be automatically rolled back.** It refuses to start if it detects existing cases or runs. It writes `restore_mapping_project_9.json` with old-to-new IDs and warnings. If a restore fails partway, do **not** rerun on the same project: create a new empty project and inspect the mapping/error report.

## Local backup and GitHub

- Put backup data and MP4s in `C:\TR\Backup`, **not** the Git repository.
- Commit only the scripts, `requirements.txt`, and README to GitHub.
- Never commit `.env`, API tokens, private evidence, or restore mappings.
- Keep a second independent backup copy on an external drive; Google Drive can be added later.
- Large attachments may need substantial local disk space.

## Important security notes

Downloaded evidence may contain confidential information. Restrict Google Drive folder access and use encryption as required by your organization. The script only downloads embedded media hosted on the same TestRail domain and refuses off-domain image URLs.
