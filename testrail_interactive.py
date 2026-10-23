"""Interactive launcher for the TestRail final backup and restore scripts.

Requires testrail_common.py, testrail_backup_final.py, and
 testra il_restore_final.py (without the space) in the same directory.
Run: py testrail_interactive.py
"""
import sys
from testrail_common import require_env


def choose(title, options, label):
    if not options:
        raise SystemExit(f'No {title.lower()} available.')
    print(f'\n{title}:')
    for i, obj in enumerate(options, 1):
        print(f'  {i}. {label(obj)}')
    while True:
        value = input('Select number (blank cancels): ').strip()
        if not value:
            raise SystemExit('Cancelled.')
        if value.isdigit() and 1 <= int(value) <= len(options):
            return options[int(value)-1]
        print('Invalid selection.')


def yes_no(message):
    while True:
        response = input(message + ' [Y/N]: ').strip().lower()
        if response in ('y', 'n'):
            return response == 'y'
        print('Enter Y or N.')


def main():
    action = choose('TESTRAIL', ['Backup', 'Restore preview', 'Restore apply'], lambda x: x)
    if action.startswith('Restore'):
        import testrail_restore_final
        sys.argv = ['testrail_restore_final.py'] + (['--apply'] if action == 'Restore apply' else [])
        return testrail_restore_final.main()

    api = require_env('')
    projects = sorted(api.collection('get_projects', 'projects'), key=lambda p: p.get('name', '').casefold())
    project = choose('SOURCE PROJECTS', projects, lambda p: f"P{p['id']} — {p.get('name','')}")
    pid = int(project['id'])
    scope = choose('BACKUP SCOPE', ['Test plan', 'Single test run', 'Multiple test runs', 'All runs'], lambda x: x)
    flags = ['--project', str(pid)]
    if scope == 'Test plan':
        plans = api.collection(f'get_plans/{pid}', 'plans')
        plan = choose('TEST PLANS', plans, lambda p: f"Plan {p['id']} — {p.get('name','')}" )
        full_plan = api.get(f"get_plan/{plan['id']}")
        runs = []
        for entry in full_plan.get('entries', []):
            runs.extend(entry.get('runs', []))
        ids = sorted({int(r['id']) for r in runs if r.get('id') is not None})
        if not ids:
            raise SystemExit('This plan has no runs. Nothing to back up.')
        print(f"\nPlan: {full_plan.get('name', plan.get('name'))}")
        for r in runs:
            print(f"  R{r.get('id')}: {r.get('name','')}")
        print(f'Runs selected: {len(ids)}')
        flags += ['--runs', ','.join(map(str, ids)), '--all-runs']
    elif scope in ('Single test run', 'Multiple test runs'):
        runs = api.collection(f'get_runs/{pid}', 'runs')
        if scope == 'Single test run':
            r = choose('TEST RUNS', runs, lambda r: f"R{r['id']} — {r.get('name','')}")
            ids = [int(r['id'])]
        else:
            print('\nAVAILABLE RUNS:')
            for i, r in enumerate(runs, 1):
                print(f"  {i}. R{r['id']} — {r.get('name','')}")
            raw = input('Select run NUMBERS separated by commas (blank cancels): ').strip()
            if not raw:
                raise SystemExit('Cancelled.')
            try:
                nums = [int(x.strip()) for x in raw.split(',')]
                if not nums or any(n < 1 or n > len(runs) for n in nums):
                    raise ValueError
            except ValueError:
                raise SystemExit('Invalid run selection.')
            ids = sorted({int(runs[n-1]['id']) for n in nums})
        flags += ['--runs', ','.join(map(str, ids)), '--all-runs']
    else:
        flags += ['--all-runs']
    print('\nThe backup script will display the final confirmation before writing files.')
    import testrail_backup_final
    sys.argv = ['testrail_backup_final.py'] + flags
    return testrail_backup_final.main()


if __name__ == '__main__':
    main()
