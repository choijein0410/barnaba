"""Paired baseline/PR comparison; a green audit means equivalent failures.

Neither the production PR nor the upstream repository is modified. Each source
tree is exported at a fixed commit, and each pytest invocation is a new process.
"""
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET

BASE = '48ac4a9496c68fc48698f65c5e0d9563761681ec'
HEAD = 'af4d60a0ea37cf85acb51d1917be68effb40a3ce'
ROOT = Path.cwd()
AUDIT = Path(__file__).resolve().parent
OUT = ROOT / 'results'
OUT.mkdir(exist_ok=True)
SERIES = sys.argv[1]
EXPECTED = json.loads((AUDIT / 'upstream-expectations.json').read_text())[SERIES]
CONSTRAINTS = AUDIT / 'constraints' / (SERIES + '.txt')


def run(cmd, name, cwd=ROOT, env=None, allowed=(0,)):
    print('RUN', name, cmd, flush=True)
    with (OUT / (name + '.log')).open('w') as f:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=f,
                           stderr=subprocess.STDOUT)
    print('EXIT', name, p.returncode, flush=True)
    if p.returncode not in allowed:
        print((OUT / (name + '.log')).read_text()[-12000:], flush=True)
        raise RuntimeError(name + ' failed: ' + str(p.returncode))
    return p.returncode


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT / 'source')] + list(args))


def export(commit, name):
    path = ROOT / name
    path.mkdir()
    with tarfile.open(fileobj=io.BytesIO(git('archive', commit))) as t:
        t.extractall(str(path))
    manifest = {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in path.rglob('*') if p.is_file()}
    (OUT / (name + '-source-hashes.json')).write_text(json.dumps(manifest, indent=2))
    return path


def read_results(name):
    cases = {}
    for case in ET.parse(str(OUT / (name + '.xml'))).getroot().iter('testcase'):
        nodeid = case.attrib['classname'].replace('.', '/') + '.py::' + case.attrib['name']
        problem = case.find('failure')
        if problem is None:
            problem = case.find('error')
        if problem is not None:
            status = problem.tag
            message = problem.attrib.get('message', '')
        else:
            status = 'skipped' if case.find('skipped') is not None else 'passed'
            message = ''
        cases[nodeid] = {'status': status, 'message': message}
    return cases


def pytest(path, name, *extra):
    env = dict(os.environ, PYTHONPATH=str(path))
    run([sys.executable, '-c',
         'import barnaba, json; print(json.dumps({"barnaba":barnaba.__file__}))'],
        name + '-import', path, env)
    rc = run(['pytest', '-v', '--cov=barnaba',
              '--junitxml=' + str(OUT / (name + '.xml'))] + list(extra),
             name, path, env, (0, 1))
    data = read_results(name)
    print(name, {s: sum(v['status'] == s for v in data.values())
                 for s in ('passed', 'failure', 'error', 'skipped')}, flush=True)
    return {'returncode': rc, 'cases': data}


def versions():
    return {name: importlib.metadata.version(name) for name in EXPECTED['packages']}


def normalize(message):
    # Pytest may show different root prefixes in assertion introspection.
    return re.sub(r'0x[0-9a-fA-F]+', '<address>', message).replace(
        str(ROOT / 'baseline'), '<source>').replace(str(ROOT / 'candidate'), '<source>')


assert platform.python_version() == EXPECTED['python']
assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
assert git('rev-parse', HEAD + '^{tree}').decode().strip() == '3f6f45174c5d114d86d304ba8bf6e6e9c3461cb7'
changed = git('diff', '--name-only', BASE, HEAD).decode().splitlines()
assert changed == ['barnaba/nucleic.py', 'test/test_skipped_residues.py'], changed
baseline = export(BASE, 'baseline')
candidate = export(HEAD, 'candidate')
metadata = {
    'base_sha': BASE, 'head_sha': HEAD,
    'source_diff': git('diff', BASE, HEAD).decode(),
    'python': sys.version, 'platform': platform.platform(),
    'machine': platform.machine(),
    'os_release': Path('/etc/os-release').read_text(),
    'runner': {k: os.environ.get(k) for k in
               ['ImageOS', 'ImageVersion', 'RUNNER_OS', 'RUNNER_ARCH', 'GITHUB_RUN_ID']},
    'expected_upstream_image': '20261004.327.1',
}
(OUT / 'environment.json').write_text(json.dumps(metadata, indent=2))

# Use the original install commands, constraining all runtime and pytest
# distributions that were recorded in the upstream logs. Build-isolation
# tool versions were not reported there and are not claimed to be identical.
run(['pip', 'install', '.', '-c', str(CONSTRAINTS)], 'install-baseline', baseline)
run(['pip', 'install', 'pytest', 'pytest-cov', '-c', str(CONSTRAINTS)], 'install-pytest')
before = versions()
assert before == EXPECTED['packages'], (before, EXPECTED['packages'])
run(['pip', 'freeze', '--all'], 'pip-freeze-before')
old = pytest(baseline, 'baseline-full')

# No dependencies are allowed to change between arms of the paired comparison.
run(['pip', 'install', '--no-deps', '.'], 'install-candidate', candidate)
assert versions() == before
new = pytest(candidate, 'candidate-full')
run(['pip', 'freeze', '--all'], 'pip-freeze-after')

# Separately transplant only the eight new tests onto pristine upstream code.
# This happens after the untouched upstream full-suite run.
shutil.copyfile(candidate / 'test/test_skipped_residues.py',
                baseline / 'test/test_skipped_residues.py')
regression = pytest(baseline, 'baseline-new-regressions', 'test/test_skipped_residues.py')
old_cases, new_cases = old['cases'], new['cases']
shared_differences = {key: {'baseline': value, 'candidate': new_cases.get(key)}
                      for key, value in old_cases.items()
                      if key not in new_cases or value['status'] != new_cases[key]['status']
                      or normalize(value['message']) != normalize(new_cases[key]['message'])}
old_failed = sorted(k for k, v in old_cases.items() if v['status'] in ('failure', 'error'))
new_failed = sorted(k for k, v in new_cases.items() if v['status'] in ('failure', 'error'))
expected_failed = sorted(v['nodeid'] for v in EXPECTED['failures'])
added_cases = {k: v for k, v in new_cases.items() if k not in old_cases}
checks = {
    'all_33_shared_tests_have_identical_status_and_failure_message':
        len(old_cases) == 33 and not shared_differences,
    'failure_nodeids_match_original_ci': old_failed == new_failed == expected_failed,
    'all_eight_new_tests_pass_on_patch': len(added_cases) == 8 and
        all(v['status'] == 'passed' for v in added_cases.values()),
    'baseline_new_tests_fail_five_pass_three': len(regression['cases']) == 8 and
        sum(v['status'] == 'failure' for v in regression['cases'].values()) == 5 and
        sum(v['status'] == 'passed' for v in regression['cases'].values()) == 3,
    'runtime_and_test_versions_match_upstream_log': versions() == EXPECTED['packages'],
    'runner_image_matches_original_ci': os.environ.get('ImageVersion') == '20261004.327.1',
}
summary = {'series': SERIES, 'checks': checks, 'baseline': old, 'candidate': new,
           'baseline_new_regressions': regression, 'shared_differences': shared_differences,
           'versions': versions()}
(OUT / 'comparison.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(checks, indent=2), flush=True)
assert all(checks.values()), checks
