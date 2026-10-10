"""Gates for the Waystar nightly cycle."""

from cashflow_ops.checks.source_checks import pt_city_source_gate, waystar_claims_gate


def test_waystar_ok_when_file_present():
    crit, alerts = waystar_claims_gate(recent_present=True, skipped=False)
    assert crit == []
    assert alerts == []


def test_waystar_ok_when_skipped():
    crit, alerts = waystar_claims_gate(recent_present=False, skipped=True)
    assert crit == []


def test_waystar_fails_when_missing():
    crit, alerts = waystar_claims_gate(recent_present=False, skipped=False)
    assert crit == ["Waystar recent claims file missing"]


def test_waystar_fresh_file_no_alert():
    crit, alerts = waystar_claims_gate(recent_present=True, skipped=False, age_hours=0.4)
    assert crit == []
    assert alerts == []


def test_waystar_stale_file_alerts_without_blocking():
    crit, alerts = waystar_claims_gate(recent_present=True, skipped=False, age_hours=24.2)
    assert crit == []
    assert alerts[0]["alert_key"] == "waystar_claims_stale"
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["payload"] == {"age_hours": 24.2}


def test_pt_city_ok_when_files_present():
    crit, alerts = pt_city_source_gate(
        visit_present=True,
        patient_present=True,
        charge_csvs=2,
        skipped=False,
    )
    assert crit == []
    assert alerts == []


def test_pt_city_ok_when_db_has_visits():
    crit, alerts = pt_city_source_gate(
        visit_present=False,
        patient_present=True,
        charge_csvs=1,
        skipped=False,
        db_visits=180000,
    )
    assert crit == []
    assert alerts[0]["alert_key"] == "pt_city_visit_csv_missing"


def test_pt_city_fails_without_visit_or_patient():
    crit, alerts = pt_city_source_gate(
        visit_present=False,
        patient_present=False,
        charge_csvs=0,
        skipped=False,
        db_visits=0,
    )
    assert "PT_CITY visit extract missing" in crit
    assert "PT_CITY patient account↔EMR CSV missing" in crit
