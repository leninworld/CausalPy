"""Proposed I1 container controller. Execution requires exact packet approval."""
from pathlib import Path
import csv
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

P = Path('/packet')
R = Path('/runtime')
M = json.loads((P / 'manifest.json').read_text())
KIND = os.environ['VALIDATION_KIND']
MODE = os.environ.get('VALIDATION_MODE', 'baseline')
PY = os.environ.get('VALIDATION_PYTHON', '3.12')
GROUP = os.environ.get('VALIDATION_PANDAS', 'pandas3')
END = float(os.environ['VALIDATION_END_EPOCH'])
START = float(os.environ['VALIDATION_START_EPOCH'])
ROOT = Path('/work/source')
STEPS = []
BASE_ENV = dict(os.environ, MPLBACKEND='Agg', PYTHONPATH='/packet',
                PYTEST_ADDOPTS='', UV_PYTHON_DOWNLOADS='never',
                PYTENSOR_FLAGS='base_compiledir=/work/pytensor-main',
                MPLCONFIGDIR='/work/matplotlib')


def snapshot():
    group = Path('/sys/fs/cgroup')
    current = int((group / 'memory.current').read_text())
    stat = dict(line.split() for line in (group / 'memory.stat').read_text().splitlines())
    return {'at': time.time(), 'current': current,
            'working': max(0, current - int(stat.get('inactive_file', 0))),
            'peak': int((group / 'memory.peak').read_text()),
            'limit': int((group / 'memory.max').read_text()),
            'free': shutil.disk_usage('/work').free}


def guard(future=0):
    assert time.time() + future < END, 'GLOBAL_REMAINING_FORECAST'
    s = snapshot()
    assert s['working'] < M['work_pressure_gib'] * 1024**3, 'WORKING_MEMORY_STOP'
    assert s['free'] > 4 * 1024**3, 'CONTAINER_DISK_STOP'
    return s


def run(name, args, seconds, expected=0, cwd=ROOT, extra_env=None):
    guard()
    start = time.monotonic()
    env = dict(BASE_ENV, **(extra_env or {}))
    log = R / (name + '.log')
    assert not log.exists()
    timeout = min(seconds, END - time.time() - 15)
    assert timeout > 0
    with log.open('x') as out:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        problem = None
        try:
            while proc.poll() is None:
                guard()
                assert time.monotonic() - start < timeout, 'STEP_TIMEOUT_' + name
                assert log.stat().st_size < 32 * 1024**2, 'OUTPUT_LIMIT_' + name
                time.sleep(.5)
        except BaseException as error:
            problem = str(error)
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            raise
        finally:
            STEPS.append({'name': name, 'args': args, 'exit': proc.returncode,
                          'seconds': time.monotonic() - start, 'error': problem,
                          'resource': snapshot()})
    assert proc.returncode == expected, 'STEP_EXIT_' + name


def verify_overlay():
    for path, expected in M['overlay_sha256'].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected


def initialize_source():
    shutil.copytree('/source', ROOT)
    assert subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                   text=True).strip() == M['base']
    for path, expected in M['baseline_sha256'].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected
    assert hashlib.sha256((ROOT / 'uv.lock').read_bytes()).hexdigest() == M['uv_lock_sha256']
    if MODE == 'candidate':
        for path in M['overlay_sha256']:
            target = ROOT / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(P / 'overlay' / path, target)
        run('intent-test', ['git', 'add', '-N',
                            'causalpy/tests/test_ipw_treatment_validation.py'], 10)
        verify_overlay()


def focused_regressions():
    path = 'causalpy/tests/test_ipw_treatment_validation.py'
    if MODE == 'baseline':
        shutil.copyfile(P / 'overlay' / path, ROOT / path)
    run('regressions', ['uv', 'run', '--no-sync', 'pytest', '-o', 'addopts=',
                       '--no-cov', '-n', '0', '-q', '-p', 'witness', path], 120,
        expected=1 if MODE == 'baseline' else 0,
        extra_env={'WITNESS_PATH': str(R / 'regressions.json')})
    actual = json.loads((R / 'regressions.json').read_text())['reports']
    wanted = json.loads((P / 'regression-contract.json').read_text())['cases']
    assert set(actual) == {path + '::' + x['test_id'] for x in wanted}
    for case in wanted:
        row = actual[path + '::' + case['test_id']]
        assert row['setup'] == row['teardown'] == 'passed', 'REGRESSION_FIXTURE_ERROR'
        expected = case['baseline_expected'] if MODE == 'baseline' else 'PASS'
        if expected.startswith('PASS'):
            assert row['call'] == 'passed', case['test_id']
        elif expected == 'FAIL_SILENT_ACCEPTANCE':
            assert row['call'] == 'failed' and 'DID NOT RAISE' in row['call_error']
        else:
            assert row['call'] == 'failed' and 'ValueError:' in row['call_error']


