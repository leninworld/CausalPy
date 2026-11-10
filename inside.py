"""Disabled CausalPy baseline adapter. Run only in the exact reviewed hosted Docker packet."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

from source_identity import verify
from validation_runtime import Runtime, cgroup_memory, docker_only, require

P = Path('/packet')
W = Path('/work')
M = json.loads((P / 'manifest.json').read_text())
PHASE = os.environ['CAUSAL_PHASE']
R = Path('/runtime') / PHASE
R.mkdir()
DEADLINE = float(os.environ['VALIDATION_DEADLINE_EPOCH'])
RT = Runtime(DEADLINE, R)
STEPS = []
LOCAL_END = None
PRISTINE = W / 'pristine'
NATIVE = W / 'native'
TARGET = M['target_notebook']


def run(label, args, seconds, cwd=W):
    if LOCAL_END is not None:
        seconds = min(seconds, LOCAL_END - time.time())
        require(seconds > 0, "Local phase budget exhausted")
    record = RT.run(label, args, cwd, seconds)
    STEPS.append(dict(label=label, **record))
    return (R / (label + '.log')).read_bytes()


def clean(label):
    require(run(label, ['git', '-C', str(PRISTINE), 'status', '--porcelain', '--untracked-files=no'], 15) == b'',
            'Pristine tracked source changed: ' + label)
    run(label + '-full-status', ['git', '-C', str(PRISTINE), 'status', '--porcelain', '--ignored'], 15)


def admission(label):
    host = json.loads(Path('/capacity/host.json').read_text())
    require(0 <= time.time() - host['at'] < 30, 'Stale actual host capacity')
    measured = RT.memory.snapshot()['sampled_nonreclaimable_peak']
    required = max(8 * 1024**3, measured + 2 * 1024**3)
    actual = cgroup_memory()
    require(required <= actual['limit'] and host['available'] >= required + 1024**3,
            'Full phase memory admission failed')
    (R / (label + '-admission.json')).write_text(json.dumps(
        dict(host=host, cgroup=actual, measured_peak=measured, required=required), indent=2))


def notebook_evidence(before):
    after = json.loads((NATIVE / TARGET).read_text())
    sources = lambda nb: [(c['cell_type'], ''.join(c.get('source', []))) for c in nb['cells']]
    require(sources(before) == sources(after), 'Baseline notebook code or prose changed')
    require(not any(o.get('output_type') == 'error' for c in after['cells']
                    for o in c.get('outputs', [])), 'Native notebook error output')
    outputs = [{'cell': i, 'execution_count': c.get('execution_count'),
                'metadata': c.get('metadata', {}), 'outputs': c.get('outputs', [])}
               for i, c in enumerate(after['cells']) if c['cell_type'] == 'code']
    require('r_hat' in json.dumps(outputs) and 'ess_bulk' in json.dumps(outputs),
            'Native sampler diagnostics not retained')
    (R / 'native-notebook.ipynb').write_bytes((NATIVE / TARGET).read_bytes())
    (R / 'native-cell-evidence.json').write_text(json.dumps(outputs, indent=2))
    (R / 'native-output-drift.json').write_text(json.dumps({
        'source_cells_unchanged': True,
        'before_sha256': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
        'after_sha256': hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest(),
        'qualification': 'Fresh native baseline sampling; diagnostics retained, not automatically declared converged.'
    }, indent=2))


def validate():
    global LOCAL_END
    verify('/source', M, run, 'source')
    require(not PRISTINE.exists() and not NATIVE.exists(), 'Source copies already exist')
    shutil.copytree('/source', PRISTINE)
    shutil.copytree('/source', NATIVE)
    os.environ.update(HOME=str(W / 'home'), UV_CACHE_DIR=str(W / 'uv-cache'),
                      PREK_HOME=str(W / 'prek-cache'), MPLCONFIGDIR=str(W / 'matplotlib'),
                      PYTENSOR_FLAGS='base_compiledir=/work/pytensor',
                      UV_PYTHON_DOWNLOADS='never', UV_HTTP_RETRIES='0')
    Path(os.environ['HOME']).mkdir(exist_ok=True)
    LOCAL_END = min(DEADLINE, time.time() + M['phase_caps_seconds']['environment'])
    run('uv-version', ['uv', '--version'], 10)
    run('venv', ['uv', 'venv', '--python', sys.executable], 30, PRISTINE)
    run('docs-dependencies', ['uv', 'pip', 'install', '-r', 'docs/requirements.txt'], 360, PRISTINE)
    run('project-test-docs', ['uv', 'pip', 'install', '-e', '.[test,docs]'], 240, PRISTINE)
    run('resolved-closure', ['uv', 'pip', 'freeze'], 20, PRISTINE)
    run('dependency-check', ['uv', 'pip', 'check'], 30, PRISTINE)
    # prek is absent from test/docs extras. Keep the separate tool out of the scientific environment.
    run('prek-venv', ['uv', 'venv', '--python', sys.executable, str(W / 'tools')], 30)
    run('prek-tool', ['uv', 'pip', 'install', '--python', str(W / 'tools/bin/python'),
                      'prek==0.5.7'], 60)
    run('prek-version', [str(W / 'tools/bin/prek'), '--version'], 10)
    LOCAL_END = None  # Only the local environment budget ends; original workflow deadline never changes.
    (NATIVE / '.venv').symlink_to(PRISTINE / '.venv', target_is_directory=True)
    os.environ['PATH'] = str(PRISTINE / '.venv/bin') + ':' + str(W / 'tools/bin') + ':' + os.environ['PATH']
    code = "import importlib,json,sys,pathlib; import causalpy; assert pathlib.Path(causalpy.__file__).resolve().is_relative_to(pathlib.Path('/work/pristine/causalpy')), 'Wrong installed CausalPy source'; names=['causalpy','pymc','pytensor','pymc_extras','arviz','maketables','papermill','nbclient','sphinx']; print(json.dumps({'python':sys.version,'modules':{n:{'path':getattr(importlib.import_module(n),'__file__',None),'version':getattr(importlib.import_module(n),'__version__',None)} for n in names}},indent=2))"
    run('actual-imports', ['uv', 'run', '--no-sync', 'python', '-c', code], 60, PRISTINE)
    clean('after-install-clean')
    before = json.loads((NATIVE / TARGET).read_text())
    admission('native')
    run('native-full', ['uv', 'run', '--no-sync', 'python', 'scripts/run_notebooks/runner.py',
                       '--full', '--notebook', TARGET], 360, NATIVE)
    delta = run('native-delta', ['git', '-C', str(NATIVE), 'status', '--porcelain', '-z', '--untracked-files=no'], 15)
    changed = [part[3:].decode() for part in delta.split(b'\0') if part]
    require(set(changed) <= {TARGET}, 'Native execution changed other source')
    notebook_evidence(before)
    run('upstream-mock', ['uv', 'run', '--no-sync', 'python', 'scripts/run_notebooks/runner.py',
                         '--notebook', TARGET], 180, PRISTINE)
    clean('after-mock-clean')
    run('whole-prek', ['prek', 'run', '--all-files'], 600, PRISTINE)
    clean('after-prek-clean')
    run('notebook-spelling-conversion', ['uv', 'run', '--no-sync', 'python',
        './docs/source/.codespell/notebook_to_markdown.py', '--tempdir', 'tmp_markdown'], 60, PRISTINE)
    run('whole-codespell', ['uv', 'run', '--no-sync', 'codespell'], 60, PRISTINE)
    clean('after-codespell-clean')
    admission('sphinx')
    run('native-make-html', ['uv', 'run', '--no-sync', 'make', 'html'], 360, PRISTINE)
    clean('after-sphinx-clean')
    require((PRISTINE / 'docs/_build/notebooks' / (Path(TARGET).stem + '.html')).is_file(),
            'Target Sphinx page missing')
    verify('/source', M, run, 'final-source')


def render(fixture=False):
    global LOCAL_END
    LOCAL_END = min(DEADLINE, time.time() + (60 if fixture else M["phase_caps_seconds"]["render"]))
    root = W / ('browser-fixture' if fixture else 'pristine/docs/_build')
    if fixture:
        root.mkdir()
        page = root / 'fixture.html'
        page.write_text('<!doctype html><title>CausalPy render fixture</title><h1>Render fixture</h1><p>Actual offline Chromium.</p>')
    else:
        page = root / 'notebooks' / (Path(TARGET).stem + '.html')
    run('browser-version', ['chromium', '--version'], 10)
    run('system-closure', ['dpkg-query', '-W'], 15)
    run('uv-native-version', ['uv', '--version'], 10)
    run('compiler-version', ['g++', '--version'], 10)
    run('poppler-version', ['pdftoppm', '-v'], 10)
    args = ['chromium', '--headless', '--no-sandbox', '--disable-gpu',
            '--disable-dev-shm-usage', '--disable-background-networking',
            '--user-data-dir=' + str(W / ('chrome-' + PHASE)),
            '--no-pdf-header-footer', '--print-to-pdf=' + str(R / 'rendered.pdf'), page.as_uri()]
    run('offline-browser-print', args, 90)
    run('offline-browser-screen', ['chromium', '--headless', '--no-sandbox', '--disable-gpu',
        '--disable-dev-shm-usage', '--disable-background-networking',
        '--user-data-dir=' + str(W / ('screen-' + PHASE)),
        '--window-size=1440,1600', '--screenshot=' + str(R / 'screen.png'), page.as_uri()], 60)
    require((R / 'screen.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), 'No real browser viewport image')
    require((R / 'rendered.pdf').read_bytes().startswith(b'%PDF-'), 'Browser did not produce PDF')
    run('rendered-text', ['pdftotext', str(R / 'rendered.pdf'), str(R / 'rendered.txt')], 15)
    text = ' '.join((R / 'rendered.txt').read_text().split())
    if fixture:
        expected = 'Actual offline Chromium.'
    else:
        nb = json.loads((PRISTINE / TARGET).read_text())
        expected = next(' '.join(line.lstrip('#').split()) for c in nb['cells'] if c['cell_type'] == 'markdown'
                        for line in ''.join(c.get('source', [])).splitlines() if line.startswith('# '))
    require(expected in text, 'Expected actual page text missing from browser render')
    run('rendered-pages', ['pdftoppm', '-scale-to', '1600', '-png', str(R / 'rendered.pdf'),
                         str(R / 'page')], 60)
    require(any(R.glob('page-*.png')), 'No actual rendered-page images')
    (R / 'render-proof.json').write_text(json.dumps({
        'source': str(page), 'derived_expected_heading': expected, 'network': 'none (enforced by host container network)',
        'actual_browser_render': True,
        'views': 'Full print-media PDF/pages plus actual top screen-media viewport; inspect pixels, do not claim full scrolling screen capture', 'visual_inspection': 'PENDING Codex pixel inspection after SHA-verified export',
        'not_a_final_validation_pass': True}, indent=2))


def main():
    docker_only()
    os.environ['HOME'] = str(W / 'home')
    (W / 'home').mkdir(exist_ok=True)
    require(M['execution_enabled'] is True, 'Disabled packet')
    result = {'phase': PHASE, 'passed': False, 'full_validation_passed': False}
    try:
        if PHASE == 'fixture':
            from fixtures import main as fixtures
            fixtures(RT, run, W, R)
        elif PHASE == 'browser-fixture':
            render(True)
        elif PHASE == 'validate':
            validate()
        elif PHASE == 'render':
            render()
        else:
            raise RuntimeError('Unexpected phase')
        result['passed'] = True
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
    finally:
        result.update(steps=STEPS, memory=RT.memory.snapshot(), cgroup=cgroup_memory())
        (R / 'result.json').write_text(json.dumps(result, indent=2))
    require(result['passed'], 'Baseline phase failed; no automatic retry')


if __name__ == '__main__':
    main()
