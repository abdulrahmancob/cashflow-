"""Gates that keep obsolete patient-first helpers out of the Case pipeline."""

from __future__ import annotations

import sys

# Modules that implement patient-first coverage waves — must not be imported
# by run_case_pipeline / case_download / case_extract / case_merge.
OBSOLETE_PATIENT_PATH_MODULES = frozenset(
    {
        "snowflake_pull.scripts.run_track_d",
        "run_track_d",
    }
)

OBSOLETE_HELPER_NAMES = frozenset(
    {
        "_build_patients_csv",
    }
)


def assert_case_pipeline_clean_imports() -> None:
    loaded = OBSOLETE_PATIENT_PATH_MODULES.intersection(sys.modules)
    if loaded:
        raise RuntimeError(
            "Case pipeline gate violated — patient-path modules loaded: "
            + ", ".join(sorted(loaded))
        )


def assert_not_patient_first_symbol(name: str) -> None:
    if name in OBSOLETE_HELPER_NAMES:
        raise RuntimeError(
            f"Case pipeline must not call obsolete helper {name!r}"
        )
