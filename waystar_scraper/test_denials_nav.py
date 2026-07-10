"""Tests for denials workcenter search argument builders."""

import unittest

from config import CATCH_ALL_WORKGROUP_ID, DEFAULT_DENIAL_DATE_FROM_2026, DEFAULT_DENIAL_DATE_TO_2026
from denials_nav import (
    build_workcenter_search_args,
    parse_workflow_stages,
    resolve_workgroup_id,
)


class TestDenialsNav(unittest.TestCase):
    def test_resolve_workgroup_catch_all(self) -> None:
        self.assertEqual(resolve_workgroup_id("catch-all"), CATCH_ALL_WORKGROUP_ID)
        self.assertEqual(resolve_workgroup_id("Catch-All"), CATCH_ALL_WORKGROUP_ID)
        self.assertIsNone(resolve_workgroup_id("current"))
        self.assertEqual(resolve_workgroup_id("41436"), "41436")

    def test_resolve_workgroup_invalid(self) -> None:
        with self.assertRaises(ValueError):
            resolve_workgroup_id("unknown-group")

    def test_parse_workflow_stages_all(self) -> None:
        self.assertEqual(parse_workflow_stages("all"), [])
        self.assertEqual(parse_workflow_stages(None), [])
        self.assertEqual(parse_workflow_stages(""), [])

    def test_parse_workflow_stages_list(self) -> None:
        self.assertEqual(parse_workflow_stages("1,8"), ["1", "8"])

    def test_build_search_args_catch_all_2026(self) -> None:
        args = build_workcenter_search_args(
            workgroup_id=CATCH_ALL_WORKGROUP_ID,
            workflow_stages=[],
            denial_date_from=DEFAULT_DENIAL_DATE_FROM_2026,
            denial_date_to=DEFAULT_DENIAL_DATE_TO_2026,
        )
        self.assertEqual(args["WorkgroupID"], CATCH_ALL_WORKGROUP_ID)
        self.assertEqual(args["WorkcenterStages"], [])
        self.assertEqual(args["DenialDateFrom"], DEFAULT_DENIAL_DATE_FROM_2026)
        self.assertEqual(args["DenialDateTo"], DEFAULT_DENIAL_DATE_TO_2026)
        self.assertFalse(args["ReminderDateDueOrPastDue"])
        self.assertEqual(args["WorkcenterGridArgs"]["PageSize"], "50")

    def test_build_search_args_reminder_due(self) -> None:
        args = build_workcenter_search_args(
            workgroup_id=CATCH_ALL_WORKGROUP_ID,
            reminder_due_only=True,
            workflow_stages=["1", "8"],
        )
        self.assertTrue(args["ReminderDateDueOrPastDue"])
        self.assertEqual(args["WorkcenterStages"], ["1", "8"])


if __name__ == "__main__":
    unittest.main()
