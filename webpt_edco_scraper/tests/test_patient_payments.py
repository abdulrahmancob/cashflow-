from pathlib import Path

from patient_payments_api import parse_patient_payments_html, parse_mmddyyyy
from payments_scrape import build_export_payments_cohort


SAMPLE = """
<script>
var transactions = [{"transactionId":1,"type":"Copay","amountPaid":10,"amountDue":25,"dateOfTransaction":"01/15/2026","dateOfService":"01/15/2026","description":"Office Visit Copay","paidMethodType":"Cash","creditType":"","checkAuthorizationNumber":""},{"transactionId":2,"type":"Copay","amountPaid":40,"amountDue":40,"dateOfTransaction":"06/22/2024","dateOfService":"06/22/2024","description":"Office Visit Copay","paidMethodType":"Debit Card","creditType":"Visa","checkAuthorizationNumber":"003298"}];
</script>
<table id="transactions-list" class="dataTable"><tbody></tbody></table>
<table id="transactions-balance">
<tr><td class="balance-label">Total Charge:</td><td class="total-due"></td></tr>
<tr><td class="balance-label">Total Paid:</td><td class="total-paid"></td></tr>
<tr><td class="balance-label">Balance:</td><td class="total-balance"></td></tr>
</table>
"""

SAMPLE_DOM = """
<table id="transactions-list" class="dataTable">
<tbody>
<tr class="odd">
<td class="dateOfService">06/22/2024</td>
<td class="dateOfTransaction">06/22/2024</td>
<td class="type">Copay</td>
<td class="description">Office Visit Copay</td>
<td class="amountDue-money">$40.00</td>
<td class="amountPaid-money">$40.00</td>
<td class="paidMethodType">Debit Card</td>
<td class="creditType">Visa</td>
<td class="checkAuthorizationNumber">003298</td>
<td></td><td></td>
</tr>
<tr class="even">
<td class="dateOfService">01/15/2026</td>
<td class="dateOfTransaction">01/15/2026</td>
<td class="type">Copay</td>
<td class="description">Office Visit Copay</td>
<td class="amountDue-money">$25.00</td>
<td class="amountPaid-money">$10.00</td>
<td class="paidMethodType">Cash</td>
<td class="creditType"></td>
<td class="checkAuthorizationNumber"></td>
<td></td><td></td>
</tr>
</tbody>
</table>
<table id="transactions-balance">
<tr><td class="balance-label">Total Charge:</td><td class="total-due">$65.00</td></tr>
<tr><td class="balance-label">Total Paid:</td><td class="total-paid">$50.00</td></tr>
<tr><td class="balance-label">Balance:</td><td class="total-balance">$15.00</td></tr>
</table>
"""


def test_parse_payments_js_blob():
    txns, totals = parse_patient_payments_html(SAMPLE)
    assert len(txns) == 2
    assert txns[0].amount_due == 25.0
    assert txns[0].amount_paid == 10.0
    assert totals["total_charge"] == 65.0
    assert totals["total_paid"] == 50.0
    assert totals["balance"] == 15.0


def test_parse_payments_dom():
    txns, totals = parse_patient_payments_html(SAMPLE_DOM)
    assert len(txns) == 2
    assert txns[1].amount_due == 25.0
    assert totals["balance"] == 15.0


def test_parse_mmddyyyy():
    assert parse_mmddyyyy("01/15/2026") == "2026-01-15"
    assert parse_mmddyyyy("6/22/2024") == "2024-06-22"
    assert parse_mmddyyyy("") is None


def test_parse_real_http_fixture():
    path = Path("output/jun_jul_2026/debug/payments_http_23865053.html")
    if not path.exists():
        return
    txns, totals = parse_patient_payments_html(
        path.read_text(encoding="utf-8", errors="replace")
    )
    assert len(txns) == 3
    assert totals["total_charge"] == 105.0


def test_build_export_payments_cohort(tmp_path: Path):
    csv_path = tmp_path / "export.csv"
    csv_path.write_text(
        "facility_id,facility_name,patient_id,patient_name,case_id,"
        "appointment_dates\n"
        "1,Clinic,100,Doe Jane,200,2026-03-01 10:00:00; 2026-09-01 10:00:00\n"
        "1,Clinic,101,No Case,,2026-02-01 10:00:00\n"
        "1,Clinic,102,Sep Only,300,2026-09-05 10:00:00\n",
        encoding="utf-8",
    )
    cohort = build_export_payments_cohort(
        export_csv=csv_path,
        start_month="2026-01",
        end_month="2026-08",
    )
    assert len(cohort) == 1
    assert cohort[0]["patient_id"] == "100"
    assert cohort[0]["case_id"] == "200"
