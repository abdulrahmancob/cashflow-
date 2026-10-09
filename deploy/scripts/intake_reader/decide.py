"""Marks, write-ins and booking → one Reading (R8, R9, R12, R13, R14)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .anchors import GROUPS, PRIORITY
from .controls import Control
from .writein import WriteIn, looks_like_name

_REVIEW_MARGIN = 0.3
_NEAR_MISS = -0.25


@dataclass
class Reading:
    source: str = "unreadable"
    marks: list[str] = field(default_factory=list)
    other_text: str = ""
    booking: list[str] = field(default_factory=list)
    family: str = ""
    page: int = -1
    confidence: float = 0.0
    needs_review: bool = True
    reasons: list[str] = field(default_factory=list)
    debug: dict = field(default_factory=dict)

    @property
    def group(self) -> str:
        return GROUPS.get(self.source, self.source)

    def as_row(self) -> dict[str, str]:
        return {
            "source": self.source,
            "source_group": self.group,
            "marks": ",".join(self.marks),
            "other_text": self.other_text,
            "booking": ",".join(self.booking),
            "confidence": f"{self.confidence:.2f}",
            "needs_review": "1" if self.needs_review else "",
            "family": self.family,
            "page": str(self.page),
            "reason": ";".join(self.reasons),
        }


def primary(marks: set[str]) -> str:
    for code in PRIORITY:
        if code in marks:
            return code
    return sorted(marks)[0]


def decide(
    family_id: str,
    page: int,
    hear: list[Control],
    booking: list[Control],
    writeins: list[WriteIn],
    text_hints: set[str] | None = None,
    block_found: bool = True,
    block_cut: bool = False,
) -> Reading:
    reading = Reading(family=family_id, page=page)
    hints = text_hints or set()
    if not block_found:
        reading.source = "no_question"
        reading.reasons.append("no_question")
        reading.confidence = 0.9
        reading.needs_review = False
        return reading
    if not hear:
        reading.source = "unreadable"
        reading.reasons.append("controls_not_found")
        return reading

    marks: dict[str, float] = {}
    for control in hear:
        if control.marked:
            marks[control.code] = max(marks.get(control.code, -9), control.score)
    near = [c for c in hear if not c.marked and c.score > _NEAR_MISS]
    # R12: OCR mark glyphs only break a near tie, never create a source alone.
    if not marks and len(near) == 1 and near[0].code in hints:
        marks[near[0].code] = near[0].score
        reading.reasons.append("text_hint")

    texts: list[str] = []
    for item in writeins:
        label = item.text.strip()
        if item.code == "other":
            code = item.mapped or "other"
            marks[code] = max(marks.get(code, 0.0), 0.5 if item.mapped else 0.3)
            if "other" in marks and code != "other":
                del marks["other"]
            texts.append(label)
            if item.mapped is None:
                reading.reasons.append("other_unmapped")
        elif item.code == "doctor":
            if "doctor" in marks:
                texts.append(f"doctor: {label}")
                continue
            code = "doctor" if (item.mapped in (None, "doctor", "phone") or looks_like_name(label)) else item.mapped
            if item.mapped not in (None, "doctor") and item.mapped != code:
                code = item.mapped
            marks[code] = max(marks.get(code, 0.0), 0.5)
            texts.append(f"doctor: {label}")
            reading.reasons.append("doctor_line_writein")
        else:
            # Word of Mouth ___ / Clinic staff ___ / De boca en boca ___: text on the line is that
            # option; when its box is not checked and the words name another source ("Sent by a
            # doctor" on the Word of Mouth line) the words win
            if item.code in marks:
                texts.append(f"{item.code}: {label}")
                continue
            code = item.mapped if item.mapped not in (None, "phone", item.code) else item.code
            if code not in marks:
                marks[code] = 0.4 if code == item.code else 0.5
                reading.reasons.append(f"{item.code}_line_writein")
            texts.append(f"{item.code}: {label}")
    reading.other_text = " | ".join(t for t in texts if t)[:200]
    reading.booking = sorted({c.code for c in booking if c.marked})

    if not marks:
        reading.source = "unmarked"
        reading.marks = []
        worst = max((c.score for c in hear), default=-9)
        reading.confidence = round(max(0.0, min(1.0, 0.95 - max(0.0, worst + 0.6))), 2)
        reading.needs_review = bool(near) or block_cut
        if near:
            reading.reasons.append("near_miss:" + ",".join(c.code for c in near))
        if block_cut:
            reading.reasons.append("block_cut")
        return reading

    ordered = sorted(marks.items(), key=lambda kv: (PRIORITY.index(kv[0]) if kv[0] in PRIORITY else 99))
    reading.marks = [code for code, _ in ordered]
    if len(marks) == 1:
        reading.source = reading.marks[0]
    else:
        reading.source = "multiple"
        reading.reasons.append("primary:" + primary(set(marks)))
    weakest = min(marks.values())
    reading.confidence = round(max(0.0, min(1.0, 0.6 + 0.4 * min(weakest, 1.0))), 2)
    reading.needs_review = (
        len(marks) > 1
        or weakest < _REVIEW_MARGIN
        or block_cut
        or "other_unmapped" in reading.reasons
        or len(near) > 0
    )
    if len(marks) >= 4:
        reading.reasons.append("too_many_marks")
    if block_cut:
        reading.reasons.append("block_cut")
    if near:
        reading.reasons.append("near_miss:" + ",".join(c.code for c in near))
    return reading


def merge_readings(readings: list[Reading]) -> Reading:
    """Best-evidence merge across duplicate pages or files (R15)."""
    usable = [r for r in readings if r.source not in ("unreadable", "no_question")]
    if not usable:
        return readings[0] if readings else Reading()
    usable.sort(key=lambda r: (-r.confidence, r.needs_review))
    best = usable[0]
    confident = [r for r in usable if r.confidence >= 0.6 and r.source != "unmarked"]
    sources = {r.source for r in confident}
    if len(sources) > 1:
        merged = Reading(**{k: v for k, v in best.__dict__.items()})
        merged.source = "multiple"
        merged.marks = sorted({m for r in confident for m in r.marks}, key=lambda c: PRIORITY.index(c) if c in PRIORITY else 99)
        merged.needs_review = True
        merged.reasons = list(best.reasons) + ["files_disagree"]
        return merged
    if best.source == "unmarked":
        marked = [r for r in usable if r.source != "unmarked"]
        if marked:
            return marked[0]
    return best
