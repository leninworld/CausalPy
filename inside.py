"""Proposed I1 container controller. Execution requires exact packet approval."""
from pathlib import Path
import ast
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
import threading
import tarfile

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
ACTIVE_HASHES = {}
BASE_ENV = dict(os.environ, MPLBACKEND='Agg', PYTHONPATH='/packet',
                PYTEST_ADDOPTS='', UV_PYTHON_DOWNLOADS='never',
                PYTENSOR_FLAGS='base_compiledir=/work/pytensor-main',
                MPLCONFIGDIR='/work/matplotlib', COVERAGE_FILE='/runtime/coverage-data')


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
    step_samples = []
    sampled = threading.Event()
    def sample_loop():
        while not sampled.wait(.5):
            step_samples.append(snapshot())
    sampler = threading.Thread(target=sample_loop, daemon=True)
    sampler.start()
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
            sampled.set()
            sampler.join(timeout=2)
            STEPS.append({'max_sampled_current_bytes': max((s['current'] for s in step_samples), default=0),
                          'max_sampled_working_bytes': max((s['working'] for s in step_samples), default=0),
                          'samples': len(step_samples), 'name': name, 'args': args, 'exit': proc.returncode,
                          'seconds': time.monotonic() - start, 'error': problem,
                          'resource': snapshot()})
    assert proc.returncode == expected, 'STEP_EXIT_' + name


def verify_overlay(root=ROOT):
    for path, expected in ACTIVE_HASHES.items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == expected


def apply_frozen(folder, stem, target_root=ROOT):
    global ACTIVE_HASHES
    meta = json.loads((folder / (stem + '.json')).read_text())
    archive = folder / (stem + '.tar')
    assert meta['base'] == M['base'] and meta['epoch'] == START
    if stem == 'formatted-overlay':
        assert set(meta['files']) == set(M['overlay_sha256'])
    elif stem == 'final-overlay':
        assert set(M['overlay_sha256']) <= set(meta['files']) <= set(M['final_allowed_paths'])
        prepared = json.loads(Path('/prepared/formatted-overlay.json').read_text())
        for path, digest in prepared['files'].items():
            if path.endswith('.py') or path.endswith('.html'):
                assert meta['files'][path] == digest, 'FINAL_CODE_DIFFERS_FROM_UNIT_INPUTS'
    else:
        assert stem == 'synthetic' and target_root != ROOT
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == meta['tar_sha256']
    assert archive.stat().st_size <= 32 * 1024**2
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        assert {x.name for x in members} == set(meta['files'])
        for member in members:
            assert member.isfile() and member.name in M['final_allowed_paths']
            target = target_root / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = tar.extractfile(member)
            assert stream is not None
            target.write_bytes(stream.read())
    ACTIVE_HASHES = meta['files']
    for path, expected in ACTIVE_HASHES.items():
        assert hashlib.sha256((target_root / path).read_bytes()).hexdigest() == expected


def initialize_source():
    global ACTIVE_HASHES
    shutil.copytree('/source', ROOT)
    assert subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip() == M['base']
    for path, expected in M['baseline_sha256'].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected
    assert hashlib.sha256((ROOT / 'uv.lock').read_bytes()).hexdigest() == M['uv_lock_sha256']
    if KIND == 'prepare':
        ACTIVE_HASHES = M['overlay_sha256']
        for path in ACTIVE_HASHES:
            target = ROOT / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(P / 'overlay' / path, target)
    elif KIND in ('unit', 'rich', 'docs'):
        apply_frozen(Path('/prepared'), 'formatted-overlay')
    else:
        apply_frozen(Path('/final'), 'final-overlay')
    run('intent-test', ['git', 'add', '-N', 'causalpy/tests/test_report_tables.py'], 10)
    verify_overlay()
    for name, expected in M['notebook_cell_sources_sha256'].items():
        nb = json.loads((ROOT / 'docs/source/notebooks' / name).read_text())
        assert [hashlib.sha256(''.join(c['source']).encode()).hexdigest() for c in nb['cells']] == expected


