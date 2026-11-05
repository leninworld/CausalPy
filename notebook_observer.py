"""Resource-only notebook observer; source, arguments and sampling are unchanged."""
from pathlib import Path
import hashlib
import json
import os
import runpy
import sys
import time

import nbclient.client

P = Path('/packet')
R = Path('/runtime')
M = json.loads((P / 'manifest.json').read_text())
END = float(os.environ['VALIDATION_END_EPOCH'])
NAME = os.environ['NOTEBOOK_NAME']
MODE = os.environ['NOTEBOOK_MODE']
HISTORY = M['notebooks'][NAME]
ORIGINAL = nbclient.client.NotebookClient.execute_cell
RATIOS = [3.0]
EVENTS = []
SOURCE = Path('docs/source/notebooks') / NAME
BEFORE = json.loads(SOURCE.read_text())
CODES = [hashlib.sha256(''.join(c['source']).encode()).hexdigest()
         for c in BEFORE['cells'] if c['cell_type'] == 'code']


def checkpoint(index, kind, elapsed=0):
    cgroup = Path('/sys/fs/cgroup')
    current = int((cgroup / 'memory.current').read_text())
    stat = dict(line.split() for line in (cgroup / 'memory.stat').read_text().splitlines())
    working = max(0, current - int(stat.get('inactive_file', 0)))
    limit = int((cgroup / 'memory.max').read_text())
    original_index = index - (1 if MODE == 'mock' else 0)
    old = HISTORY.get(str(original_index))
    if kind == 'after' and old is not None and old >= 10:
        RATIOS.append(max(1.5, elapsed / old))
    unknown_price = max((v for v in HISTORY.values() if v is not None), default=30.0)
    remaining = sum((v if v is not None else unknown_price) for k, v in HISTORY.items() if int(k) > original_index)
    # Unknown committed timings use an explicit conservative proxy, not zero; references are not fresh native measurements.
    keys = list(M['notebooks'])
    later = keys[keys.index(NAME) + 1:]
    remaining += sum(sum(v if v is not None else max((x for x in M['notebooks'][k].values() if x is not None), default=30.0) for v in M['notebooks'][k].values()) for k in later)
    forecast = max(RATIOS) * remaining + 300 + 30
    projected = working * 1.1 + 2.5 * 1024**3
    event = {'at': time.time(), 'notebook': NAME, 'mode': MODE, 'index': index,
             'event': kind, 'elapsed': elapsed, 'working': working,
             'sampled_projection': projected, 'peak': int((cgroup / 'memory.peak').read_text()),
             'forecast_seconds': forecast, 'remaining_seconds': END - time.time()}
    EVENTS.append(event)
    (R / (NAME + '-cells.json')).write_text(json.dumps(EVENTS, indent=2))
    assert working < M['work_pressure_gib'] * 1024**3, 'NOTEBOOK_MEMORY_STOP'
    assert projected < limit * .9375, 'NOTEBOOK_MEMORY_PROJECTION_STOP'
    assert time.time() + forecast < END, 'NOTEBOOK_REMAINING_FORECAST_STOP'


def observed(self, cell, cell_index, *args, **kwargs):
    checkpoint(cell_index, 'before')
    start = time.monotonic()
    try:
        return ORIGINAL(self, cell, cell_index, *args, **kwargs)
    finally:
        checkpoint(cell_index, 'after', time.monotonic() - start)


nbclient.client.NotebookClient.execute_cell = observed
sys.argv[0] = 'scripts/run_notebooks/runner.py'
code = 0
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
except SystemExit as stop:
    code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
assert EVENTS and any(x['event'] == 'before' for x in EVENTS), 'CELL_OBSERVER_NOT_CALLED'
AFTER = json.loads(SOURCE.read_text())
assert CODES == [hashlib.sha256(''.join(c['source']).encode()).hexdigest()
                 for c in AFTER['cells'] if c['cell_type'] == 'code']
print('UNCHANGED_NOTEBOOK_CODE_AND_SAMPLING_ARGUMENTS', NAME, MODE)

sys.exit(code)
