"""Unit tests for claim PDF helpers."""

from claim_pdf import build_pdf_url, pdf_filename


def test_build_pdf_url():
    url = build_pdf_url(
        claim_id="11797899212",
        instance_id="12242251879",
        app_id="1",
        form="CMS1500_0212",
    )
    assert "ViewClaimPDF" in url
    assert "claimId=11797899212" in url
    assert "instanceId=12242251879" in url
    assert "form=CMS1500_0212" in url


def test_pdf_filename():
    name = pdf_filename({"claim_number": "PV410531551", "claim_id": "11893045626"})
    assert name == "PV410531551_11893045626.pdf"


if __name__ == "__main__":
    test_build_pdf_url()
    test_pdf_filename()
    print("claim_pdf tests passed")