def prepare():
    # No CausalPy source, imports, build, install or tests in this metadata phase.
    os.chdir('/work')
    run('compile-docs-closure', ['uv', 'pip', 'compile', '--no-config',
        '--python-version', '3.12.16', '--python-platform', 'x86_64-manylinux_2_28',
        '--generate-hashes', '--no-build', '--default-index', 'https://pypi.org/simple',
        '--output-file', str(R / 'docs-lock.txt'), str(P / 'docs.in')], 90, cwd=Path('/work'))
    raw = (R / 'docs-lock.txt').read_bytes()
    assert len(raw) < 2 * 1024**2 and b'--hash=sha256:' in raw
    assert b'file://' not in raw and b'git+' not in raw
    version = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'graphviz'], text=True)
    assert re.fullmatch(r'[A-Za-z0-9.+:~_-]+', version)
    graphviz = subprocess.check_output(['dot', '-V'], stderr=subprocess.STDOUT, text=True)
    doc = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
           'graphviz_version': version, 'graphviz_output': graphviz,
           'epoch': START, 'base': M['base'], 'joint_resolution': True,
           'no_build': True, 'project_imported': False}
    (R / 'closure.json').write_text(json.dumps(doc, indent=2))


def unit():
    initialize_source()
    guard(2199)
    run('sync', ['uv', 'sync', '--locked', '--extra', 'dev', '--extra', 'docs',
        '--extra', 'test', '--extra', 'lint', '--python', '/usr/local/bin/python',
        '--group', GROUP, '--build-constraint', str(P / 'build-constraints.txt')], 600)
    run('pip-check', ['uv', 'pip', 'check', '--python', str(ROOT / '.venv/bin/python')], 30)
    run('verify-versions', ['uv', 'run', '--no-sync', 'python', str(P / 'verify_versions.py')], 30)
    guard(2199)
    run('typecheck', ['uv', 'run', '--no-sync', 'make', 'typecheck'], 90)
    run('doctests', ['uv', 'run', '--no-sync', 'pytest', '--doctest-modules',
        '-p', 'causalpy.tests.doctest_sampling', '--ignore=causalpy/tests/',
        'causalpy/', '--no-cov'], 150)
    run('extra-tests', ['uv', 'run', '--no-sync', 'pytest',
        'docs/source/.codespell/test_notebook_to_markdown.py', '--no-cov'], 45)
    run('correctness', ['uv', 'run', '--no-sync', 'pytest', '-o', 'addopts=',
        '-m', 'correctness and not nightly', '--cov=causalpy', '--cov-report=',
        '--cov-fail-under=0', '--no-cov-on-fail'], 240,
        extra_env={'PYTENSOR_FLAGS': 'base_compiledir=/work/pytensor-correctness'})
    run('full-test-patch-cov', ['uv', 'run', '--no-sync', 'make', 'test-patch-cov'], 1750,
        extra_env={'PYTEST_ADDOPTS': '-n4 --dist worksteal --cov-append --junitxml=/runtime/full-tests.xml',
                   'DIFF_COVER_COMPARE_BRANCH': M['base']})
    shutil.copyfile(ROOT / 'coverage.xml', R / 'coverage.xml')
    run('prek', ['uv', 'run', '--no-sync', 'prek', 'run', '--all-files'], 240)
    run('exports', ['uv', 'run', '--no-sync', 'make', 'check-exports', 'check-architecture'], 60)
    run('verify-versions-after-exports', ['uv', 'run', '--no-sync', 'python', str(P / 'verify_versions.py')], 30)
    run('pip-check-after-exports', ['uv', 'pip', 'check', '--python', str(ROOT / '.venv/bin/python')], 30)
    focused_regressions()
    run('verify-versions-final', ['uv', 'run', '--no-sync', 'python', str(P / 'verify_versions.py')], 30)
    run('pip-check-final', ['uv', 'pip', 'check', '--python', str(ROOT / '.venv/bin/python')], 30)
    if MODE == 'candidate':
        verify_overlay()
    if MODE == 'candidate' and PY == '3.12' and GROUP == 'pandas3':
        run('build', ['uv', 'build', '--no-build-isolation', '--build-constraint',
                      str(P / 'build-constraints.txt')], 120)
        (R / 'shipping-artifact-hashes.json').write_text(json.dumps({
            f.name: {'bytes': f.stat().st_size, 'sha256': hashlib.sha256(f.read_bytes()).hexdigest()}
            for f in (ROOT / 'dist').iterdir() if f.is_file()}, indent=2))
        wheels = list((ROOT / 'dist').glob('*.whl'))
        assert len(wheels) == 1
        run('install-wheel', ['uv', 'pip', 'install', '--python', str(ROOT / '.venv/bin/python'),
                             '--no-deps', '--reinstall', str(wheels[0])], 45)
        run('installed-smoke', [str(ROOT / '.venv/bin/python'), str(P / 'installed_smoke.py')],
            60, cwd=Path('/work'), extra_env={'PYTHONPATH': ''})


