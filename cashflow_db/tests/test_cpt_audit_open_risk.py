"""Open audit-queue risk totals exclude warnings, stale, and closed items."""

from __future__ import annotations

from datetime import date

from cashflow_db.repository import cpt_audit


def test_open_risk_where_is_risk_open_not_stale():
    where, params = cpt_audit._open_risk_where()
    assert "f.bucket = 'risk'" in where
    assert "f.is_stale = false" in where
    assert "eligibility_work_item" in where
    assert "'paid'" in where
    assert "'partial'" not in where
    assert "total_paid" not in where
    assert "CURRENT_DATE" not in where
    assert params[0] == ["cpt", "icd", "demo"]
    assert params[1] == ["open", "in_progress"]
    assert "warning" not in params[1]
    assert "resolved" not in params[1]
    assert "ignored" not in params[1]


def test_open_risk_where_optional_filters():
    where, params = cpt_audit._open_risk_where(
        d0=date(2026, 8, 1),
        d1=date(2026, 8, 31),
        facilities=["Bedstuy"],
        insurers=["1199"],
    )
    assert "f.dos >= %s" in where
    assert "f.dos <= %s" in where
    assert "f.facility_name = ANY(%s)" in where
    assert "f.insurance_name = ANY(%s)" in where
    assert date(2026, 8, 1) in params
    assert date(2026, 8, 31) in params
    assert ["Bedstuy"] in params
    assert ["1199"] in params


def _is_denied_sql(sql: str) -> bool:
    return "eligibility_work_item" in sql and "cpt_audit_finding" not in sql


def test_open_risk_exposure_sql_and_share(monkeypatch):
    captured: list[str] = []

    def fake_fetchone(_conn, sql, params=None):
        captured.append(sql)
        if "INTERSECT" in sql:
            return {"visit_count": 0}
        if _is_denied_sql(sql):
            return {"exposure_amount": 0.0, "visit_count": 0}
        assert params[0] == ["cpt", "icd", "demo"]
        assert params[1] == ["open", "in_progress"]
        return {"exposure_amount": 50.0, "visit_count": 2}

    def fake_fetchall(_conn, sql, params=None):
        captured.append(sql)
        if _is_denied_sql(sql):
            return []
        if "ins_name" in sql:
            return [
                {"ins_name": "Aetna", "exposure_amount": 40.0, "visit_count": 1},
                {"ins_name": "1199", "exposure_amount": 10.0, "visit_count": 1},
            ]
        return [
            {"risk_flag": "cpt", "exposure_amount": 30.0, "visit_count": 1},
            {"risk_flag": "icd", "exposure_amount": 20.0, "visit_count": 1},
        ]

    monkeypatch.setattr(cpt_audit.client, "fetchone", fake_fetchone)
    monkeypatch.setattr(cpt_audit.client, "fetchall", fake_fetchall)
    payload = cpt_audit.open_risk_exposure(object())
    audit_blob = "\n".join(
        s for s in captured if "cpt_audit_finding" in s and "INTERSECT" not in s
    )
    assert "f.bucket = 'risk'" in audit_blob
    assert "f.is_stale = false" in audit_blob
    assert "eligibility_work_item" in audit_blob
    assert "source_visit_status" in audit_blob
    assert "'paid'" in audit_blob
    assert "'partial'" not in audit_blob
    assert "total_paid" not in audit_blob
    assert "warning" not in audit_blob
    assert "unsubmitted" not in audit_blob
    assert "LIMIT 40" not in audit_blob
    assert abs(float(payload["exposure_amount"]) - 50.0) < 0.01
    assert payload["visit_count"] == 2
    assert payload["by_insurance"][0]["ins_name"] == "Aetna"
    assert abs(float(payload["by_insurance"][0]["share_pct"]) - 80.0) < 0.01
    flags = {r["risk_flag"] for r in payload["by_flag"]}
    assert flags == {"cpt", "icd"}
    assert "unsubmitted" not in flags
    assert "denied" not in flags
    assert any("INTERSECT" in s for s in captured)
    assert any("sf.charged_amount" in s for s in captured)


def test_open_risk_exposure_merges_denied_totals_and_flag(monkeypatch):
    def fake_fetchone(_conn, sql, params=None):
        if "INTERSECT" in sql:
            return {"visit_count": 1}
        if _is_denied_sql(sql):
            return {"exposure_amount": 30.0, "visit_count": 2}
        return {"exposure_amount": 50.0, "visit_count": 2}

    def fake_fetchall(_conn, sql, params=None):
        if _is_denied_sql(sql):
            return [{"ins_name": "Aetna", "exposure_amount": 30.0, "visit_count": 2}]
        if "risk_flag" in sql:
            return [
                {"risk_flag": "cpt", "exposure_amount": 30.0, "visit_count": 1},
                {"risk_flag": "icd", "exposure_amount": 20.0, "visit_count": 1},
            ]
        return [
            {"ins_name": "Aetna", "exposure_amount": 40.0, "visit_count": 1},
            {"ins_name": "1199", "exposure_amount": 10.0, "visit_count": 1},
        ]

    monkeypatch.setattr(cpt_audit.client, "fetchone", fake_fetchone)
    monkeypatch.setattr(cpt_audit.client, "fetchall", fake_fetchall)
    payload = cpt_audit.open_risk_exposure(object())
    assert abs(float(payload["exposure_amount"]) - 80.0) < 0.01
    assert payload["visit_count"] == 3
    flags = {r["risk_flag"]: r for r in payload["by_flag"]}
    assert set(flags) == {"cpt", "icd", "denied"}
    assert abs(float(flags["denied"]["exposure_amount"]) - 30.0) < 0.01
    assert flags["denied"]["visit_count"] == 2
    by_ins = {r["ins_name"]: r for r in payload["by_insurance"]}
    assert abs(float(by_ins["Aetna"]["exposure_amount"]) - 70.0) < 0.01
    assert abs(float(by_ins["1199"]["exposure_amount"]) - 10.0) < 0.01
    assert abs(float(by_ins["Aetna"]["share_pct"]) - 87.5) < 0.01


def test_open_risk_exposure_empty(monkeypatch):
    monkeypatch.setattr(cpt_audit.client, "fetchone", lambda *_a, **_k: None)
    monkeypatch.setattr(cpt_audit.client, "fetchall", lambda *_a, **_k: [])
    payload = cpt_audit.open_risk_exposure(object())
    assert payload["exposure_amount"] == 0.0
    assert payload["visit_count"] == 0
    assert payload["by_insurance"] == []
    assert payload["by_flag"] == []
