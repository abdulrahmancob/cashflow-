#!/bin/bash
# PDF-only sync. Never touch live FSM / health written by case_drain.
set -euo pipefail
SRC=/home/abdu/data-staging/side_by_side_case
DST=/data/exports/side_by_side_case
if [[ -d "$SRC/cases" ]]; then
  rsync -a --ignore-existing \
    --exclude '*.sqlite*' \
    --exclude 'checkpoint.json' \
    --exclude 'health.json' \
    --exclude 'reports/' \
    --exclude '*.partial' \
    "$SRC/cases/" "$DST/cases/"
fi