def docs():
    initialize_source()
    closure = json.loads(Path('/prepared/closure.json').read_text())
    raw = Path('/prepared/docs-lock.txt').read_bytes()
    assert closure['base'] == M['base'] and closure['epoch'] == START
    assert hashlib.sha256(raw).hexdigest() == closure['sha256']
    version = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'graphviz'], text=True)
    assert version == closure['graphviz_version']
    run('venv', ['uv', 'venv', '--python', '/usr/local/bin/python', str(ROOT / '.venv')], 30)
    run('docs-dependencies', ['uv', 'pip', 'install', '--python', str(ROOT / '.venv/bin/python'),
        '--require-hashes', '--no-build', '-r', '/prepared/docs-lock.txt'], 480)
    run('install-project', ['uv', 'pip', 'install', '--python', str(ROOT / '.venv/bin/python'),
        '--no-deps', '--no-build-isolation', '-e', str(ROOT)], 60)
    run('pip-check', ['uv', 'pip', 'check', '--python', str(ROOT / '.venv/bin/python')], 30)
    run('freeze', ['uv', 'pip', 'freeze', '--python', str(ROOT / '.venv/bin/python')], 30)
    run('kernel', [str(ROOT / '.venv/bin/python'), '-m', 'ipykernel', 'install',
                  '--user', '--name', 'python3'], 30)
    mode = os.environ['VALIDATION_NOTEBOOK_MODE']
    for name in M['notebooks']:
        args = [str(ROOT / '.venv/bin/python'), str(P / 'notebook_observer.py'),
                '--notebook', 'docs/source/notebooks/' + name]
        if mode == 'native':
            args.append('--full')
        run('notebook-' + name.replace('.ipynb', ''), args, 1900,
            extra_env={'NOTEBOOK_NAME': name, 'NOTEBOOK_MODE': mode})
        assert (R / (name + '-cells.json')).is_file(), 'MISSING_CELL_OBSERVER_EVENTS'
        notebook_log = R / ('notebook-' + name.replace('.ipynb', '') + '.log')
        assert 'UNCHANGED_NOTEBOOK_CODE_AND_SAMPLING_ARGUMENTS' in notebook_log.read_text(), 'MISSING_NOTEBOOK_CODE_PROOF'
    run('html', ['uv', 'run', '--no-sync', 'make', 'html'], 300)
    run('freeze-final', ['uv', 'pip', 'freeze', '--python', str(ROOT / '.venv/bin/python')], 30)
    assert (R / 'freeze-final.log').read_bytes() == (R / 'freeze.log').read_bytes(), 'DOCS_ENVIRONMENT_DRIFT'
    candidates = [f for f in (ROOT / 'docs/_build').rglob('*.html')
                  if 'InversePropensityWeighting' in f.read_text(errors='replace')]
    if MODE == 'candidate':
        assert candidates, 'MISSING_API_HTML'
        assert any('Treatment encoding' in f.read_text() and 'numeric' in f.read_text()
                   for f in candidates), 'NEW_API_DOCS_NOT_RENDERED'
        verify_overlay()
    (R / 'docs-proof.json').write_text(json.dumps({'mode': mode,
        'html_pages': [str(f.relative_to(ROOT)) for f in candidates],
        'closure_sha256': closure['sha256'], 'graphviz_version': version}, indent=2))


RESULT = {'status': 'PREPARING', 'kind': KIND, 'mode': MODE, 'steps': STEPS,
          'runtime_start_epoch': START, 'global_target_epoch': END}
try:
    assert time.time() < END
    if KIND == 'prepare':
        prepare()
    elif KIND == 'unit':
        unit()
    elif KIND == 'docs':
        docs()
    else:
        raise AssertionError('UNKNOWN_KIND')
    RESULT['status'] = 'SUCCESS'
except BaseException as error:
    RESULT.update(status='STOP_' + type(error).__name__, error=str(error))
finally:
    RESULT['steps'] = STEPS
    RESULT['resource'] = snapshot()
    RESULT['ended_at'] = time.time()
    (R / 'result.json').write_text(json.dumps(RESULT, indent=2))
    print('CAUSALPY_I1_RESULT=' + json.dumps(RESULT), flush=True)
assert RESULT['status'] == 'SUCCESS'
