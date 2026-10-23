"""TestRail V9: clean comments of ALREADY RESTORED results, in place.

Preview by default; --apply edits only mapped result comments after Y/N.
Requires testrail_common.py and the existing backup + mapping under C:\\TR\\Backup.
No new results, runs, cases, or attachments are created.
"""
import argparse
import html
import re
import shutil
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests
from testrail_common import read_json, require_env


class HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0
        self.list_stack = []

    def newline(self):
        if self.parts and not self.parts[-1].endswith('\n'):
            self.parts.append('\n')

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ('script', 'style'):
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ('p', 'div', 'br', 'section', 'tr', 'h1', 'h2', 'h3', 'h4'):
            self.newline()
        elif tag in ('ul', 'ol'):
            self.newline()
            self.list_stack.append(tag)
        elif tag == 'li':
            self.newline()
            self.parts.append('- ')
        elif tag == 'img':
            desc = dict(attrs).get('alt')
            if desc:
                self.parts.append(f'[Image: {desc}]')

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ('p', 'div', 'li', 'ul', 'ol', 'tr', 'h1', 'h2', 'h3', 'h4'):
            self.newline()
        if tag in ('ul', 'ol') and self.list_stack:
            self.list_stack.pop()

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def clean_comment(raw):
    raw = str(raw or '')
    if not re.search(r'</?[a-z][^>]*>', raw, re.I):
        return html.unescape(raw).strip()
    parser = HTMLToText()
    parser.feed(raw)
    parser.close()
    text = ''.join(parser.parts)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def original_prefix(result):
    return (f'[Restored from result {result["id"]}; original author ID '
            f'{result.get("created_by")}; original timestamp {result.get("created_on")}]')