def syntax_shape(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:]
    return ast.dump(tree, include_attributes=False)


def freeze_overlay(stem, paths):
    global ACTIVE_HASHES
    ACTIVE_HASHES = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    target = R / (stem + '.tar')
    with tarfile.open(target, 'w') as tar:
        for path in sorted(ACTIVE_HASHES):
            tar.add(ROOT / path, arcname=path, recursive=False)
    assert target.stat().st_size <= 32 * 1024**2
    (R / (stem + '.json')).write_text(json.dumps({'base': M['base'], 'epoch': START, 'files': ACTIVE_HASHES,
        'tar_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}, indent=2))


def tool_fixture():
    fixture = Path('/work/tiny')
    fixture.mkdir()
    (fixture / 'input').mkdir()
    (fixture / 'input/item').write_text('same')
    shutil.copytree(fixture / 'input', fixture / 'copy')
    assert (fixture / 'copy/item').read_text() == 'same'
    with tarfile.open(fixture / 'fixture.tar', 'w') as tar:
        tar.add(fixture / 'input/item', arcname='nested/item')
    with tarfile.open(fixture / 'fixture.tar') as tar:
        member = tar.getmember('nested/item')
        assert member.isfile()
        target = fixture / member.name
        target.parent.mkdir()
        target.write_bytes(tar.extractfile(member).read())
    assert target.read_text() == 'same' and 'same' != 'different'
    overlay_name = 'causalpy/steps/report.py'
    overlay_tar = fixture / 'synthetic.tar'
    with tarfile.open(overlay_tar, 'w') as tar:
        tar.add(fixture / 'input/item', arcname=overlay_name)
    (fixture / 'synthetic.json').write_text(json.dumps({'base': M['base'], 'epoch': START,
        'tar_sha256': hashlib.sha256(overlay_tar.read_bytes()).hexdigest(),
        'files': {overlay_name: hashlib.sha256(b'same').hexdigest()}}))
    apply_frozen(fixture, 'synthetic', fixture / 'overlay-target')
    assert (fixture / 'overlay-target' / overlay_name).read_text() == 'same'
    expected = {'sync': ['--locked', '--extra', '--group', '--python'],
                'pip compile': ['--no-config', '--python-version', '--python-platform', '--generate-hashes', '--no-build', '--default-index', '--output-file'],
                'pip install': ['--python', '--require-hashes', '--no-build', '--no-deps', '--no-build-isolation', '--reinstall'],
                'build': ['--no-build-isolation'], 'venv': ['--python'], 'tool run': ['--from']}
    for command, flags in expected.items():
        result = subprocess.run(['uv', *command.split(), '--help'], capture_output=True, check=True, timeout=10)
        assert all(flag.encode() in result.stdout for flag in flags), (command, flags)
        (R / ('uv-' + command.replace(' ', '-') + '-help.txt')).write_bytes(result.stdout)
    (R / 'tiny-fixture.json').write_text(json.dumps({'copy': True, 'nested_extract': True, 'comparison': True, 'uv_flags': True}))


def verify_focused_junit(path, present):
    import xml.etree.ElementTree as ET
    cases = [x for x in ET.parse(path).getroot().iter('testcase') if x.get('classname', '').endswith('test_report_tables')]
    assert len(cases) == M['focused_cases'], len(cases)
    assert sum(x.find('skipped') is not None for x in cases) == (1 if present else 9)
    assert not any(x.find('failure') is not None or x.find('error') is not None for x in cases)


def sync_locked(extras):
    initialize_source()
    args = ['uv', 'sync', '--locked', '--python', '/usr/local/bin/python', '--group', GROUP]
    for extra in extras:
        args += ['--extra', extra]
    run('sync', args, 600)
    run('pip-check', ['uv', 'pip', 'check', '--python', str(ROOT / '.venv/bin/python')], 30)
    run('verify-versions', ['uv', 'run', '--no-sync', 'python', str(P / 'verify_versions.py')], 30)


def prepare():
    sync_locked(['dev', 'docs', 'test', 'lint'])
    paths = ['causalpy/steps/report.py', 'causalpy/tests/test_report_tables.py']
    before = {path: syntax_shape(ROOT / path) for path in paths}
    run('format-version', ['uv', 'tool', 'run', '--from', 'ruff==0.17.3', 'ruff', '--version'], 60)
    run('format', ['uv', 'tool', 'run', '--from', 'ruff==0.17.3', 'ruff', 'format', *paths], 30)
    assert before == {path: syntax_shape(ROOT / path) for path in paths}, 'FORMAT_CHANGED_EXECUTABLE_AST'
    run('lint-overlay', ['uv', 'tool', 'run', '--from', 'ruff==0.17.3', 'ruff', 'check', *paths], 30)
    freeze_overlay('formatted-overlay', M['overlay_sha256'])
    run('compile-docs-closure', ['uv', 'pip', 'compile', '--no-config',
        '--python-version', '3.12.16', '--python-platform', 'x86_64-manylinux_2_28',
        '--generate-hashes', '--no-build', '--default-index', 'https://pypi.org/simple',
        '--output-file', str(R / 'docs-lock.txt'), str(P / 'docs.in')], 90, cwd=Path('/work'))
    raw = (R / 'docs-lock.txt').read_bytes()
    assert len(raw) < 2 * 1024**2 and b'--hash=sha256:' in raw
    assert b'file://' not in raw and b'git+' not in raw
    version = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'graphviz'], text=True)
    assert re.fullmatch(r'[A-Za-z0-9.+:~_-]+', version)
    (R / 'closure.json').write_text(json.dumps({'sha256': hashlib.sha256(raw).hexdigest(),
        'epoch': START, 'base': M['base'], 'graphviz_version': version,
        'graphviz_sha256': hashlib.sha256(Path(shutil.which('dot')).read_bytes()).hexdigest(),
        'joint_resolution': True,
        'code_execution': 'Only formatting/editable build; no models or tests'}, indent=2))


def unit():
    guard(2199)
    sync_locked(['test', 'dev'])
    guard(2199)
    run('typecheck', ['uv', 'run', '--no-sync', 'make', 'typecheck'], 90)
    run('doctests', ['uv', 'run', '--no-sync', 'pytest', '--doctest-modules',
        '-p', 'causalpy.tests.doctest_sampling', '--ignore=causalpy/tests/', 'causalpy/', '--no-cov'], 150)
    run('extra-tests', ['uv', 'run', '--no-sync', 'pytest',
        'docs/source/.codespell/test_notebook_to_markdown.py', '--no-cov'], 45)
    run('correctness', ['uv', 'run', '--no-sync', 'pytest', '-o', 'addopts=',
        '-m', 'correctness and not nightly', '--cov=causalpy', '--cov-report=',
        '--cov-fail-under=0', '--no-cov-on-fail'], 240,
        extra_env={'PYTENSOR_FLAGS': 'base_compiledir=/work/pytensor-correctness'})
    run('full-tests', ['uv', 'run', '--no-sync', 'pytest', '-n', '4', '--dist', 'worksteal',
        '--cov-append', '--cov-report=xml:/runtime/unit-coverage.xml', '--no-cov-on-fail',
        '--junitxml=/runtime/full-tests.xml'], 1750)
    verify_focused_junit(R / 'full-tests.xml', False)
    run('codespell-notebooks', ['uv', 'run', '--no-sync', 'python',
        'docs/source/.codespell/notebook_to_markdown.py', '--tempdir', 'tmp_markdown'], 30)
    run('codespell', ['uv', 'run', '--no-sync', 'codespell'], 30)
    verify_overlay()
    run('versions-final', ['uv', 'run', '--no-sync', 'python', str(P / 'verify_versions.py')], 30)


def rich():
    sync_locked(['test', 'dev', 'docs', 'lint'])
    run('typecheck-present', ['uv', 'run', '--no-sync', 'make', 'typecheck'], 90)
    run('report-tests-present', ['uv', 'run', '--no-sync', 'pytest',
        'causalpy/tests/test_report_tables.py', 'causalpy/tests/test_generate_report.py',
        'causalpy/tests/test_maketables_plugin.py', '--cov-report=xml:/runtime/rich-coverage.xml',
        '--cov-fail-under=0', '--no-cov-on-fail', '--junitxml=/runtime/report-tests.xml'], 240)
    verify_focused_junit(R / 'report-tests.xml', True)
    run('exports', ['uv', 'run', '--no-sync', 'make', 'check-exports', 'check-architecture'], 60)
    verify_overlay()


def docs():
    initialize_source()
    closure = json.loads(Path('/prepared/closure.json').read_text())
    raw = Path('/prepared/docs-lock.txt').read_bytes()
    assert closure['base'] == M['base'] and closure['epoch'] == START
    assert hashlib.sha256(raw).hexdigest() == closure['sha256']
    assert subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'graphviz'], text=True) == closure['graphviz_version']
    assert hashlib.sha256(Path(shutil.which('dot')).read_bytes()).hexdigest() == closure['graphviz_sha256']
    run('dpkg-inventory', ['dpkg-query', '-W'], 30)
    run('venv', ['uv', 'venv', '--python', '/usr/local/bin/python', str(ROOT / '.venv')], 30)
    run('docs-dependencies', ['uv', 'pip', 'install', '--python', str(ROOT / '.venv/bin/python'),
        '--require-hashes', '--no-build', '-r', '/prepared/docs-lock.txt'], 480)
    run('install-project', ['uv', 'pip', 'install', '--python', str(ROOT / '.venv/bin/python'),
        '--no-deps', '--no-build-isolation', '-e', str(ROOT)], 60)
    run('pip-check', ['uv', 'pip', 'check', '--python', str(ROOT / '.venv/bin/python')], 30)
    run('freeze', ['uv', 'pip', 'freeze', '--python', str(ROOT / '.venv/bin/python')], 30)
    run('backend-versions', [str(ROOT / '.venv/bin/python'), '-c',
        "import importlib.metadata as m,json; print(json.dumps({x:m.version(x) for x in ['maketables','great-tables','pymc','pytensor','pandas','numpy','nbclient','papermill']}))"], 30)
    run('kernel', [str(ROOT / '.venv/bin/python'), '-m', 'ipykernel', 'install', '--user', '--name', 'python3'], 30)
    # Each actual docs environment must satisfy the same rich-HTML contract even if its unlocked versions differ.
    run('docs-backend-contract', [str(ROOT / '.venv/bin/python'), '-m', 'pytest', '-o', 'addopts=',
        '-q', 'causalpy/tests/test_report_tables.py', '--junitxml=/runtime/backend-contract.xml'], 180)
    verify_focused_junit(R / 'backend-contract.xml', True)
    mode = os.environ['VALIDATION_NOTEBOOK_MODE']
    for name in M['notebooks']:
        args = [str(ROOT / '.venv/bin/python'), str(P / 'notebook_observer.py'),
                '--notebook', 'docs/source/notebooks/' + name]
        if mode == 'native':
            args.append('--full')
        run('notebook-' + name.replace('.ipynb', ''), args, 1900,
            extra_env={'NOTEBOOK_NAME': name, 'NOTEBOOK_MODE': mode})
        assert (R / (name + '-cells.json')).is_file()
        assert 'UNCHANGED_NOTEBOOK_CODE_AND_SAMPLING_ARGUMENTS' in (R / ('notebook-' + name.replace('.ipynb', '') + '.log')).read_text()
    # Static inventory and unchanged source show the ordinary gallery target will execute no unrelated notebook.
    for row in M['gallery_inventory']:
        nb = json.loads((ROOT / 'docs/source/notebooks' / (row['stem'] + '.ipynb')).read_text())
        assert any(c.get('outputs') for c in nb['cells'] if c['cell_type'] == 'code'), 'UNPRICED_GALLERY_EXECUTION'
    if mode == 'native':
        run('html', ['uv', 'run', '--no-sync', 'make', 'html'], 300)
        run('prek', ['uv', 'run', '--no-sync', 'prek', 'run', '--all-files'], 240)
        # No rerun after an autofix: nonzero prek exit stops the attempt for actual-result review.
        run('finalize-overlay', [str(ROOT / '.venv/bin/python'), str(P / 'finalize_overlay.py')], 60)
    else:
        verify_overlay()  # Official mock execution discards output and leaves formatted inputs unchanged.
    run('freeze-final', ['uv', 'pip', 'freeze', '--python', str(ROOT / '.venv/bin/python')], 30)
    assert (R / 'freeze-final.log').read_bytes() == (R / 'freeze.log').read_bytes()


