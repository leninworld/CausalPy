#   Copyright 2022 - 2026 The PyMC Labs Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
"""Optional rich report tables preserve causal summaries and fallback contracts."""

from __future__ import annotations

import builtins
import importlib.util
import math
import re
from html.parser import HTMLParser

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LinearRegression

import causalpy as cp
from causalpy.checks.base import CheckResult
from causalpy.pipeline import PipelineContext
from causalpy.reporting import EffectSummary
from causalpy.steps import report as report_module
from causalpy.steps.report import GenerateReport


class _TableHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.tags = []
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)

    def handle_data(self, data):
        self.text.append(data)


@pytest.fixture
def real_maketables():
    return pytest.importorskip("maketables")


@pytest.mark.parametrize("rows", [1, 2])
def test_rich_effect_estimates_preserve_values_and_hide_index(real_maketables, rows):
    table = pd.DataFrame(
        {"mean": [12.25, 24.5][:rows], "ci_lower": [10.25, 20.5][:rows],
         "ci_upper": [14.25, 28.5][:rows], "p_value": [0.125, 0.0625][:rows]},
        index=pd.Index(["private-row"] * rows, name="private-index"),
    )
    before = table.copy(deep=True)
    html = report_module._render_effect_table(table)
    parsed = _TableHTML()
    parsed.feed(html)
    assert "gt_table" in html
    assert "private-row" not in html
    assert "private-index" not in html
    visible = " ".join(parsed.text)
    assert "12.25" in visible
    assert "10.25" in visible
    assert "14.25" in visible
    assert "mean" in visible
    assert visible.index("mean") < visible.index("ci_lower") < visible.index("ci_upper")
    pd.testing.assert_frame_equal(table, before)


def test_rich_effect_table_escapes_text_and_has_no_active_assets(real_maketables, tmp_path):
    label = '<script>alert("header")</script>'
    value = '<img src=x onerror="alert(1)"> & α\nplain_text ± R²'
    breakout = 'x" onmouseover="alert(1)'
    html = report_module._render_effect_table(pd.DataFrame({label: [value], breakout: [r"$x$ \alpha ->"], "mean": [1.25]}))
    (tmp_path / "rich-table.html").write_text(html)
    parsed = _TableHTML()
    parsed.feed(html)
    visible = " ".join(parsed.text)
    assert label in visible
    assert breakout in visible
    allowed_tags = {"div", "style", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "br", "span", "sub", "sup", "caption", "colgroup", "col"}
    allowed_attributes = {"id", "class", "style", "rowspan", "colspan", "scope", "headers", "data-quarto-disable-processing", "data-quarto-bootstrap"}
    assert set(parsed.tags) <= allowed_tags
    assert {key for key, _ in parsed.attributes} <= allowed_attributes
    assert '<img src=x onerror="alert(1)">' in visible
    assert "α" in visible
    assert "plain_text" in visible
    assert "±" in visible
    assert "sup" in parsed.tags
    assert r"$x$ \alpha ->" in visible
    assert "script" not in parsed.tags
    assert "img" not in parsed.tags
    assert "link" not in parsed.tags
    assert all(not key.lower().startswith("on") for key, _ in parsed.attributes)
    assert all(key not in {"src", "href"} for key, _ in parsed.attributes)
    assert "@import" not in html.lower()
    assert re.search(r"url\(\s*[\"\']?(?:https?:|//)", html, re.IGNORECASE) is None


def test_rich_table_does_not_display_or_mutate_global_style(real_maketables, monkeypatch):
    import importlib

    module = importlib.import_module(real_maketables.MTable.__module__)
    def forbidden_display(*args, **kwargs):
        pytest.fail("Report generation must not display notebook output")
    monkeypatch.setattr(module, "display", forbidden_display)
    monkeypatch.setattr(importlib.import_module("IPython.display"), "display", forbidden_display)
    style = dict(real_maketables.MTable.DEFAULT_GT_STYLE)
    frame = pd.DataFrame({"mean": [1.25]})
    assert isinstance(report_module._render_effect_table(frame), str)
    assert isinstance(report_module._render_effect_table(frame), str)
    assert real_maketables.MTable.DEFAULT_GT_STYLE == style


@pytest.mark.parametrize("table", [
    pd.DataFrame(),
    pd.DataFrame({"cases": [1]}),
    pd.DataFrame({"self": [1]}),
    pd.DataFrame({1: [2]}),
    pd.DataFrame([[1, 2]], columns=["same", "same"]),
    pd.DataFrame([[1]], columns=pd.MultiIndex.from_tuples([("a", "b")])),
])
def test_custom_effect_schema_retains_pandas_html(table):
    assert report_module._render_effect_table(table) == table.to_html(classes="", index=False, border=0)