def readable_prefix(result):
    stamp = result.get('created_on')
    if stamp is None:
        date = 'unknown'
    else:
        try:
            date = datetime.fromtimestamp(int(stamp), tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        except (ValueError, OverflowError, TypeError, OSError):
            date = str(stamp)
    return (f'[Restored from result {result["id"]}; original author ID '
            f'{result.get("created_by")}; original date {date}]')


def choose(options):
    while True:
        answer = input('\nSelect backup (number, blank = cancel): ').strip()
        if not answer:
            raise SystemExit('Cancelled; no changes made.')
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        print('Choose a listed number.')


def get_all_results(api, run_id):
    # The common API client implements paginated collection reads.
    return api.collection(f'get_results_for_run/{run_id}', 'results')


def progress(done, total):
    width = 30
    filled = round(width * done / max(total, 1))
    print(f'\rUpdating comments   [{"#" * filled}{"-" * (width-filled)}] '
          f'{done/max(total,1):.0%} ({done}/{total})',
          end='\n' if done >= total else '', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup-root', default=r'C:\TR\Backup')
    parser.add_argument('--apply', action='store_true', help='Edit comments in TestRail (preview by default)')
    args = parser.parse_args()
    root = Path(args.backup_root).expanduser()
    if not root.is_dir():
        raise SystemExit(f'Backup directory not found: {root}')
    backups = []
    for folder in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if (folder / 'manifest.json').is_file() and (folder / 'results.json').is_file():
            try:
                manifest = read_json(folder / 'manifest.json')
                if manifest.get('format_version') == 2:
                    backups.append((folder, manifest))
            except (ValueError, OSError):
                pass
    if not backups:
        raise SystemExit('No compatible backups found.')
    print('\nAVAILABLE BACKUPS:')
    for i, (folder, m) in enumerate(backups, 1):
        run_ids = m.get('selected_run_ids') or []
        names = m.get('selected_run_names') or {}
        label = ', '.join(f'R{rid}: {names.get(str(rid), "")}' for rid in run_ids)
        print(f'{i}. P{m.get("project_id")} {m.get("project_name", "")}')
        print(f'   {label}')
    folder, manifest = choose(backups)
    api = require_env('')
    if api.base.rstrip('/') != str(manifest.get('source_url', '')).rstrip('/'):
        raise SystemExit('REFUSED: backup TestRail account does not match configured account.')
    mapping_paths = [p for p in folder.glob('restore_mapping_project_*.json')
                     if p.stem.removeprefix('restore_mapping_project_').isdigit()]
    if len(mapping_paths) != 1:
        raise SystemExit(f'REFUSED: expected one destination mapping, found {len(mapping_paths)}. '
                         'No edits made. Select a backup with one mapping.')
    mapping_path = mapping_paths[0]
    mapping = read_json(mapping_path)
    project_id = int(mapping_path.stem.rsplit('_', 1)[-1])
    run_map = mapping.get('runs') or {}
    if len(run_map) != 1:
        raise SystemExit('REFUSED: expected exactly one mapped run. No edits made.')
    old_run, new_run = next(iter(run_map.items()))
    new_run = int(new_run)
    live_run = api.get(f'get_run/{new_run}')
    if int(live_run.get('project_id', -1)) != project_id:
        raise SystemExit('REFUSED: destination run is not in mapped project.')
    if live_run.get('is_completed'):
        raise SystemExit('REFUSED: destination run is closed.')
    results_by_run = read_json(folder / 'results.json')
    source_results = {str(r['id']): r for r in results_by_run.get(str(old_run), [])}
    mapped = mapping.get('results') or {}
    if not mapped:
        raise SystemExit('No restored results are mapped; nothing to fix.')
    remote = {str(r['id']): r for r in get_all_results(api, new_run)}
    missing = [rid for rid in mapped.values() if str(rid) not in remote]
    if missing:
        raise SystemExit(f'REFUSED: {len(missing)} mapped results not found in R{new_run}.')
    updates = []
    already_clean = 0
    conflicts = []
    for source_id, destination_id in mapped.items():
        source = source_results.get(str(source_id))
        target = remote[str(destination_id)]
        if not source:
            conflicts.append(f'Source result {source_id} absent from backup')
            continue
        expected = original_prefix(source) + '\n' + str(source.get('comment') or '')
        corrected = readable_prefix(source) + '\n' + clean_comment(source.get('comment'))
        current = str(target.get('comment') or '')
        if current == corrected:
            already_clean += 1
        elif current == expected:
            updates.append((str(destination_id), corrected))
        else:
            conflicts.append(f'Result {destination_id} was modified or has unexpected comment; not overwriting')
    print('\n' + '=' * 62)
    print('RESULT COMMENT FIX V9 - ' + ('APPLY' if args.apply else 'PREVIEW'))
    print('=' * 62)
    print(f'Destination: P{project_id} | R{new_run} {live_run.get("name", "")}')
    print(f'Restored results mapped: {len(mapped)}')
    print(f'Comments to clean: {len(updates)} | Already clean: {already_clean}')
    print(f'Conflicts / unexpected changes: {len(conflicts)}')
    print('Only comment fields are edited. No new results or status changes.')
    if conflicts:
        for conflict in conflicts[:10]:
            print('  WARNING:', conflict)
        raise SystemExit('REFUSED: review unexpected comments before making changes.')
    if not updates:
        print('Nothing to change.')
        return
    print('\nExample corrected comment:\n' + updates[0][1][:600])
    if not args.apply:
        print('\nPREVIEW COMPLETE. Run with --apply to update existing comments.')
        return
    if input(f'\nProceed with fixing {len(updates)} comments? [Y/N]: ').strip().lower() != 'y':
        raise SystemExit('Cancelled; no changes made.')
    # Save the full live result records for a local rollback reference.
    safety = folder / ('result_comments_before_v9_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.json')
    import json
    safety.write_text(json.dumps({rid: remote[rid].get('comment') for rid, _ in updates}, indent=2,
                                 ensure_ascii=False), encoding='utf-8')
    print('Original comments saved to:', safety)
    progress(0, len(updates))
    for number, (result_id, corrected) in enumerate(updates, 1):
        # Edit an existing result. This endpoint changes only supplied fields.
        try:
            response = api.s.post(api.api + f'edit_result/{result_id}',
                                  json={'comment': corrected}, timeout=(20, 180))
            if not response.ok:
                raise RuntimeError(f'HTTP {response.status_code}: {response.text[:1000]}')
            returned = response.json()
            if str(returned.get('comment', '')) != corrected:
                raise RuntimeError('Response comment does not match requested update; verify in TestRail')
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f'\nSTOPPED at result {result_id}: {exc}')
            print('Changes already made remain in TestRail; rerun safely after reviewing the error.')
            raise SystemExit(1)
        progress(number, len(updates))
    print(f'\nCOMPLETE: updated {len(updates)} existing comments in P{project_id} R{new_run}.')
    print('No statuses, cases, runs, or attachments were modified.')


if __name__ == '__main__':
    main()
