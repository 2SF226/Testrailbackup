"""TestRail V8: restore execution results ONLY into an existing restored run.

Requires the original backup directory and its restore_mapping_project_<ID>.json.
PREVIEW by default. Use --apply to write after confirmation.
Does not create suites, sections, cases, runs, or upload attachments.
"""
import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
import requests
from testrail_common import read_json, write_json, require_env


def progress(done, total):
    width = 30
    n = round(width * done / max(total, 1))
    print(f'\rRestoring results    [{"#" * n}{"-" * (width-n)}] {done/max(total,1):.0%} ({done}/{total})',
          end='\n' if done >= total else '', flush=True)


def choose(options, prompt):
    while True:
        raw = input(f'\n{prompt} (number, blank = cancel): ').strip()
        if not raw:
            raise SystemExit('Cancelled; no changes made.')
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw)-1]
        print('Choose a listed number.')


def get_remote_results(api, run_id):
    # Fail closed if the API cannot confirm whether results already exist.
    return api.collection(f'get_results_for_run/{run_id}', 'results')


def post_result_once(api, test_id, payload):
    # A non-idempotent POST must not be retried automatically after a timeout.
    url = api.api + f'add_result/{test_id}'
    response = api.s.post(url, json=payload, timeout=(20, 180))
    if not response.ok:
        body = response.text[:2500].replace('\r', ' ').replace('\n', ' ')
        raise RuntimeError(f'HTTP {response.status_code} from add_result/{test_id}: {body}')
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup-root', default=r'C:\TR\Backup')
    parser.add_argument('--apply', action='store_true', help='Actually upload results (default: preview)')
    args = parser.parse_args()

    backup_root = Path(args.backup_root).expanduser()
    if not backup_root.is_dir():
        raise SystemExit(f'Backup directory not found: {backup_root}')
    choices = []
    for folder in sorted(backup_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if folder.is_dir() and (folder/'manifest.json').is_file() and (folder/'results.json').is_file():
            try:
                manifest = read_json(folder/'manifest.json')
                if manifest.get('format_version') == 2:
                    choices.append((folder, manifest))
            except Exception:
                continue
    if not choices:
        raise SystemExit('No compatible backups found.')
    print('\nAVAILABLE BACKUPS:')
    for i, (folder, manifest) in enumerate(choices, 1):
        selected = manifest.get('selected_run_ids') or []
        run_names = manifest.get('selected_run_names') or {}
        run_label = ', '.join(f'R{rid}: {run_names.get(str(rid), "")}' for rid in selected)
        print(f'{i}. P{manifest.get("project_id")} {manifest.get("project_name", "")}')
        if run_label:
            print(f'   {run_label}')
        print(f'   Backup: {folder.name[-52:]}')
    root, manifest = choose(choices, 'Select backup')

    api = require_env('')
    if api.base.rstrip('/') != str(manifest.get('source_url', '')).rstrip('/'):
        raise SystemExit('REFUSED: backup account URL differs from configured TestRail account.')

    # Only canonical mapping names qualify; ignore timestamped safety copies.
    mappings = sorted(root.glob('restore_mapping_project_*.json'))
    mappings = [p for p in mappings if p.stem.removeprefix('restore_mapping_project_').isdigit()]
    if not mappings:
        raise SystemExit('No restore mapping found for this backup. Cannot safely match old test IDs to restored tests.')
    # No mapping menu. If more than one destination has a mapping, ask for the
    # destination project, not a JSON filename.
    if len(mappings) == 1:
        mapping_path = mappings[0]
    else:
        options = []
        for path in mappings:
            candidate = read_json(path)
            if candidate.get('runs') and candidate.get('cases'):
                options.append((path, candidate))
        if not options:
            raise SystemExit('No usable restore mappings found.')
        print('\nAVAILABLE RESTORED PROJECTS:')
        for i, (path, candidate) in enumerate(options, 1):
            pid = path.stem.removeprefix('restore_mapping_project_')
            run_ids = ', '.join(f'R{r}' for r in candidate.get('runs', {}).values())
            print(f'{i}. Project P{pid} | Restored run(s): {run_ids}')
        mapping_path, _ = choose(options, 'Select destination project')
    mapping = read_json(mapping_path)
    project_id = int(mapping_path.stem.rsplit('_', 1)[-1])

    projects = api.collection('get_projects', 'projects')
    project = next((p for p in projects if int(p['id']) == project_id), None)
    if not project:
        raise SystemExit(f'REFUSED: destination P{project_id} not found.')
    runs = read_json(root/'runs.json')
    tests = read_json(root/'tests.json')
    results = read_json(root/'results.json')
    if isinstance(runs, dict):
        runs = list(runs.values())
    if not isinstance(tests, dict) or not isinstance(results, dict):
        raise SystemExit('Unsupported backup tests/results structure.')
    run_options = []
    for run in runs:
        old_run = str(run['id'])
        if old_run in mapping.get('runs', {}):
            run_options.append((old_run, int(mapping['runs'][old_run]), run.get('name', '')))
    if not run_options:
        raise SystemExit('REFUSED: no mapped runs found.')
    if len(run_options) == 1:
        old_run, new_run, name = run_options[0]
        print(f'Using mapped run: R{old_run} -> R{new_run} | {name}')
    else:
        print('\nMAPPED RUNS:')
        for i, (old, new, name) in enumerate(run_options, 1):
            print(f'{i}. Source R{old} -> Destination R{new} | {name}')
        old_run, new_run, name = choose(run_options, 'Select run to restore results into')

    live_run = api.get(f'get_run/{new_run}')
    if int(live_run.get('project_id', -1)) != project_id:
        raise SystemExit('REFUSED: mapped run is not in the mapped destination project.')
    if live_run.get('is_completed'):
        raise SystemExit('REFUSED: destination run is closed/completed.')
    live_tests = api.collection(f'get_tests/{new_run}', 'tests')
    by_case = {str(t['case_id']): int(t['id']) for t in live_tests}
    old_test_to_case = {str(t['id']): str(t['case_id']) for t in tests.get(old_run, [])}
    cases_map = mapping.get('cases', {})
    planned = []
    problems = []
    skipped_untested = []
    statuses = api.get('get_statuses')
    status_by_id = {int(s['id']): s for s in statuses if s.get('id') is not None}
    # TestRail exposes Untested in get_statuses but does not allow posting it via add_result.
    untested_ids = {sid for sid, status in status_by_id.items()
                    if str(status.get('name') or status.get('label') or '').strip().casefold() == 'untested'}
    for result in sorted(results.get(old_run, []), key=lambda r: (r.get('created_on') or 0, r.get('id') or 0)):
        rid = str(result['id'])
        old_case = old_test_to_case.get(str(result.get('test_id')))
        new_case = cases_map.get(old_case)
        new_test = by_case.get(str(new_case))
        if not old_case or not new_case or not new_test:
            problems.append(f'Result {rid}: original test/case has no matching destination test')
            continue
        if not result.get('status_id'):
            problems.append(f'Result {rid}: missing status_id')
            continue
        status_id = int(result['status_id'])
        if status_id in untested_ids:
            skipped_untested.append(rid)
            continue
        planned.append((rid, result, new_test))
    if problems:
        print('\nMAPPING ERRORS:')
        for issue in problems[:20]:
            print(' -', issue)
        raise SystemExit(f'REFUSED: {len(problems)} result(s) cannot be safely mapped.')

    allowed = set(status_by_id)
    invalid = [(rid, res['status_id']) for rid, res, _ in planned if int(res['status_id']) not in allowed]
    if invalid:
        raise SystemExit(f'REFUSED: source status IDs not available in destination account: {invalid[:10]}')

    remote_results = get_remote_results(api, new_run)
    mapped = mapping.setdefault('results', {})
    remote_ids = {str(r['id']) for r in remote_results}
    stale = [rid for rid, _, _ in planned if rid in mapped and str(mapped[rid]) not in remote_ids]
    if stale:
        raise SystemExit(f'REFUSED: {len(stale)} mapped results are missing from destination. Review mapping before proceeding.')
    unmapped_remote = remote_ids - {str(x) for x in mapped.values()}
    if unmapped_remote:
        raise SystemExit(f'REFUSED: destination run already has {len(unmapped_remote)} result(s) not tracked by this mapping. No writes made.')

    pending = [(rid, r, tid) for rid, r, tid in planned if rid not in mapped]
    print('\n' + '=' * 64)
    print('RESULTS-ONLY RESTORE V8 - ' + ('APPLY' if args.apply else 'PREVIEW'))
    print('=' * 64)
    print(f'Source: P{manifest.get("project_id")} R{old_run}')
    print(f'Destination: P{project_id} {project.get("name")} | R{new_run} {live_run.get("name")}')
    print(f'Source results: {len(planned) + len(skipped_untested)} | Untested skipped: {len(skipped_untested)}')
    print(f'Eligible results: {len(planned)} | Already mapped: {len(planned)-len(pending)} | To upload: {len(pending)}')
    print(f'Destination test count: {len(live_tests)}')
    print('No cases, runs, or attachments will be created or changed.')
    print('Historical author/timestamp are included in the comment; native values cannot be recreated.')
    print('Payload uses status_id + comment only to avoid unsupported optional fields.')
    if not pending:
        print('\nNothing to upload. All backed-up results are already mapped.')
        return
    if not args.apply:
        print('\nPREVIEW COMPLETE. No changes made. Run with --apply to upload.')
        return
    if input(f'\nProceed with uploading {len(pending)} results to P{project_id} R{new_run}? [Y/N]: ').strip().lower() != 'y':
        raise SystemExit('Cancelled. No changes made.')

    # Preserve original mapping before any writes. Subsequent uploads update it in place.
    snapshot = mapping_path.with_name(mapping_path.stem + '_before_results_v8_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.json')
    shutil.copy2(mapping_path, snapshot)
    print('Mapping safety copy:', snapshot)
    total = len(pending)
    progress(0, total)
    for number, (rid, source, test_id) in enumerate(pending, 1):
        prefix = f'[Restored from result {rid}; original author ID {source.get("created_by")}; original timestamp {source.get("created_on")}]'
        comment = prefix + '\n' + str(source.get('comment') or '')
        payload = {'status_id': int(source['status_id']), 'comment': comment}
        try:
            created = post_result_once(api, test_id, payload)
            new_id = created.get('id')
            if new_id is None:
                raise RuntimeError('TestRail accepted result but returned no ID; verify destination before retrying')
            mapped[rid] = new_id
            write_json(mapping_path, mapping)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            print(f'\nSTOPPED on source result {rid}, destination test T{test_id}: {exc}')
            print('Previously uploaded results are saved in the mapping.')
            print('IMPORTANT: if the request timed out, check the destination run before rerunning.')
            raise SystemExit(1)
        progress(number, total)
    print(f'\nCOMPLETE: uploaded {total} results to P{project_id} R{new_run}.')
    print('Updated mapping:', mapping_path)
    print(f'Untested records skipped: {len(skipped_untested)}')
    print('Result attachments are NOT uploaded by this version.')


if __name__ == '__main__':
    main()
