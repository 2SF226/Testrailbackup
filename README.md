# TestRail Backup & Restore

Interactive Python tools for backing up TestRail test runs and restoring their cases, execution results, and evidence into another project.

## Verified workflow

The backup and restore workflow was tested on **Project 7 / Run 35 (Queue Management)** and restored to **Sandbox Project 8 / Run 40** on October 22, 2026.

- 19 test cases and 19 execution statuses verified against the source.
- 41 unique evidence files uploaded (the 71-row source index contained 30 repeated references).
- The source evidence comprised CSV, MP4, JPG, and PNG attachments.
- No restore errors were reported; the restored result evidence was visually checked in TestRail.

This verifies the tested run, **not** every TestRail project configuration or the ability to reproduce historical authors and timestamps.

## Requirements

- Windows with Python 3.12 (tested), PowerShell, and access to the TestRail API.
- Install dependencies from the repository directory:

  ```powershell
  py -m pip install -r requirements.txt
  ```

- Create a local `.env` file (never commit it):

  ```dotenv
  TESTRAIL_BASE_URL=https://your-site.testrail.io
  TESTRAIL_EMAIL=your-email@example.com
  TESTRAIL_PASSWORD=your-api-key-or-credential
  ```

## Files

| File | Purpose |
| --- | --- |
| `testrail_interactive.py` | Numbered menu for backups, restore previews, and restores |
| `testrail_backup_final.py` | Download test metadata and evidence; verify local backups |
| `testrail_restore_final.py` | Restore into a different project; preview is the default (verified V7) |
| `testrail_common.py` | Shared TestRail API, JSON, and checksum helpers |
| `requirements.txt` | Python dependencies |

Older development versions should be kept in `archive/` locally, not used for production restores.

## Interactive usage

From the repository directory:

```powershell
py .\testrail_interactive.py
```

Select one of these menu actions:

1. **Backup** — Choose a source project, then a test plan, single run, multiple runs, or all runs. Confirm with `Y`.
2. **Restore preview** — Choose a backup and destination project; review the source test IDs, case IDs, titles, and evidence summary. Makes no TestRail changes.
3. **Restore apply** — Choose the verified backup and destination, decide whether to restore results, and confirm with `Y`. This **creates new destination objects**; it is not an overwrite or synchronization operation.

The default backup directory is `C:\TR\Backup`.

## Verify a backup

Use the exact directory printed by the backup script:

```powershell
py .\testrail_backup_final.py --verify "C:\TR\Backup\YOUR_BACKUP_FOLDER"
```

The verifier checks indexed evidence files against their recorded SHA256 checksums. A clean checksum report does not establish that the TestRail API exposed every possible result-level attachment.

## Direct commands (optional)

```powershell
py .\testrail_backup_final.py --project 7 --runs 35
py .\testrail_restore_final.py
py .\testrail_restore_final.py --apply
```

Prefer the interactive launcher for normal use.

## Safety and limitations

- **Always preview first.** Validate the selected project, run, and case titles.
- A restore creates a new suite/cases/run/results and attachments in a **different** destination project. It does not merge with an existing run.
- The script validates restored run membership and statuses before proceeding with evidence uploads.
- If a restore is interrupted, inspect the generated `restore_mapping_project_<id>.json` before attempting another restore; repeating Apply can create duplicates.
- Some TestRail API attachment ownership information is incomplete. Verify that CSV, MP4, and screenshots appear in the intended result sections after restoration.
- Historical authors/timestamps cannot be recreated exactly. Some rich-text images may require checking or relinking.
- Do not add credentials, raw backup data, exported evidence, or `.env` to Git.

## GitHub publishing

```powershell
git status
git check-ignore .env
git add README.md testrail_interactive.py testrail_backup_final.py testrail_restore_final.py testrail_common.py requirements.txt .gitignore
git commit -m "Document verified TestRail backup and restore workflow"
git push -u origin main
```

Review `git status` before committing and confirm secrets are excluded. If `.env` is already tracked, remove it from the Git index before committing (`git rm --cached .env`) and rotate any exposed credentials.
