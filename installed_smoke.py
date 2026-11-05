"""Exercise the actual final installed wheel outside its source checkout."""
from pathlib import Path
import hashlib
import json
import pandas as pd
import causalpy as cp

assert '/site-packages/' in cp.__file__ and '/work/source/' not in cp.__file__
final = json.loads(Path('/final/final-overlay.json').read_text())
package = Path(cp.__file__).parent
for path in ['causalpy/steps/report.py', 'causalpy/templates/report.html']:
    target = package / path.removeprefix('causalpy/')
    assert hashlib.sha256(target.read_bytes()).hexdigest() == final['files'][path]
assert len(cp.load_data('its')) > 0
context = cp.PipelineContext(data=pd.DataFrame())
context.effect_summary = cp.EffectSummary(table=pd.DataFrame({'mean': [12.25]}), text='<script>literal</script>')
html = cp.GenerateReport(include_plots=False).run(context).report
assert 'gt_table' in html and '12.25' in html
assert '&lt;script&gt;literal&lt;/script&gt;' in html
print('INSTALLED_PUBLIC_REPORT_API_AND_SHIPPED_TEMPLATE_PASS', cp.__file__)
