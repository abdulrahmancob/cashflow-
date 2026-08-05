"""Tests for canonical payer registry resolve + Fidelis/NYNM cluster."""

from __future__ import annotations

import unittest

from cashflow_reconcile.insurance_map import (
    load_insurance_rules,
    map_insurance_to_payors,
    payor_matches_insurance,
)
from cashflow_reconcile.payer_registry import (
    extract_ach_payer_head,
    extract_eft_refs_from_description,
    is_ach_processor,
    normalize_raw,
    resolve,
    resolve_tracker_description,
)


class NormalizeRawTests(unittest.TestCase):
    def test_strips_provider_number(self):
        cleaned = normalize_raw(
            "Fidelis-Medicaid Provider Number: 1-888-343-3547"
        )
        self.assertEqual(cleaned.lower(), "fidelis medicaid")

    def test_collapses_punctuation(self):
        self.assertEqual(normalize_raw("Emblem-HIP"), "Emblem HIP")


class FidelisClusterResolveTests(unittest.TestCase):
    def test_webpt_variants_resolve_to_fidelis(self):
        for raw in (
            "Fidelis-Medicaid",
            "Fidelis Care",
            "Fidelis-Medicare",
            "Wellcare",
            "Ambetter",
            "Fidelis-Medicaid Provider Number: 1-888-343-3547",
        ):
            hit = resolve(raw, "webpt")
            self.assertIsNotNone(hit, raw)
            assert hit is not None
            self.assertEqual(hit.code, "FIDELIS", raw)

    def test_revflow_new_york_network_ipa(self):
        hit = resolve("NEW YORK NETWORK IPA", "revflow")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.code, "FIDELIS")

    def test_tracker_nynm(self):
        hit = resolve("NYNM", "tracker")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.code, "FIDELIS")

    def test_tracker_description_nynm(self):
        hit = resolve_tracker_description("NYNM ACH CREDIT 260105EFT100415")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.code, "FIDELIS")
        self.assertEqual(extract_ach_payer_head("NYNM ACH CREDIT X"), "NYNM")

    def test_tracker_description_nynm_des_format(self):
        desc = (
            "NYNM DES:NYNM PMT ID:823830674 INDN:Physical Therapy of th "
            "CO ID:2113322995 CCD PMT INFO:TRN*1*260105EFT100415*1999999999\\"
        )
        self.assertEqual(extract_ach_payer_head(desc), "NYNM")
        hit = resolve_tracker_description(desc)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.code, "FIDELIS")


class AchProcessorAndTrnTests(unittest.TestCase):
    def test_extract_trn_eft_from_echo_description(self):
        desc = (
            "HNB - ECHO DES:HCCLAIMPMT ID:823830674 INDN:PHYSICAL THERAPY OF TH "
            "CO ID:1341858386 CCD PMT INFO:TRN*1*1256512458*1341858379\\"
        )
        self.assertEqual(extract_eft_refs_from_description(desc), ["1256512458"])
        self.assertEqual(extract_ach_payer_head(desc), "HNB ECHO")
        hit = resolve_tracker_description(desc)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.code, "ACH_PROCESSOR_ECHO")
        self.assertTrue(is_ach_processor(hit))

    def test_pnc_echo_and_payplus_are_processors(self):
        self.assertEqual(resolve("PNC-ECHO", "tracker").code, "ACH_PROCESSOR_ECHO")
        self.assertEqual(resolve("PayPlus", "tracker").code, "ACH_PROCESSOR_PAYPLUS")
        self.assertTrue(is_ach_processor("ACH_PROCESSOR_ECHO"))
        self.assertFalse(is_ach_processor("ANTHEM"))


class OtherSeedClustersTests(unittest.TestCase):
    def test_healthfirst_and_metroplus(self):
        self.assertEqual(resolve("Healthfirst-Medicaid", "webpt").code, "HEALTHFIRST")
        self.assertEqual(resolve("HEALTHFIRST PHSP, INC.", "revflow").code, "HEALTHFIRST")
        self.assertEqual(resolve("Metroplus", "webpt").code, "METROPLUS")
        self.assertEqual(resolve("METROPLUS ESSENTIAL PLAN", "revflow").code, "METROPLUS")

    def test_affinity_is_molina(self):
        self.assertEqual(resolve("Affinity", "webpt").code, "MOLINA")
        self.assertEqual(
            resolve("MOLINA HEALTHCARE OF NEW YORK, INC.", "revflow").code, "MOLINA"
        )

    def test_emblem_ghi(self):
        self.assertEqual(resolve("GHI", "webpt").code, "EMBLEM")
        self.assertEqual(resolve("Emblem-HIP", "webpt").code, "EMBLEM")


class InsuranceMapBridgeTests(unittest.TestCase):
    def test_wellcare_maps_to_fidelis_revflow_payors(self):
        rules = load_insurance_rules()
        mapped = map_insurance_to_payors("Wellcare", rules)
        self.assertIn("NEW YORK NETWORK IPA", mapped)
        self.assertIn("FIDELIS CAREWELLCARE BY FIDELIS CARE", mapped)

    def test_fidelis_medicaid_matches_new_york_network_ipa(self):
        rules = load_insurance_rules()
        self.assertTrue(
            payor_matches_insurance(
                "NEW YORK NETWORK IPA",
                ["Fidelis-Medicaid"],
                rules,
            )
        )

    def test_nynm_and_fidelis_share_org(self):
        webpt = resolve("Fidelis-Medicaid", "webpt")
        tracker = resolve("NYNM", "tracker")
        revflow = resolve("NEW YORK NETWORK IPA", "revflow")
        self.assertEqual(webpt.code, tracker.code)
        self.assertEqual(webpt.code, revflow.code)


if __name__ == "__main__":
    unittest.main()
