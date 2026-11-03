"""Exercise the actual installed wheel outside the checkout, without sampling."""
from pathlib import Path
import hashlib
import json
import pandas as pd
import causalpy as cp
from causalpy.custom_exceptions import DataException

assert '/site-packages/' in cp.__file__ and '/work/source/' not in cp.__file__
data = pd.DataFrame({'trt': [0, 1, 0, 1], 'x': [1., 2., 3., 4.],
                     'outcome': [2., 4., 1., 5.]})
result = cp.InversePropensityWeighting(data, 'trt ~ 1 + x', 'outcome', 'robust')
assert result.t.shape == (4, 1)
try:
    cp.InversePropensityWeighting(data.assign(trt=data.trt.astype(bool)),
                                 'trt ~ 1 + x', 'outcome', 'robust')
except DataException as error:
    assert 'one numeric 0/1 column' in str(error)
else:
    raise AssertionError('Installed wheel accepted categorical treatment')
manifest = json.loads(Path('/packet/manifest.json').read_text())
module = Path(cp.__file__).parent / 'experiments/inverse_propensity_weighting.py'
assert hashlib.sha256(module.read_bytes()).hexdigest() == manifest['overlay_sha256'][
    'causalpy/experiments/inverse_propensity_weighting.py']
print('INSTALLED_PUBLIC_API_SMOKE_PASS', cp.__file__)
