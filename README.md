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

Open PowerShell in the repository folder:

```powershell
cd "C:\Users\Niel\Documents\Github\Testrailbackup"
py .\testrail_interactive.py
```

The first menu is:

```text
TESTRAIL:
  1. Backup
  2. Restore preview
  3. Restore apply
Select number (blank cancels):
```

### 1 — Backup

Enter `1` and select the **source project by its displayed number** (the list numbers are menu positions, not TestRail project IDs). The next menu is:

```text
BACKUP SCOPE:
  1. Test plan
  2. Single test run
  3. Multiple test runs
  4. All runs
Select number (blank cancels):
```

| Option | What it does | How to select | Best used for |
| --- | --- | --- | --- |
| **1. Test plan** | Lists test plans in the chosen project, then backs up all distinct runs belonging to the selected plan. | Choose the plan's **displayed number**. The program shows the included run IDs before confirmation. | Backing up a complete TestRail plan containing one or more runs. |
| **2. Single test run** | Backs up one chosen run, including its selected cases, available execution results and indexed evidence. | Choose the run's **displayed number**, e.g. `1` may correspond to R35. | Testing a restore or archiving one completed run. |
| **3. Multiple test runs** | Backs up several selected runs from the same source project into one backup folder. | Enter **menu numbers separated by commas**, e.g. `1,3,5`; these are not run IDs. | Backing up several related runs without selecting all runs. |
| **4. All runs** | Backs up every run returned by the TestRail API for the selected project. | Select `4`; no individual run selection is required. | Backing up all available runs in one project. |

**Important:** All four choices are *run-based*. `All runs` is not a guaranteed full-project snapshot of every case that is not in a run, and a plan backup is a collection of the plan's runs rather than a full-fidelity reconstruction of the TestRail plan container itself.

After the selection, inspect **SELECTED RUNS** and confirm `Create backup? [Y/N]` with `Y`. The default destination is `C:\TR\Backup`, in a new timestamped directory. Keep the folder created by the script for verification and restoration.

**Example — back up R35:**

```text
TESTRAIL: 1. Backup                  -> enter 1
SOURCE PROJECTS: P7 ...          -> enter its displayed menu number
BACKUP SCOPE: 2. Single test run -> enter 2
TEST RUNS: R35 ...               -> enter its displayed menu number
Create backup? [Y/N]:           -> enter Y
```

### 2 — Restore preview (recommended before every restore)

Enter `2` from the first menu. Select the backup folder by number, then the **destination project** by number. Choose whether to include execution results (`Y` or `N`). The preview checks backup integrity, shows the source test IDs/case IDs/titles and evidence inventory, and **does not modify TestRail**.

Verify that the intended source run and test titles are shown before choosing Apply.

### 3 — Restore apply

Enter `3` from the first menu, select the same verified backup and the correct destination project, and answer the execution-results question. Confirm with `Y` only if you intend to create new TestRail objects.

The restore creates new suites/sections as needed, cases, run(s), results (if selected) and indexed evidence. It is **not** an in-place update, merge, or synchronization. The V7 restore validates destination run membership (case IDs and titles) and execution statuses before uploading evidence.

**Do not run Apply twice on the same backup.** Review the mapping file and destination before any repeat attempt. Once completed, visually confirm that CSV, MP4, and screenshots appear in the corresponding result sections.

### Menu tips

- `1`, `2`, `3`, etc. are **positions in the current menu**, not necessarily TestRail IDs. Read the label next to each number.
- For **Multiple test runs**, enter comma-separated menu positions (`1,3,5`).
- Press **Enter on a blank selection** to cancel; confirmation prompts require `Y` or `N`.
- Backup choices are based on the runs available to your authenticated TestRail account.

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