def coverage_gate():
    sync_locked(['test', 'dev', 'docs', 'lint'])
    inputs = list(Path('/coverage-inputs').rglob('coverage-data'))
    assert len(inputs) == 4, [str(x) for x in inputs]
    run('combine', ['uv', 'run', '--no-sync', 'coverage', 'combine', '--keep', *map(str, inputs)], 60)
    run('coverage-xml', ['uv', 'run', '--no-sync', 'coverage', 'xml', '-o', '/runtime/combined-coverage.xml'], 30)
    run('diff-cover', ['uv', 'run', '--no-sync', 'diff-cover', '/runtime/combined-coverage.xml',
        '--compare-branch', M['base'], '--fail-under', '96', '--exclude', str(ROOT / 'causalpy/tests/*'),
        '--json-report', '/runtime/patch-coverage.json'], 30)
    run('final-notebook-conversion-tests', ['uv', 'run', '--no-sync', 'pytest',
        'docs/source/.codespell/test_notebook_to_markdown.py', '--no-cov'], 45)
    run('final-codespell-notebooks', ['uv', 'run', '--no-sync', 'python',
        'docs/source/.codespell/notebook_to_markdown.py', '--tempdir', 'tmp_markdown'], 30)
    run('final-codespell', ['uv', 'run', '--no-sync', 'codespell'], 30)
    run('verify-final-overlay', [str(ROOT / '.venv/bin/python'), str(P / 'finalize_overlay.py'), '--verify'], 60)
    run('final-doc-file-tests', ['uv', 'run', '--no-sync', 'pytest', '-o', 'addopts=',
        'causalpy/tests/test_notebook_validation.py', 'causalpy/tests/test_generate_gallery.py',
        'causalpy/tests/test_bayesian_sc_comparison_note.py'], 90)
    run('build', ['uv', 'build'], 120)
    wheels = list((ROOT / 'dist').glob('*.whl'))
    assert len(wheels) == 1
    (R / 'shipping-artifact-hashes.json').write_text(json.dumps({
        f.name: {'bytes': f.stat().st_size, 'sha256': hashlib.sha256(f.read_bytes()).hexdigest()}
        for f in (ROOT / 'dist').iterdir() if f.is_file()}, indent=2))
    run('install-wheel', ['uv', 'pip', 'install', '--python', str(ROOT / '.venv/bin/python'),
        '--no-deps', '--reinstall', str(wheels[0])], 45)
    run('installed-smoke', [str(ROOT / '.venv/bin/python'), str(P / 'installed_smoke.py')],
        60, cwd=Path('/work'), extra_env={'PYTHONPATH': ''})
    verify_overlay()


RESULT = {'status': 'PREPARING', 'kind': KIND, 'mode': MODE, 'steps': STEPS,
          'runtime_start_epoch': START, 'global_target_epoch': END}
try:
    assert time.time() < END
    tool_fixture()
    if KIND == 'prepare':
        prepare()
    elif KIND == 'unit':
        unit()
    elif KIND == 'docs':
        docs()
    elif KIND == 'rich':
        rich()
    elif KIND == 'coverage':
        coverage_gate()
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
