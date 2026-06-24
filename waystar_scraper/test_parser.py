"""Smoke tests for HTML parsing (no live login required)."""

from parser import parse_search_result_html

SAMPLE_HTML = """
<div id="claimListingTableContainer">
  <span id="resultCountTotal">85313</span>
  <span id="totalPageCount">2844</span>
  <table id="claimsGrid">
    <tr class="gridViewRow gridViewExpandableRow"
        data-claimid="11684440292"
        data-patientname="ROY, PAPIA"
        data-instanceid="12235118386"
        data-payername="MetroPlus Health Plan (13265)"
        data-payerid="13265"
        data-servicedate="4/29/2026"
        data-hasremit="True"
        data-totalcharges="270.00"
        data-isheld="False"
        data-isopen="False">
      <td class="patientNumberCell">PV410222955</td>
      <td class="dtLastSubmissionCell">06/08/2026</td>
      <td class="subPayerCell"><span title="METROPLUS (13265)">METROPL...(13265)</span></td>
      <td class="chargesCell">$270.00</td>
      <td class="descriptionCell">
        <a class="claimStatusLink" title="CLAIM SENT TO AN INTERMEDIARY">Sent to Intermediary</a>
      </td>
      <td class="providerNameCell"><span>ABDELSALAM, YASSER</span></td>
      <td class="workGroupCell"><span>Not in a WorkGroup</span></td>
    </tr>
  </table>
</div>
"""


def test_parse_sample():
    result = parse_search_result_html(SAMPLE_HTML)
    assert result["total_results"] == 85313
    assert result["total_pages"] == 2844
    assert len(result["claims"]) == 1

    claim = result["claims"][0]
    assert claim["claim_id"] == "11684440292"
    assert claim["patient_name"] == "ROY, PAPIA"
    assert claim["claim_number"] == "PV410222955"
    assert claim["status"] == "Sent to Intermediary"
    assert claim["charges"] == 270.0
    assert claim["has_remit"] is True


if __name__ == "__main__":
    test_parse_sample()
    print("parser smoke test passed")
