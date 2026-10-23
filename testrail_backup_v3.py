"""V3: Backup selected TestRail runs, their cases, results and evidence to a local folder.

Example:
 py testrail_backup_v3.py --project 7 --interactive
 py testrail_backup_v3.py --project 7 --runs 101,102
"""
import argparse, datetime, html, json, re, sys
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from testrail_common import API, IMAGE_RE, file_record, require_env, safe_filename, write_json

CATEGORIES = ('Queue Management', 'Retail Entrance Traffic Count')
# These two C2377 references were confirmed not to display as separate images in
# TestRail. They are retained in the original case HTML and logged for review.
KNOWN_UNRESOLVED = {
    2377: {
        'e0c86a83-bd9a-4655-8b26-754d56dbf96f',
        'b6b7f399-13e3-4b53-b74d-369dcd0c618e',
    }
}

def progress(label, done, total):
    """Refresh one progress line in-place; end each phase with a newline."""
    percent = 100 if total == 0 else int(done * 100 / total)
    blocks = 30 if total == 0 else round(done * 30 / total)
    line = f'{label:<19} [{"#" * blocks}{"-" * (30 - blocks)}] {percent:3d}% ({done}/{total})'
    # A single carriage return overwrites the same line. Never nest bars.
    sys.stdout.write('\r' + line + '   ')
    if done >= total:
        sys.stdout.write('\n')
    sys.stdout.flush()


def backup_folder_name(project, selected, project_id, stamp):
    """Human-readable project/run names plus IDs, safe for Windows paths."""
    project_name = safe_filename(str(project.get('name') or f'Project_{project_id}')).strip(' ._')
    project_name = project_name[:55].rstrip(' ._') or f'Project_{project_id}'
    if len(selected) == 1:
        run = selected[0]
        run_name = safe_filename(str(run.get('name') or f'Run_{run["id"]}')).strip(' ._')
        run_name = run_name[:65].rstrip(' ._') or f'Run_{run["id"]}'
        scope = f'Run{run["id"]}_{run_name}'
    else:
        scope = f'{len(selected)}Runs'
    return f'TestRail_P{project_id}_{project_name}_{scope}_{stamp}'

def try_collection(api, endpoint, key, errors, label):
    try: return api.collection(endpoint, key)
    except Exception as e:
        errors.append({'scope': label, 'error': str(e)})
        return []

def try_get(api, endpoint, errors, label):
    try: return api.get(endpoint)
    except Exception as e:
        errors.append({'scope': label, 'error': str(e)})
        return None

def save_attachment(api, attachment, owner_type, owner_id, root, files, errors):
    aid = attachment.get('id')
    if not aid: return
    filename = safe_filename(attachment.get('filename') or f'attachment_{aid}')
    path = root / 'evidence' / owner_type / str(owner_id) / f'{aid}_{filename}'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(api.download_attachment(aid))
        rec = file_record(path, root)
        rec.update({'attachment_id': aid, 'owner_type': owner_type, 'owner_id': owner_id, 'original_filename': attachment.get('filename')})
        files.append(rec)
    except Exception as e:
        errors.append({'scope': f'{owner_type}:{owner_id} attachment:{aid}', 'error': str(e)})

