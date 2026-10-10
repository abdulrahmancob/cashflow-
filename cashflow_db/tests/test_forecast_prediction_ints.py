"""Integer prediction columns are written as integers even after pandas turns them into floats."""

from __future__ import annotations


def test_prediction_integer_columns_survive_pandas_float_drift():
    import math

    import numpy as np
    import pandas as pd

    from cashflow_db.repository.forecast import _int_or_none, _prediction_params

    assert _int_or_none(268.0) == 268
    assert _int_or_none(np.float64(12.0)) == 12
    assert _int_or_none(np.int64(7)) == 7
    assert _int_or_none(float("nan")) is None
    assert _int_or_none(pd.NA) is None
    assert _int_or_none(None) is None
    assert _int_or_none("n/a") is None
    assert _int_or_none(math.inf) is None

    # The overlay appends rows without overdue_days, which turns the column into floats.
    base = pd.DataFrame([{"visit_id": "v1", "outcome_stage": "overdue", "overdue_days": 268}])
    extra = pd.DataFrame([{"visit_id": None, "outcome_stage": "on_track", "expected_amount": 10.0}])
    merged = pd.concat([base, extra], ignore_index=True)
    rows = merged.to_dict("records")
    first = _prediction_params("run", rows[0])
    second = _prediction_params("run", rows[1])
    assert first[5] == 268 and isinstance(first[5], int)
    assert second[5] is None
