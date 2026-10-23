"""TestRail Restore V7 — membership-verified, duplicate-protected restore.

Preview by default; --apply enables writes after Y confirmation.
Creates only cases selected in backed-up run(s), plus necessary parent sections.
Hard-stops if a newly created destination run contains unexpected tests/titles.
Deduplicates identical evidence index rows, not distinct attachments.
Historical author/timestamps and some result-attachment ownership may be limited.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from testrail_common import read_json, write_json, require_env, checksum

CASE_SKIP = {'id','section_id','suite_id','created_by','created_on','updated_by','updated_on',
             'display_order','is_deleted','milestone_id','estimate_forecast','template_id'}
RESULT_FIELDS = {'status_id','comment','version','elapsed','defects'}
SUPPORTED = {'.jpg','.jpeg','.png','.gif','.bmp','.webp','.csv','.mp4','.txt','.pdf','.zip','.json','.log'}


def progress(label, current, total, width=30):
    """One updating terminal line per restore phase, like backup V3."""
    total = max(1, total)
    fraction = min(1.0, current / total)
    filled = round(width * fraction)
    bar = '#' * filled + '-' * (width - filled)
    print(f"\r{label:<20} [{bar}] {fraction:3.0%} ({current}/{total})", end='\n' if current >= total else '', flush=True)


def choose(items, prompt):
    while True:
        answer = input(f"\n{prompt} (number, blank = cancel): ").strip()
        if not answer:
            raise SystemExit("Cancelled. No changes made.")
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            return items[int(answer)-1]
        print("Choose a listed number.")


def validate_backup_membership(manifest, cases, sections, suites, runs, tests, results):
    """Refuse backup/run/case mismatches before any TestRail writes."""
    problems = []
    source_run_ids = {str(x) for x in manifest.get('selected_run_ids', [])}
    actual_run_ids = {str(x.get('id')) for x in runs}
    if not runs or len(actual_run_ids) != len(runs):
        problems.append('No runs or repeated run IDs')
    if source_run_ids and actual_run_ids != source_run_ids:
        problems.append(f'Run selection differs from manifest: {sorted(source_run_ids)} vs {sorted(actual_run_ids)}')
    if set(tests) != actual_run_ids:
        problems.append(f'Test run IDs differ from selected runs: {sorted(tests)} vs {sorted(actual_run_ids)}')
    if set(results) - actual_run_ids:
        problems.append('Results reference an unselected run')
    all_case_ids = set()
    for run in runs:
        run_id = str(run['id'])
        run_tests = tests.get(run_id, [])
        if not run_tests:
            problems.append(f'R{run_id}: no tests')
        seen_tests, seen_cases = set(), set()
        for t in run_tests:
            tid, cid = str(t.get('id')), str(t.get('case_id'))
            if str(t.get('run_id')) != run_id:
                problems.append(f'T{tid} has wrong run_id {t.get("run_id")}')
            if tid in seen_tests or cid in seen_cases:
                problems.append(f'R{run_id}: repeated T{tid} or C{cid}')
            seen_tests.add(tid); seen_cases.add(cid); all_case_ids.add(cid)
            c = cases.get(cid)
            if not c:
                problems.append(f'T{tid} refers to C{cid} missing from cases.json')
            elif t.get('title') != c.get('title'):
                problems.append(f'T{tid}/C{cid} title mismatch: {t.get("title")!r} != {c.get("title")!r}')
        for result in results.get(run_id, []):
            if str(result.get('test_id')) not in seen_tests:
                problems.append(f'R{run_id} result {result.get("id")} refers to an unselected test')
    if set(cases) != all_case_ids:
        problems.append(f'cases.json must contain only selected cases: extra={sorted(set(cases)-all_case_ids)} missing={sorted(all_case_ids-set(cases))}')
    sec = {str(x['id']): x for x in sections}
    suite_ids = {str(x['id']) for x in suites}
    for cid, c in cases.items():
        section_id = str(c.get('section_id'))
        if section_id not in sec:
            problems.append(f'C{cid}: missing source section {section_id}')
        if c.get('suite_id') is not None and str(c['suite_id']) not in suite_ids:
            problems.append(f'C{cid}: missing source suite {c["suite_id"]}')
    for run in runs:
        sid = run.get('suite_id')
        if sid is not None and str(sid) not in suite_ids:
            problems.append(f'R{run["id"]}: missing source suite {sid}')
    if problems:
        raise SystemExit('REFUSED: backup membership validation failed:\n  - ' + '\n  - '.join(problems[:30]))
    return all_case_ids


def select_required_sections(sections, cases):
    """Restore only sections containing selected cases and their ancestors."""
    index = {str(x['id']): x for x in sections}
    wanted = set()
    for c in cases.values():
        sid = str(c['section_id'])
        seen = set()
        while sid not in ('None', '', '0'):
            if sid not in index:
                raise SystemExit(f'REFUSED: missing section ancestor {sid}')
            if sid in seen:
                raise SystemExit(f'REFUSED: cycle in section tree at {sid}')
            seen.add(sid); wanted.add(sid)
            parent = index[sid].get('parent_id')
            sid = str(parent) if parent else ''
    return [s for s in sections if str(s['id']) in wanted]


def dedupe_evidence(records):
    """Duplicate index rows do not represent separate evidence uploads."""
    unique = {}
    for rec in records:
        key = str(rec.get('path', ''))
        if not key:
            raise SystemExit('REFUSED: evidence index row has no file path')
        old = unique.get(key)
        if old is not None:
            critical = ('owner_type', 'owner_id', 'attachment_id', 'sha256', 'bytes', 'original_filename')
            if any(str(old.get(k)) != str(rec.get(k)) for k in critical):
                raise SystemExit(f'REFUSED: conflicting duplicate evidence index records: {key}')
        else:
            unique[key] = rec
    return list(unique.values())


def check_new_run_membership(api, run_id, source_tests, case_map):
    """Hard stop before results/evidence if TestRail inserted incorrect cases."""
    dest_tests = api.collection(f'get_tests/{run_id}', 'tests')
    expected = {}
    for t in source_tests:
        source_case = str(t['case_id'])
        mapped_case = case_map.get(source_case)
        if not mapped_case:
            raise RuntimeError(f'No destination case mapping for source C{source_case}')
        expected[str(mapped_case)] = t['title']
    actual = {str(t.get('case_id')): t for t in dest_tests}
    if len(actual) != len(dest_tests) or set(actual) != set(expected):
        raise RuntimeError(
            f'DESTINATION RUN MEMBERSHIP MISMATCH for R{run_id}: '
            f'expected case IDs {sorted(expected)}, got {sorted(actual)}. '
            'No results or attachments have been uploaded.')
    wrong_titles = [(cid, expected[cid], actual[cid].get('title'))
                    for cid in expected if expected[cid] != actual[cid].get('title')]
    if wrong_titles:
        raise RuntimeError(f'DESTINATION RUN TITLE MISMATCH for R{run_id}: {wrong_titles[:5]}. '
                           'No results or attachments have been uploaded.')
    return dest_tests


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backup-root', default=r'C:\TR\Backup')
    p.add_argument('--apply', action='store_true', help='Enable actual destination writes')
    p.add_argument('--skip-results', action='store_true', help='Skip execution history (also prompted interactively)')
    p.add_argument('--skip-evidence', action='store_true')
    p.add_argument('--destination-project', type=int, default=None, help='Optional destination project ID; defaults to numbered selection')
    args = p.parse_args()
    parent = Path(args.backup_root).expanduser()
    if not parent.is_dir():
        raise SystemExit(f"Backup root not found: {parent}")
    backups = []
    for folder in sorted(parent.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not folder.is_dir() or not (folder/'manifest.json').is_file(): continue
        try:
            m = read_json(folder/'manifest.json')
            if m.get('format_version') == 2:
                backups.append((folder,m))
        except Exception as exc:
            print(f"Skipping {folder.name}: {exc}")
    if not backups:
        raise SystemExit("No compatible backups found.")
    print("\nAVAILABLE BACKUPS:")
    for i,(folder,m) in enumerate(backups,1):
        runlabels = ', '.join(f"R{rid}: {m.get('selected_run_names',{}).get(str(rid),'')}"
                              for rid in m.get('selected_run_ids',[]))
        print(f"{i}. {m.get('project_name','Unknown project')} | {runlabels}")
        print(f"   {folder.name}")
    root, manifest = choose(backups,'Select backup')
    api = require_env('')
    if api.base.rstrip('/') != str(manifest.get('source_url','')).rstrip('/'):
        raise SystemExit("Backup source account does not match configured TestRail URL.")
    projects = sorted(api.collection('get_projects','projects'),key=lambda x:x.get('name','').casefold())
    print("\nAVAILABLE DESTINATION PROJECTS:")
    for i,project in enumerate(projects,1):
        print(f"{i}. P{project['id']}: {project.get('name')}")
    dest = next((x for x in projects if int(x['id']) == args.destination_project), None) if args.destination_project else choose(projects,'Select destination project')
    if not dest: raise SystemExit('Destination project not found.')
    project_id = dest['id']
    if project_id == manifest.get('project_id'):
        raise SystemExit("REFUSED: destination cannot be the original source project.")
    cases = read_json(root/'cases.json')
    sections = read_json(root/'sections.json')
    suites = read_json(root/'suites.json')
    runs = read_json(root/'runs.json')
    tests = read_json(root/'tests.json')
    results = read_json(root/'results.json')
    evidence_records = read_json(root/'evidence_index.json')
    unique_evidence = dedupe_evidence(evidence_records)
    duplicate_evidence = len(evidence_records) - len(unique_evidence)
    selected_cases = validate_backup_membership(manifest, cases, sections, suites, runs, tests, results)
    required_sections = select_required_sections(sections, cases)
    print('\nSOURCE RUN CONTENT — verify these are the cases you expect:')
    for run in runs:
        print(f'  R{run["id"]}: {run.get("name", "")}')
        for source_test in tests[str(run['id'])]:
            print(f'    T{source_test["id"]} / C{source_test["case_id"]}: {source_test["title"]}')
    print(f'  Source case IDs checked: {len(selected_cases)}; source title mismatches: 0')
    print(f'  Sections needed for these cases: {len(required_sections)} of {len(sections)}')
    print(f'  Evidence index rows: {len(evidence_records)}; unique files: {len(unique_evidence)}; repeated rows: {duplicate_evidence}')
    evidence = unique_evidence
    failures = []
    for item in evidence:
        relative = item.get('path','')
        path = (root/relative).resolve()
        if not path.is_relative_to(root.resolve()):
            failures.append(f"Unsafe path: {relative}")
        elif not path.is_file():
            failures.append(f"Missing: {relative}")
        elif item.get('sha256') and checksum(path) != item['sha256']:
            failures.append(f"Checksum mismatch: {relative}")
    print("\n" + "="*65)
    print("RESTORE V7 — " + ("APPLY REQUESTED" if args.apply else "PREVIEW ONLY"))
    print("="*65)
    print(f"Source: P{manifest.get('project_id')} {manifest.get('project_name')}")
    print(f"Destination: P{project_id} {dest.get('name')}")
    print(f"Cases: {len(cases)} | Runs: {len(runs)} | Results: {sum(map(len,results.values()))} | Unique evidence: {len(evidence)}")
    print(f"Known unresolved embedded links: {manifest.get('unresolved_embedded_count',0)}")
    print(f"Backup download errors: {len(manifest.get('errors',[]))} | Evidence verification failures: {len(failures)}")
    print("LIMITATIONS: source rich-text image URLs are not relinked; historical author/time cannot be recreated.")
    print("Result evidence coverage is not guaranteed by the source backup.")
    # Interactive choice applies to both preview and actual restoration.
    if args.skip_results:
        restore_results = False
        print('Restore test results? NO (--skip-results)')
    else:
        while True:
            answer = input('Restore test results? [Y/N]: ').strip().lower()
            if answer in ('y', 'n'):
                restore_results = answer == 'y'
                break
            print('Please enter Y or N.')
    args.skip_results = not restore_results
    print('Selected mode:', 'Cases + runs + results + evidence' if restore_results else
          'Cases + runs + case/test evidence (NO execution results)')
    if failures or manifest.get('errors'):
        raise SystemExit("REFUSED: backup has missing/corrupt evidence or download errors.")
    if not args.apply:
        print("\nPREVIEW FINISHED. No TestRail changes made.")
        print("To attempt restore, rerun with --apply; existing suites are allowed; existing test runs are not.")
        return
    # Abort if the same source run was already restored to this destination,
    # even if the previous backup lives in a different directory.
    if suites and dest.get('suite_mode') != 3:
        raise SystemExit('REFUSED: source suites require a multi-suite destination.')
    existing_runs = api.collection(f'get_runs/{project_id}', 'runs')
    existing_names = {str(r.get('name','')).casefold() for r in existing_runs}
    for source_run in runs:
        marker = f'Restore P{manifest["project_id"]} R{source_run["id"]}'
        found = [r for r in existing_runs if marker.lower() in str(r.get('name', '')).lower()]
        if found:
            formatted = ', '.join(f'R{r["id"]}: {r.get("name", "")}' for r in found)
            raise SystemExit('REFUSED: the same source run was already restored here: ' + formatted +
                             '. Delete the old restored run and its cases deliberately before starting a new restore.')
    # A restored run name must be distinct to prevent confusing copies.
    for source_run in runs:
        candidate = f"{source_run['name']} [Restore P{manifest['project_id']} R{source_run['id']} {datetime.now():%Y%m%d_%H%M%S}]"
        source_run['_restore_name'] = candidate[:240]
        if source_run['_restore_name'].casefold() in existing_names:
            raise SystemExit('REFUSED: duplicate destination run name.')
    if (root/f'restore_mapping_project_{project_id}.json').exists():
        raise SystemExit('REFUSED: a mapping already exists for this backup/destination. Use a NEW backup for the next test, or investigate partial restore.')
    print(f'Existing destination runs: {len(existing_runs)}. They will not be modified.')
    print(f'This restore will create exactly {len(cases)} cases in {len(required_sections)} necessary sections.')
    print('The exact source test titles are shown above. Make sure they match your intended run.')
    print("\nWARNING: This will CREATE cases, sections, runs, results and attachments in the destination.")
    print("It will not overwrite source data, but a failed restore may leave a PARTIAL destination.")
    if input("Proceed with restore? [Y/N]: ").strip().lower() != "y":
        raise SystemExit("Cancelled. No changes made.")
    idmap = {'suites': {}, 'sections': {}, 'cases': {}, 'runs': {}, 'tests': {}, 'results': {},
             'attachments': {}, 'warnings': [], 'errors': []}
    # Persist a record BEFORE any remote writes, so interrupted restores cannot be repeated accidentally.
    out = root / f'restore_mapping_project_{project_id}.json'
    def persist(): write_json(out, idmap)
    persist()
    try:
        if suites:
            progress('Restoring suites', 0, len(suites))
            # Destination must support multiple suites for source suite hierarchy.
            for suite_number, s in enumerate(suites, 1):
                new = api.post(f'add_suite/{project_id}', {'name': s['name'], 'description': s.get('description') or ''})
                idmap['suites'][str(s['id'])] = new['id']; persist()
                progress('Restoring suites', suite_number, len(suites))
        else:
            # Single-suite mode. Sections can be created without suite_id.
            pass
        remaining = {str(s['id']): s for s in required_sections}
        if required_sections: progress('Restoring sections', 0, len(required_sections))
        while remaining:
            progressed = False
            for sid, s in list(remaining.items()):
                parent = s.get('parent_id')
                if parent and str(parent) not in idmap['sections']: continue
                payload = {'name': s['name'], 'description': s.get('description') or ''}
                if parent: payload['parent_id'] = idmap['sections'][str(parent)]
                if s.get('suite_id') and str(s['suite_id']) in idmap['suites']:
                    payload['suite_id'] = idmap['suites'][str(s['suite_id'])]
                new = api.post(f'add_section/{project_id}', payload)
                idmap['sections'][sid] = new['id']; del remaining[sid]; progressed = True; persist()
                progress('Restoring sections', len(required_sections) - len(remaining), len(required_sections))
            if not progressed: raise RuntimeError('Cannot resolve source section hierarchy')
        if cases: progress('Restoring cases', 0, len(cases))
        for case_number, (cid, c) in enumerate(cases.items(), 1):
            sec = idmap['sections'].get(str(c.get('section_id')))
            if not sec: raise RuntimeError(f'Case {cid} section {c.get("section_id")} not found')
            payload = {k: v for k, v in c.items() if k not in CASE_SKIP and (k.startswith('custom_') or k in ('title', 'type_id', 'priority_id', 'estimate', 'refs')) and v is not None}
            # New destination may have different type/priority IDs; remove to avoid wrong mapping.
            payload.pop('type_id', None); payload.pop('priority_id', None)
            payload['title'] = c['title']
            payload['refs'] = str(c.get('refs') or '')  # Original ID is in mapping JSON
            new = api.post(f'add_case/{sec}', payload)
            if new.get('title') and new['title'] != c['title']:
                raise RuntimeError(f'DESTINATION CASE TITLE MISMATCH: source C{cid} {c["title"]!r}, created {new["title"]!r}')
            idmap['cases'][cid] = new['id']; persist()
            progress('Restoring cases', case_number, len(cases))
        # Create runs using explicit case selection; plan membership is not recreated by v1.
        if runs: progress('Restoring runs', 0, len(runs))
        total_results = 0 if args.skip_results else sum(len(v) for v in results.values())
        result_number = 0
        for run_number, run in enumerate(runs, 1):
            old_id = str(run['id'])
            source_case_ids = list(dict.fromkeys(str(t['case_id']) for t in tests.get(old_id, []) if t.get('case_id')))
            new_case_ids = [idmap['cases'][c] for c in source_case_ids]
            payload = {'name': run['_restore_name'], 'description': (run.get('description') or '') + f'\n[Restored from original run R{old_id}]',
                       'include_all': False, 'case_ids': new_case_ids}
            if run.get('suite_id') and str(run['suite_id']) in idmap['suites']:
                payload['suite_id'] = idmap['suites'][str(run['suite_id'])]
            new = api.post(f'add_run/{project_id}', payload)
            idmap['runs'][old_id] = new['id']; persist()
            progress('Restoring runs', run_number, len(runs))
            new_tests = check_new_run_membership(api, new['id'], tests.get(old_id, []), idmap['cases'])
            print(f'VERIFIED R{new["id"]}: exactly {len(new_tests)} expected case IDs and titles match.')
            if args.skip_results:
                continue
            by_case = {str(t['case_id']): t['id'] for t in new_tests}
            for source_test in tests.get(old_id, []):
                mapped_case = idmap['cases'].get(str(source_test.get('case_id')))
                mapped_test = by_case.get(str(mapped_case))
                if mapped_test: idmap['tests'][str(source_test['id'])] = mapped_test
            persist()
            old_test_case = {str(t['id']): str(t['case_id']) for t in tests.get(old_id, [])}
            # Replaying results in chronological order preserves sequence, but not original timestamps.
            for res in sorted(results.get(old_id, []), key=lambda x: (x.get('created_on') or 0, x.get('id') or 0)):
                result_number += 1
                original_case = old_test_case.get(str(res.get('test_id')))
                if not original_case:
                    progress('Restoring results', result_number, total_results)
                    continue
                new_case = idmap['cases'].get(original_case)
                new_test = by_case.get(str(new_case))
                if not new_test: raise RuntimeError(f'New test missing for case {original_case}')
                payload = {k: v for k, v in res.items() if k in RESULT_FIELDS and v is not None}
                payload.pop('assignedto_id', None)  # user IDs are account-specific
                # Keep the source comment clean. Original authors/timestamps remain in backup metadata.
                payload['comment'] = str(payload.get('comment') or '')
                if not payload.get('status_id'):
                    progress('Restoring results', result_number, total_results)
                    continue
                created = api.post(f'add_result/{new_test}', payload)
                idmap['results'][str(res['id'])] = created['id']; persist()
                complexity = res.get('custom_result_hvacomplexity')
                if complexity is not None:
                    try:
                        response = api.s.post(api.api + f"edit_result/{created['id']}", json={'custom_result_hvacomplexity': complexity}, timeout=(20,180))
                        response.raise_for_status()
                    except Exception as exc:
                        idmap['errors'].append({'result':res['id'],'complexity_error':str(exc)}); persist()
                progress('Restoring results', result_number, total_results)
        if not args.skip_results:
            print('\nVerifying final test statuses...')
            for original_run in runs:
                source_run_id = str(original_run['id'])
                target_run_id = idmap['runs'][source_run_id]
                restored = api.collection(f'get_tests/{target_run_id}', 'tests')
                statuses = {str(t['case_id']): t.get('status_id') for t in restored}
                for st in tests.get(source_run_id, []):
                    dest_case = idmap['cases'][str(st['case_id'])]
                    expected_status = st.get('status_id')
                    if statuses.get(str(dest_case)) != expected_status:
                        raise RuntimeError(
                            f'STATUS MISMATCH in R{target_run_id} C{dest_case}: '
                            f'source T{st["id"]} status={expected_status}, '
                            f'destination status={statuses.get(str(dest_case))}. '
                            'Evidence upload has not started; inspect partial restore.')
                print(f'  R{target_run_id}: {len(tests[source_run_id])} test statuses verified.')
        if not args.skip_evidence:
            # Source result attachment IDs are exposed on get_results_for_run.
            # The backed-up bytes live under owner_type=test; upload them to the
            # mapped execution result, not just the destination case.
            result_owners = {}
            for source_run, source_results in results.items():
                for res in source_results:
                    for aid in res.get('attachment_ids') or []:
                        key = (str(res.get('test_id')), str(aid))
                        result_owners.setdefault(key, []).append(str(res['id']))
            # A result can appear twice in the source API/index. Treat repeated
            # occurrences of the SAME result ID as one owner, not ambiguity.
            result_owners = {key: list(dict.fromkeys(owners))
                             for key, owners in result_owners.items()}
            case_by_test = {str(t['id']): str(t['case_id']) for group in tests.values() for t in group}
            total = len(evidence)
            progress('Restoring evidence', 0, total)
            for evidence_number, item in enumerate(evidence, 1):
                typ, old = item['owner_type'], str(item['owner_id'])
                path = (root / item['path']).resolve()
                filename = str(item.get('original_filename') or path.name)
                ext = Path(filename).suffix.lower()
                if not path.is_relative_to(root.resolve()) or not path.is_file():
                    raise RuntimeError('Evidence path missing/unsafe: '+str(item['path']))
                if path.stat().st_size != item['bytes'] or checksum(path) != item['sha256']:
                    raise RuntimeError('Evidence changed during restore: '+str(item['path']))
                if ext not in SUPPORTED and typ != 'embedded':
                    idmap['errors'].append({'attachment':item['path'],'reason':'Unsupported extension: '+ext}); persist()
                    progress('Restoring evidence', evidence_number, total)
                    continue
                target_type, dest = None, None
                if typ == 'case':
                    target_type, dest = 'case', idmap['cases'].get(old)
                elif typ == 'embedded':
                    target_type, dest = 'case', idmap['cases'].get(old)
                elif typ == 'test':
                    owners = result_owners.get((old, str(item.get('attachment_id'))), [])
                    if len(owners) == 1 and not args.skip_results:
                        dest = idmap['results'].get(owners[0]); target_type = 'result'
                    elif owners:
                        idmap['errors'].append({'attachment':item['path'],'reason':'Multiple distinct result owners: '+str(owners)})
                    else:
                        # No TestRail API to attach directly to a test: preserve
                        # the file on its case, and record the ownership change.
                        dest = idmap['cases'].get(case_by_test.get(old)); target_type = 'case'
                if not dest or not target_type:
                    idmap['errors'].append({'attachment':item['path'],'reason':'No destination mapping'})
                    persist(); progress('Restoring evidence', evidence_number, total); continue
                endpoint = f'add_attachment_to_{target_type}/{dest}'
                # Non-idempotent upload: no automatic retries on ambiguous failure.
                try:
                    import mimetypes
                    mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                    with path.open('rb') as handle:
                        response = api.s.post(api.api + endpoint,
                                              files={'attachment':(filename,handle,mime)},
                                              timeout=(20,600))
                    if not response.ok:
                        raise RuntimeError(f'HTTP {response.status_code}: {response.text[:400]}')
                    payload = response.json()
                    attachment_id = payload.get('attachment_id',payload.get('id'))
                    idmap['attachments'][item['path']] = {
                        'old_id':item.get('attachment_id'), 'new_id':attachment_id,
                        'owner_type':typ, 'destination_type':target_type,
                        'destination_id':dest, 'filename':filename,
                        'source_result_ids':result_owners.get((old,str(item.get('attachment_id'))),[])
                    }
                except Exception as exc:
                    idmap['errors'].append({'attachment':item['path'],'error':str(exc),'action':'inspect before retry'})
                    persist()
                    raise RuntimeError('Evidence upload failed/uncertain. Stop to avoid duplicates: '+str(exc)) from exc
                persist()
                progress('Restoring evidence', evidence_number, total)
        if not args.skip_evidence and len(idmap['attachments']) != len(evidence):
            msg = (f'INCOMPLETE EVIDENCE: uploaded {len(idmap["attachments"])} of '
                   f'{len(evidence)} unique indexed files. The restore is partial.')
            idmap['errors'].append({'evidence_incomplete': msg})
            persist()
            raise RuntimeError(msg)
        # Relink only exact backed-up embedded src URLs for which we have
        # a verified upload response ID. Preserve all unrelated HTML/fields.
        from testrail_common import IMAGE_RE
        embedded_by_case = {}
        for item in evidence:
            if item.get('owner_type') != 'embedded': continue
            uploaded = idmap['attachments'].get(item['path'], {})
            new_id = uploaded.get('new_id')
            if not new_id:
                idmap['errors'].append({'attachment':item['path'],'reason':'No new ID to relink embedded image'})
                continue
            embedded_by_case.setdefault(str(item['owner_id']), []).append((str(item.get('source_url') or ''), str(new_id)))
        for old_case, image_links in embedded_by_case.items():
            source_case = cases.get(old_case)
            new_case = idmap['cases'].get(old_case)
            if not source_case or not new_case: continue
            changed = {}
            def replace_value(value):
                if isinstance(value, str):
                    def change_src(match):
                        original = match.group('url')
                        for old_url, new_id in image_links:
                            if old_url and original == old_url:
                                return match.group('prefix') + f'{api.base}/index.php?/attachments/get/{new_id}' + match.group('suffix')
                        return match.group(0)
                    return IMAGE_RE.sub(change_src, value)
                if isinstance(value, list): return [replace_value(x) for x in value]
                if isinstance(value, dict): return {k:replace_value(v) for k,v in value.items()}
                return value
            for field, value in source_case.items():
                if field.startswith('custom_') and isinstance(value, (str,list,dict)):
                    new_value = replace_value(value)
                    if new_value != value: changed[field] = new_value
            if changed:
                # update_case is idempotent for the same values; however no
                # automatic retry is made on network failures.
                response = api.s.post(api.api + f'update_case/{new_case}', json=changed, timeout=(20,180))
                if not response.ok:
                    idmap['errors'].append({'case':old_case,'relink_error':f'HTTP {response.status_code}: {response.text[:300]}'})
                else:
                    idmap.setdefault('relinked_cases',{})[old_case] = list(changed)
            persist()
        idmap['warnings'].append('Verify rich-text embedded image rendering and exact image identity in TestRail.')
        idmap['warnings'].append('Historical author/timestamp cannot be recreated; source metadata is preserved in backup JSON.')
        persist()
    except Exception as e:
        idmap['errors'].append({'fatal': str(e)})
        persist()
        raise
    print('RESTORE ATTEMPT FINISHED. Verify restored cases, runs, results and images in TestRail. ID mapping:', out)
    print('Execution results:', 'SKIPPED BY CHOICE' if args.skip_results else 'ATTEMPTED')
    print('Unique attachments uploaded:', len(idmap['attachments']), 'of', len(evidence), 'unique files')
    print('Original index records:', len(evidence_records), '| Duplicated index rows:', duplicate_evidence)
    print('Restored destination runs:', {k: v for k, v in idmap['runs'].items()})
    print('Embedded image rendering must be checked in TestRail before calling this a complete restore.')
    print('Evidence/restore errors:', len(idmap['errors']))
    if not idmap['errors'] and len(idmap['attachments']) == len(evidence) and not args.skip_evidence:
        print('All unique indexed evidence files have upload records. Verify placement in TestRail.')
    if idmap['errors']: print('Review mapping errors; some evidence may need manual relinking.')


if __name__ == '__main__':
    main()