def scan_embedded(api, case, root, files, errors, seen, unresolved, case_attachments):
    # Traverse nested step arrays and arbitrary rich-text custom fields.
    def walk(value, path):
        if isinstance(value, dict):
            for k, v in value.items(): walk(v, path + [str(k)])
        elif isinstance(value, list):
            for i, v in enumerate(value): walk(v, path + [str(i)])
        elif isinstance(value, str) and ('src=' in value.lower()):
            for match in IMAGE_RE.finditer(value):
                raw = html.unescape(match.group('url'))
                if raw.startswith('data:'):
                    errors.append({'scope': f'case:{case["id"]} embedded', 'error': 'Data URI requires manual review'})
                    continue
                # Normalize TestRail's cache-busting fragments (#_t=...) and query variants.
                url = urljoin(api.base + '/', raw)
                parsed_url = urlparse(url)
                normalized = urlunparse(parsed_url._replace(fragment=''))
                id_match = re.search(r'attachments/get/([^&#/?]+)', raw, re.I)
                identity = ('attachment', id_match.group(1)) if id_match else ('url', normalized)
                if identity in seen: continue
                seen.add(identity)
                attachment_ref = id_match.group(1) if id_match else None
                if attachment_ref in KNOWN_UNRESOLVED.get(int(case['id']), set()):
                    unresolved.append({
                        'case_id': case['id'], 'field_path': path,
                        'source_url': raw, 'normalized_url': normalized,
                        'attachment_reference': attachment_ref,
                        'status': 'known_unresolved_not_visible_in_ui',
                        'reason': 'Not displayed as a separate screenshot during manual C2377 review; original HTML preserved',
                        'available_case_attachments': [
                            {'id': a.get('id'), 'filename': a.get('filename'), 'size': a.get('size')}
                            for a in case_attachments
                        ],
                    })
                    continue
                try:
                    # TestRail embedded links can contain UUID or numeric attachment IDs.
                    # Prefer the authenticated API; browser links may need session cookies.
                    m = id_match
                    if m:
                        try:
                            data = api.download_attachment(m.group(1))
                        except Exception as api_error:
                            try:
                                data = api.download_embedded(normalized)
                            except Exception as web_error:
                                raise RuntimeError(f'API: {api_error}; browser URL: {web_error}')
                    else:
                        data = api.download_embedded(normalized)
                    if not data or data[:100].lstrip().lower().startswith((b'<!doctype html', b'<html')):
                        raise ValueError('Empty response or HTML login/error page instead of image')
                    # Infer extension from actual image bytes when the URL has no extension.
                    signatures = ((b'\x89PNG\r\n\x1a\n', '.png'), (b'\xff\xd8\xff', '.jpg'),
                                  (b'GIF87a', '.gif'), (b'GIF89a', '.gif'), (b'BM', '.bmp'))
                    ext = next((extension for signature, extension in signatures if data.startswith(signature)), '')
                    if not ext and data[:4] == b'RIFF' and data[8:12] == b'WEBP': ext = '.webp'
                    if not ext: ext = Path(urlparse(normalized).path).suffix.lower()
                    if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'): ext = '.bin'
                    name = f'embedded_{len(files)+1:06d}{ext}'
                    dest = root / 'evidence' / 'embedded' / f'C{case["id"]}' / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    rec = file_record(dest, root)
                    rec.update({'owner_type': 'embedded', 'owner_id': case['id'], 'source_url': raw, 'field_path': path})
                    files.append(rec)
                except Exception as e:
                    error = {'scope': f'case:{case["id"]} embedded:{raw[:160]}', 'error': str(e)}
                    errors.append(error)
                    # Do not silently treat UUID references as numeric attachments.
                    # Preserve enough metadata to investigate or manually recover them.
                    unresolved.append({
                        'case_id': case['id'],
                        'status': 'download_failed',
                        'field_path': path,
                        'source_url': raw,
                        'normalized_url': normalized,
                        'attachment_reference': id_match.group(1) if id_match else None,
                        'error': str(e),
                        'available_case_attachments': [
                            {'id': a.get('id'), 'filename': a.get('filename'),
                             'size': a.get('size')} for a in case_attachments
                        ],
                    })
    walk(case, [])

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', type=int, required=True)
    p.add_argument('--output', default=r'C:\TR\Backup', help=r'Backup parent directory (default: C:\TR\Backup); created automatically')
    p.add_argument('--runs', help='Comma-separated TestRail run IDs')
    p.add_argument('--interactive', action='store_true')
    p.add_argument('--all-runs', action='store_true', help='Backup all runs, including closed')
    args = p.parse_args()
    if not (args.runs or args.interactive or args.all_runs): p.error('Specify --runs, --interactive, or --all-runs')
    api = require_env('')
    errors, files = [], []
    project = api.get(f'get_project/{args.project}')
    runs = api.collection(f'get_runs/{args.project}&is_completed=0', 'runs')
    if args.all_runs:
        runs = api.collection(f'get_runs/{args.project}', 'runs')
    if args.interactive:
        print('\nAvailable runs (sample categories highlighted):')
        for r in runs:
            mark = '*' if any(x.lower() in r.get('name', '').lower() for x in CATEGORIES) else ' '
            print(f'{mark} {r["id"]:>7} | {r.get("name", "")[:105]}')
        chosen = input('\nEnter run IDs separated by commas (blank = cancel): ').strip()
        if not chosen: raise SystemExit('Cancelled')
        selected_ids = {int(x.strip()) for x in chosen.split(',')}
    elif args.runs:
        selected_ids = {int(x.strip()) for x in args.runs.split(',')}
    else:
        selected_ids = {r['id'] for r in runs}
    selected = [r for r in runs if r['id'] in selected_ids]
    missing = selected_ids - {r['id'] for r in selected}
    if missing: raise SystemExit(f'Run IDs not found in fetched list: {sorted(missing)} (try --all-runs for closed runs)')
    if not selected: raise SystemExit('No runs selected')
    print('\nSELECTED RUNS:')
    for r in selected: print(' ', r['id'], r['name'])
    confirm = input("Create backup? [Y/N]: ").strip().lower()

    if confirm != "y":
        print("Cancelled")
        raise SystemExit(0)

    stamp = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')
    root = Path(args.output).expanduser().resolve() / backup_folder_name(project, selected, args.project, stamp)
    if root.exists(): raise SystemExit('Backup folder already exists')
    root.mkdir(parents=True)
    suites = try_collection(api, f'get_suites/{args.project}', 'suites', errors, 'suites')
    if not suites:
        # Single-suite projects may not expose get_suites; get sections without suite ID.
        sections = try_collection(api, f'get_sections/{args.project}', 'sections', errors, 'sections')
    else:
        sections = []
        for suite in suites:
            sections += try_collection(api, f'get_sections/{args.project}&suite_id={suite["id"]}', 'sections', errors, f'sections suite {suite["id"]}')
    cases, tests, results, result_index = {}, {}, {}, {}
    # get_attachments_for_result returned HTTP 404 on this server; do not call it.
    # Display one fixed-width, in-place progress bar per phase, without nested tqdm.
    print('Collecting runs and test metadata...', flush=True)
    all_tests = []
    for run in selected:
        rid = run['id']
        print(f'Run {rid}: {run["name"]}', flush=True)
        run_tests = try_collection(api, f'get_tests/{rid}', 'tests', errors, f'tests run {rid}')
        tests[str(rid)] = run_tests
        run_results = try_collection(api, f'get_results_for_run/{rid}', 'results', errors, f'results run {rid}')
        results[str(rid)] = run_results
        all_tests.extend(run_tests)
        for result in run_results:
            result_id = result.get('id')
            if result_id:
                result_index[str(result_id)] = {'run_id': rid, 'test_id': result.get('test_id')}
    progress('Collecting tests', 0, len(all_tests))
    for i, test in enumerate(all_tests, 1):
        cid = test.get('case_id')
        if cid and str(cid) not in cases:
            case = try_get(api, f'get_case/{cid}', errors, f'case {cid}')
            if case: cases[str(cid)] = case
        tid = test['id']
        attachments = try_collection(api, f'get_attachments_for_test/{tid}', 'attachments', errors, f'test attachments {tid}')
        for a in attachments:
            save_attachment(api, a, 'test', tid, root, files, errors)
        progress('Collecting tests', i, len(all_tests))

    seen = set()
    unresolved = []
    case_attachment_index = {}
    print('Collecting case attachments and embedded images...', flush=True)
    progress('Case evidence', 0, len(cases))
    for i, (cid, case) in enumerate(cases.items(), 1):
        attachments = try_collection(api, f'get_attachments_for_case/{cid}', 'attachments', errors, f'case attachments {cid}')
        case_attachment_index[cid] = attachments
        for a in attachments:
            save_attachment(api, a, 'case', int(cid), root, files, errors)
        scan_embedded(api, case, root, files, errors, seen, unresolved, attachments)
        progress('Case evidence', i, len(cases))
    write_json(root / 'project.json', project)
    write_json(root / 'suites.json', suites)
    write_json(root / 'sections.json', sections)
    write_json(root / 'cases.json', cases)
    write_json(root / 'runs.json', selected)
    write_json(root / 'tests.json', tests)
    write_json(root / 'results.json', results)
    write_json(root / 'result_index.json', result_index)
    write_json(root / 'evidence_index.json', files)
    write_json(root / 'case_attachment_index.json', case_attachment_index)
    write_json(root / 'unresolved_embedded.json', unresolved)
    manifest = {'format_version': 2, 'created_at': datetime.datetime.now().astimezone().isoformat(),
                'source_url': api.base, 'project_id': args.project, 'project_name': project.get('name'), 'selected_run_ids': sorted(selected_ids), 'selected_run_names': {str(r['id']): r.get('name') for r in selected},
                'case_count': len(cases), 'run_count': len(selected), 'result_count': sum(map(len, results.values())),
                'evidence_count': len(files), 'unresolved_embedded_count': len(unresolved), 'errors': errors, 'complete': not errors and not unresolved, 'result_attachment_api_supported': False,
                'note': 'Result attachments collected via get_attachments_for_test; rich-text image links may require web-session authentication. Verify evidence before restoration. Original timestamps/authors retained in JSON.'}
    write_json(root / 'manifest.json', manifest)
    print('\nBACKUP:', root)
    print(f'Cases: {len(cases)} | Runs: {len(selected)} | Evidence: {len(files)} | Errors: {len(errors)}')
    print(f'Known/unresolved embedded references: {len(unresolved)}')
    if errors or unresolved:
        print('WARNING: BACKUP REQUIRES REVIEW. See manifest.json and unresolved_embedded.json.')
    else:
        print('No download/API errors or unresolved embedded references recorded.')
    print('Result attachment coverage still requires verification before restore.')

if __name__ == '__main__': main()
