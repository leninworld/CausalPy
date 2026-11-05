"""Inspect actual native report HTML and freeze only scoped final source/output files."""
from pathlib import Path
from html.parser import HTMLParser
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile

ROOT = Path('/work/source')
R = Path('/runtime')
M = json.loads(Path('/packet/manifest.json').read_text())
START = float(os.environ['VALIDATION_START_EPOCH'])
formatted = json.loads(Path('/prepared/formatted-overlay.json').read_text())
assert formatted['base'] == M['base'] and formatted['epoch'] == START
for path, expected in formatted['files'].items():
    if path.endswith('.py') or path.endswith('.html'):
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, 'CODE_CHANGED_AFTER_FORMAT_' + path


class Report(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.done = False
        self.tags = []
        self.attrs = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == 'div' and ('class', 'report-table') in attrs and not self.depth:
            self.depth = 1
            return
        if self.depth:
            self.tags.append(tag)
            self.attrs.extend(attrs)
            if tag == 'div':
                self.depth += 1

    def handle_endtag(self, tag):
        if tag == 'div' and self.depth:
            self.depth -= 1
            if not self.depth:
                self.done = True

    def handle_data(self, data):
        if self.depth:
            self.text.append(data)


class Frames(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reports = []

    def handle_starttag(self, tag, attrs):
        if tag == 'iframe':
            value = dict(attrs).get('srcdoc', '')
            if 'CausalPy Analysis Report' in value:
                self.reports.append(value)


allowed_tags = {'div', 'style', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'br', 'span', 'sub', 'sup', 'caption', 'colgroup', 'col'}
allowed_attrs = {'id', 'class', 'style', 'rowspan', 'colspan', 'scope', 'headers', 'data-quarto-disable-processing', 'data-quarto-bootstrap'}
proof = {}
for name in M['notebooks']:
    path = ROOT / 'docs/source/notebooks' / name
    nb = json.loads(path.read_text())
    codes = [hashlib.sha256(''.join(c['source']).encode()).hexdigest() for c in nb['cells'] if c['cell_type'] == 'code']
    assert codes == M['notebook_code_sha256'][name], 'NOTEBOOK_CODE_OR_SAMPLING_CHANGED'
    assert [hashlib.sha256(''.join(c['source']).encode()).hexdigest() for c in nb['cells']] == M['notebook_cell_sources_sha256'][name]
    if '--verify' in sys.argv:
        continue
    frames = Frames()
    for cell in nb['cells']:
        for output in cell.get('outputs', []):
            value = output.get('data', {}).get('text/html', '')
            frames.feed(''.join(value) if isinstance(value, list) else value)
    assert frames.reports, 'MISSING_REPORT_HTML_' + name
    rows = []
    for number, raw in enumerate(frames.reports):
        table = Report()
        table.feed(raw)
        assert table.done and 'table' in table.tags
        assert any(key == 'class' and 'gt_table' in value.split() for key, value in table.attrs if value is not None)
        assert set(table.tags) <= allowed_tags
        assert {key for key, _ in table.attrs} <= allowed_attrs
        text = ''.join(table.text)
        assert '@import' not in text.lower()
        assert re.search(r'url\(\s*["\x27]?(?:https?:|//|javascript:|vbscript:)', text, re.IGNORECASE) is None
        out = R / ('report-' + name.replace('.ipynb', '') + '-' + str(number) + '.html')
        out.write_text(raw)
        rows.append({'sha256': hashlib.sha256(raw.encode()).hexdigest(), 'tags': sorted(set(table.tags)),
                     'attribute_names': sorted({key for key, _ in table.attrs})})
    proof[name] = rows

# Do not publish unrelated re-encodings of unchanged gallery thumbnails.
changed = subprocess.check_output(['git', '-C', str(ROOT), 'diff', '--name-only'], text=True).splitlines()
restored = []
for path in changed:
    if path.startswith('docs/source/_static/thumbnails/') and path not in M['final_allowed_paths']:
        raw = subprocess.check_output(['git', '-C', str(ROOT), 'show', M['base'] + ':' + path])
        (ROOT / path).write_bytes(raw)
        restored.append(path)
changed = subprocess.check_output(['git', '-C', str(ROOT), 'diff', '--name-only'], text=True).splitlines()
assert set(changed) <= set(M['final_allowed_paths']), changed
if '--verify' in sys.argv:
    expected = json.loads(Path('/final/final-overlay.json').read_text())
    assert expected['base'] == M['base'] and expected['epoch'] == START
    for path, digest in expected['files'].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
    print('FINAL_SOURCE_AND_NATIVE_OUTPUT_BYTES_UNCHANGED')
else:
    files = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in sorted(set(changed) | set(formatted['files']))}
    archive = R / 'final-overlay.tar'
    with tarfile.open(archive, 'w') as tar:
        for path in files:
            tar.add(ROOT / path, arcname=path, recursive=False)
    assert archive.stat().st_size <= 32 * 1024**2
    (R / 'final-overlay.json').write_text(json.dumps({'base': M['base'], 'epoch': START, 'files': files,
        'tar_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'reports': proof,
        'restored_unrelated_thumbnails': restored, 'python_and_template_identical_to_formatted_inputs': True,
        'all_notebook_code_identical': True}, indent=2))
    print('FINAL_NATIVE_OUTPUTS_FROZEN_WITH_IDENTICAL_TESTED_CODE')
