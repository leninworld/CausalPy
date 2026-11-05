"""Verify the declared locked CI matrix inside the owned container."""
import importlib.metadata as md
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys

expected = {'numpy': '2.5.4', 'patsy': '1.0.3', 'pymc': '6.3.2',
            'pytensor': '3.3.2', 'mypy': '2.4.0',
            'pandas': '2.3.3' if os.environ['VALIDATION_PANDAS'] == 'pandas2' else '3.0.6'}
actual = {name: md.version(name) for name in expected}
assert actual == expected, (actual, expected)
assert platform.python_version().startswith(os.environ['VALIDATION_PYTHON'] + '.')
assert platform.machine() == 'x86_64'
assert shutil.which('g++')
import pytensor
assert pytensor.config.cxx
print(json.dumps({'versions': actual, 'python': sys.version,
                  'cxx': pytensor.config.cxx, 'blas': pytensor.config.blas__ldflags,
                  'compiler': subprocess.check_output(['g++', '--version'], text=True)}))

kind = os.environ["VALIDATION_KIND"]
present = importlib.util.find_spec("maketables") is not None
assert present == (kind != "unit"), (kind, present)
if present:
    assert md.version("maketables") == "0.2.0"
    assert md.version("great-tables") == "0.24.0"
print("OPTIONAL_BACKEND", kind, present)