@pytest.mark.parametrize("table", [
    pd.DataFrame({1: [2]}),
    pd.DataFrame([[1, 2]], columns=["same", "same"]),
    pd.DataFrame([[1]], columns=pd.MultiIndex.from_tuples([("a", "b")])),
])
def test_sensitivity_tables_keep_all_supported_pandas_schemas(table):
    context = PipelineContext(data=pd.DataFrame())
    context.sensitivity_results = [CheckResult(check_name="Diagnostic", passed=None, text="Unchanged", table=table)]
    html = GenerateReport(include_plots=False).run(context).report
    assert table.to_html(classes="", index=False, border=0) in html
    assert "INFO" in html


def test_genuine_absent_maketables_uses_real_pandas():
    if importlib.util.find_spec("maketables") is not None:
        pytest.skip("This contract runs in the genuine dependency-absent lane")
    table = pd.DataFrame({"estimate": ["<script> & value"]})
    assert report_module._render_effect_table(table) == table.to_html(classes="", index=False, border=0)


@pytest.mark.parametrize("missing_name", ["maketables", "great_tables"])
def test_only_missing_root_dependency_triggers_fallback(monkeypatch, missing_name):
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == "maketables":
            raise ModuleNotFoundError("Missing dependency", name=missing_name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing)
    table = pd.DataFrame({"mean": [1.25]})
    if missing_name == "maketables":
        assert report_module._render_effect_table(table) == table.to_html(classes="", index=False, border=0)
    else:
        with pytest.raises(ModuleNotFoundError, match="Missing dependency"):
            report_module._render_effect_table(table)


@pytest.mark.parametrize(
    "result, expected", [(RuntimeError("backend failed"), RuntimeError), (None, TypeError)]
)
def test_installed_renderer_failures_are_not_hidden(real_maketables, monkeypatch, result, expected):
    def render(self, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(real_maketables.MTable, "make", render)
    with pytest.raises(expected):
        report_module._render_effect_table(pd.DataFrame({"mean": [1.25]}))


def test_real_rich_report_is_returned_and_saved(real_maketables, tmp_path):
    context = PipelineContext(data=pd.DataFrame())
    context.effect_summary = EffectSummary(table=pd.DataFrame({"mean": [12.25]}), text="<script>prose</script>")
    destination = tmp_path / "report.html"
    html = GenerateReport(include_plots=False, output_file=destination).run(context).report
    assert html == destination.read_text()
    assert "gt_table" in html
    assert 'class="report-table"' in html
    assert "&lt;script&gt;prose&lt;/script&gt;" in html


def test_rich_table_preserves_small_large_and_nonfinite_values(real_maketables):
    table = pd.DataFrame({"mean": [1e-9, 9e12, -0.125], "median": [float("nan"), float("inf"), -float("inf")]})
    before = table.copy(deep=True)
    parsed = _TableHTML()
    parsed.feed(report_module._render_effect_table(table))
    numbers = []
    for token in parsed.text:
        try:
            numbers.append(float(token.strip().replace(",", "")))
        except ValueError:
            continue
    for expected in [1e-9, 9e12, -0.125]:
        assert any(math.isclose(actual, expected, rel_tol=1e-9, abs_tol=0) for actual in numbers)
    assert any("inf" in token.lower() or "∞" in token for token in parsed.text)
    pd.testing.assert_frame_equal(table, before)


def test_real_its_causal_summary_reaches_rich_report(real_maketables):
    time = np.arange(50)
    data = pd.DataFrame({"t": time, "y": 2 + 0.3 * time + np.where(time >= 30, 5.0, 0.0)})
    experiment = cp.InterruptedTimeSeries(
        data, treatment_time=30, formula="y ~ 1 + t",
        model=cp.create_causalpy_compatible_class(LinearRegression()),
    ).fit()
    summary = experiment.effect_summary()
    before = summary.table.copy(deep=True)
    assert np.isclose(summary.table.loc["average", "mean"], 5.0)
    context = PipelineContext(data=data)
    context.experiment = experiment
    context.effect_summary = summary
    html = GenerateReport(include_plots=False).run(context).report
    assert "gt_table" in html
    parsed = _TableHTML()
    parsed.feed(html)
    values = []
    for token in parsed.text:
        try:
            values.append(float(token.strip().replace(",", "")))
        except ValueError:
            continue
    assert any(math.isclose(value, 5.0, rel_tol=1e-9, abs_tol=1e-9) for value in values)
    pd.testing.assert_frame_equal(summary.table, before)
