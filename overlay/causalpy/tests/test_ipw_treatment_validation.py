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
"""IPW requires a single numeric treatment response, without choosing a level."""

import numpy as np
import pandas as pd
import pymc as pm
import pytest

import causalpy as cp
from causalpy.custom_exceptions import DataException


@pytest.fixture(autouse=True)
def no_constructor_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treatment validation must not enter a sampler."""

    def fail_sampling(*args, **kwargs):
        pytest.fail("Constructing IPW must not sample")

    monkeypatch.setattr(pm, "sample", fail_sampling)
    monkeypatch.setattr(pm, "sample_prior_predictive", fail_sampling)


def _data(treatment) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trt": treatment,
            "x": [1.0, 2.0, 3.0, 4.0],
            "outcome": [3.0, 1.0, 8.0, 5.0],
        }
    )


def _construct(data: pd.DataFrame, formula: str = "trt ~ 1 + x"):
    return cp.InversePropensityWeighting(
        data,
        formula=formula,
        outcome_variable="outcome",
        weighting_scheme="robust",
    )


@pytest.mark.parametrize(
    "treatment,formula",
    [
        pytest.param([False, True, False, True], "trt ~ 1 + x", id="bool-two-levels"),
        pytest.param([False] * 4, "trt ~ 1 + x", id="bool-all-false"),
        pytest.param([True] * 4, "trt ~ 1 + x", id="bool-all-true"),
        pytest.param(["no", "yes", "no", "yes"], "trt ~ 1 + x", id="string-two-levels"),
        pytest.param(["no"] * 4, "trt ~ 1 + x", id="string-one-level"),
        pytest.param(
            pd.Series(["no", "yes", "no", "yes"], dtype="string"),
            "trt ~ 1 + x",
            id="nullable-string",
        ),
        pytest.param(
            pd.Categorical([0, 1, 0, 1]), "trt ~ 1 + x", id="categorical-numeric"
        ),
        pytest.param(
            pd.Categorical([0] * 4), "trt ~ 1 + x", id="categorical-one-level"
        ),
        pytest.param([0, 1, 0, 1], "C(trt) ~ 1 + x", id="explicit-categorical"),
        pytest.param([0] * 4, "C(trt) ~ 1 + x", id="explicit-categorical-constant"),
        pytest.param([0, 1, 0, 1], "I(trt == 1) ~ 1 + x", id="comparison-lhs"),
        pytest.param([0, 1, 0, 1], "trt + x ~ 1", id="numeric-multivariate-lhs"),
    ],
)
def test_ipw_rejects_non_numeric_single_response(treatment, formula):
    """Categorical factors cannot silently recode treatment, even at one level."""
    with pytest.raises(DataException, match="one numeric 0/1 column") as exc:
        _construct(_data(treatment), formula)

    assert "Patsy produced columns" in str(exc.value)
    assert "trt" in str(exc.value)
    assert "Encode" in str(exc.value)


@pytest.mark.parametrize(
    "treatment,formula,expected",
    [
        pytest.param([0, 1, 0, 1], "trt ~ 1 + x", [0, 1, 0, 1], id="integer"),
        pytest.param([0.0, 1.0, 0.0, 1.0], "trt ~ 1 + x", [0, 1, 0, 1], id="float"),
        pytest.param(
            [0, 1, 0, 1],
            "I(1 - trt) ~ 1 + x",
            [1, 0, 1, 0],
            id="numeric-transform",
        ),
        pytest.param([0] * 4, "trt ~ 1 + x", [0] * 4, id="constant-zero"),
        pytest.param([1] * 4, "trt ~ 1 + x", [1] * 4, id="constant-one"),
    ],
)
def test_ipw_preserves_numeric_treatment(treatment, formula, expected):
    """Valid numeric encodings retain the response and outcome-design shapes."""
    result = _construct(_data(treatment), formula)

    assert result.t.shape == (4, 1)
    np.testing.assert_array_equal(result.t.ravel(), expected)
    np.testing.assert_array_equal(result.X_outcome["trt"], expected)
    X, t, coords = result._fit_inputs()
    np.testing.assert_array_equal(t, result.t)
    assert X.shape == (4, 2)
    assert coords["obs_ind"] == list(range(4))


@pytest.mark.parametrize("value", [0.5, 2])
def test_ipw_preserves_numeric_value_validation(value):
    """The existing binary-value rejection remains a separate validation path."""
    with pytest.raises(DataException, match="0-1 binary"):
        _construct(_data([0, value, 0, value]))


def test_ipw_preserves_row_dropping_with_duplicate_index():
    """Patsy's kept row positions still pair treatments with their outcomes."""
    data = _data([0, 1, 1, 0])
    data.index = [90, 20, 90, -3]
    data.iloc[1, data.columns.get_loc("x")] = np.nan
    result = _construct(data)

    np.testing.assert_array_equal(result.t.ravel(), [0, 1, 0])
    np.testing.assert_array_equal(result.X_outcome["trt"], [0, 1, 0])
    np.testing.assert_array_equal(result.y, data["outcome"].iloc[[0, 2, 3]])
    assert result.X.shape == (3, 2)
    assert result.coords["obs_ind"] == [0, 1, 2]
