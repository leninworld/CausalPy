"""Passive focused-test outcomes; does not change collection or execution."""
import json
import os
from pathlib import Path

ROWS = {}


def pytest_runtest_logreport(report):
    row = ROWS.setdefault(report.nodeid, {})
    row[report.when] = report.outcome
    if report.failed:
        row[report.when + '_error'] = str(report.longrepr)


def pytest_sessionfinish(session, exitstatus):
    Path(os.environ['WITNESS_PATH']).write_text(json.dumps({
        'exitstatus': int(exitstatus), 'reports': ROWS}, indent=2))
