"""Intake referral-source reader (rules R1–R16 of the intake census plan)."""

from .anchors import FAMILIES, GROUPS, PRIORITY, SOURCE_CODES, detect_family
from .decide import Reading, decide, merge_readings
from .reader import IntakeResult, read_intake

__all__ = [
    "FAMILIES",
    "GROUPS",
    "PRIORITY",
    "SOURCE_CODES",
    "IntakeResult",
    "Reading",
    "decide",
    "detect_family",
    "merge_readings",
    "read_intake",
]
