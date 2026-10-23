"""Copy a TestRail run into a NEW Untested cycle in a chosen destination project.

Same project: rerun_run (existing cases reused). Different project: clone selected
cases and required sections, then create a clean run (no historical results).
No automatic retries after writes. Case attachments are NOT migrated cross-project.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path
from testrail_common import require_env

SKIP_CASE = {'id','section_id','suite_id','created_by','created_on','updated_by',
             'updated_on','display_order','is_deleted','milestone_id',
             'estimate_forecast','template_id','version'}

def choose(items, label):
    if not items:
        raise SystemExit('No '+label.lower()+' available. No changes made.')
    for i, row in enumerate(items, 1):
        print(f'  {i}. {row[1]}')
    while True:
        x = input('Select number (blank cancels): ').strip()
        if not x: raise SystemExit('Cancelled; nothing created.')
        if x.isdigit() and 1 <= int(x) <= len(items): return items[int(x)-1][0]
        print('Enter one of the listed numbers.')

def verified_tests(api, run_id):
    ts = api.collection(f'get_tests/{run_id}', 'tests')
    if not ts: raise SystemExit('Selected run contains no tests.')
    ids = [int(t['case_id']) for t in ts]
    if len(ids) != len(set(ids)): raise SystemExit('Duplicate source case IDs; cannot safely copy.')
    return ts

def case_payload(c):
    p = {k:v for k,v in c.items() if v is not None and k not in SKIP_CASE
         and (k.startswith('custom_') or k in ('title','estimate','refs'))}
    p['title'] = c['title']
    return p

def get_source_sections(api, pid, suite_id):
    endpoint = f'get_sections/{pid}'
    if suite_id: endpoint += f'&suite_id={suite_id}'
    return {int(x['id']):x for x in api.collection(endpoint,'sections')}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args=parser.parse_args()
    api=require_env('')
    projects=sorted(api.collection('get_projects','projects'),key=lambda p:p.get('name','').casefold())
    print('\nSOURCE PROJECTS:')
    src=choose([(p,f"P{p['id']} — {p['name']}") for p in projects], 'source projects')
    pid=int(src['id'])
    runs=[r for r in api.collection(f'get_runs/{pid}','runs') if not r.get('plan_id')]
    runs.sort(key=lambda r:int(r['id']),reverse=True)
    print('\nSOURCE TEST RUNS:')
    run=choose([(r,f"R{r['id']} — {r['name']}") for r in runs],'test runs')
    old=int(run['id'])
    full=api.get(f'get_run/{old}')
    if int(full['project_id'])!=pid: raise SystemExit('Source run/project mismatch.')
    tests=verified_tests(api,old)
    original_ids={int(t['case_id']) for t in tests}
    print('\nDESTINATION PROJECTS:')
    dest=choose([(p,f"P{p['id']} — {p['name']}"+(' (same project)' if int(p['id'])==pid else '')) for p in projects], 'destinations')
    did=int(dest['id'])
    same=(did==pid)
    suffix=datetime.now().strftime('%Y%m%d_%H%M%S')
    suggestion=f"{full['name']} [Retest {suffix}]"
    name=input(f'\nNew run name (Enter = {suggestion}): ').strip() or suggestion
    if len(name)>240: raise SystemExit('Run name is too long.')
    source_cases={}
    sections={}
    required=set()
    if not same:
        for i,t in enumerate(tests,1):
            cid=int(t['case_id'])
            source_cases[cid]=api.get(f'get_case/{cid}')
            if source_cases[cid].get('title') != t.get('title'):
                raise SystemExit(f'Source test/case title mismatch for C{cid}; aborting.')
        source_suite=int(full.get('suite_id') or 0)
        sections=get_source_sections(api,pid,source_suite)
        for c in source_cases.values():
            sid=int(c['section_id'])
            seen=set()
            while sid:
                if sid in seen or sid not in sections:
                    raise SystemExit('Source section hierarchy incomplete/cyclic; aborting.')
                seen.add(sid);required.add(sid)
                sid=int(sections[sid].get('parent_id') or 0)
    print('\nCOPY A TEST RUN — '+('APPLY' if args.apply else 'PREVIEW'))
    print(f"Source: P{pid}, R{old} — {full['name']}")
    print(f"Destination: P{did} — {dest['name']}")
    print(f'New run: {name}')
    print(f'Tests: {len(tests)}; NEW statuses: Untested; original run unchanged')
    if same:
        print('Same project: existing cases, steps, Expected Results and case attachments reused.')
    else:
        print(f'Other project: create {len(source_cases)} new cases and {len(required)} required sections.')
        print('Steps and Expected Results cloned; old execution results/comments NOT copied.')
        print('WARNING: Case attachments and inline image files are NOT migrated across projects.')
        print('WARNING: Destination-specific custom fields may require compatible TestRail configuration.')
    print('\nSOURCE CASES:')
    for t in tests:
        print(f"  C{t['case_id']}: {t.get('title','')}")
    if not args.apply:
        print('\nPREVIEW ONLY; no changes. Run with --apply to create this new cycle.')
        return
    if input(f'\nCreate this new Untested run in P{did}? [Y/N]: ').strip().lower()!='y':
        raise SystemExit('Cancelled; no changes made.')
    idmap={'source_project':pid,'source_run':old,'destination_project':did,
           'destination_run':None,'suite':None,'sections':{},'cases':{},'state':'started'}
    report=Path.cwd()/f'copy_run_P{pid}_R{old}_to_P{did}_{suffix}.json'
    def save(): report.write_text(json.dumps(idmap,indent=2),encoding='utf-8')
    save()
    try:
        if same:
            created=api.post(f'rerun_run/{old}',{'name':name})
            new_id=int(created['id']);idmap['destination_run']=new_id;save()
            expect=original_ids
        else:
            suite_id=None
            if int(dest.get('suite_mode') or 0)==3:
                source_suite=(api.get(f'get_suite/{int(full["suite_id"])}')
                              if full.get('suite_id') else {'name':full['name'],'description':''})
                new_suite=api.post(f'add_suite/{did}',{'name':f"{source_suite['name']} [Retest {suffix}]",
                                                    'description':source_suite.get('description') or ''})
                suite_id=int(new_suite['id']);idmap['suite']=suite_id;save()
            pending=set(required)
            while pending:
                progressed=False
                for sid in list(pending):
                    sec=sections[sid];parent=int(sec.get('parent_id') or 0)
                    if parent and parent not in idmap['sections']:continue
                    data={'name':sec['name'],'description':sec.get('description') or ''}
                    if parent:data['parent_id']=idmap['sections'][parent]
                    if suite_id:data['suite_id']=suite_id
                    made=api.post(f'add_section/{did}',data)
                    idmap['sections'][sid]=int(made['id']);pending.remove(sid);progressed=True;save()
                if not progressed:raise RuntimeError('Could not map source sections.')
            for t in tests:
                cid=int(t['case_id']);c=source_cases[cid]
                section=idmap['sections'][int(c['section_id'])]
                made=api.post(f'add_case/{section}',case_payload(c))
                idmap['cases'][cid]=int(made['id']);save()
                print(f"Copied case {len(idmap['cases'])}/{len(tests)}: C{cid} -> C{made['id']}")
            expect=set(idmap['cases'].values())
            data={'name':name,'include_all':False,'case_ids':list(expect),
                  'description':(full.get('description') or '')+f'\nFresh copy of P{pid} R{old}'}
            if suite_id:data['suite_id']=suite_id
            created=api.post(f'add_run/{did}',data)
            new_id=int(created['id']);idmap['destination_run']=new_id;save()
        new=verified_tests(api,new_id)
        new_ids={int(t['case_id']) for t in new}
        not_untested=[int(t['id']) for t in new if int(t.get('status_id') or 3)!=3]
        if new_ids!=expect or len(new)!=len(tests) or not_untested:
            raise RuntimeError(f'Verification FAILED: missing={sorted(expect-new_ids)} '
                               f'extra={sorted(new_ids-expect)} not_untested={not_untested}')
        idmap['state']='verified';save()
        print(f'\nSUCCESS: R{new_id} in P{did}; {len(new)} cases, all Untested.')
        print('Original run remains unchanged. Mapping report:',report)
        print(f"Open: {api.base.rstrip('/')}/index.php?/runs/view/{new_id}")
    except Exception:
        idmap['state']='partial_or_failed';save()
        print('ERROR: A partial destination may exist. Do not rerun until inspected.')
        print('Mapping report:',report)
        raise
if __name__=='__main__':main()
