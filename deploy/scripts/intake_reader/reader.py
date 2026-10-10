"""Read one intake PDF: find the hear question, read the marks, return a Reading.

Pipeline per file (R1, R2, R16):
  quick scan   each page (cap 20): native text when the PDF has a text layer, else a 100-dpi OCR
               after orientation detection; stop at the first page with the question.
  block        re-render that page at zoom 2.0 (3.0 for the tiny form), crop the question block,
               OCR word boxes, detect the family, match labels, stitch the next page when the
               option list runs off the page.
  marks        binarize, mask lines, find and score controls, read write-ins, decide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from . import anchors
from .anchors import GENERIC, Family, QUESTION_RE, detect_family
from .controls import find_controls, score_controls
from .decide import Reading, decide, merge_readings
from .labels import Layout, analyse, phrase_in_line
from .page import binarize, clean_for_ocr, mask_lines, median_text_height, normalize_contrast, ocr_words, render, rotate, upright
from .writein import detect as detect_writein
from .writein import stray_ink

QUICK_ZOOM = 1.4
MAX_PAGES = 20
from .anchors import OPTION_ANCHOR_RE  # noqa: E402
_MARK_GLYPH_RE = re.compile(r"^[@●•✓✔☑☒■▪xX✗✘]|^\(?[yYxX]\)?$|^\[[xX✓]\]$")


@dataclass
class PageScan:
    index: int
    angle: int
    how: str
    text: str
    native: bool
    has_question: bool
    quick_words: list[dict] = field(default_factory=list)
    question_y: int | None = None
    booking_y: int | None = None
    text_h: int = 12


@dataclass
class IntakeResult:
    reading: Reading
    text: str
    method: str
    pages: int
    scanned: list[PageScan]


def _question_in(words: list[dict], text: str) -> tuple[int | None, int | None, int]:
    """(question y, booking y, text height) from quick-scan words."""
    from .labels import group_lines

    th = median_text_height(words)
    lines = group_lines(words, th)
    q_y = None
    b_y = None
    for line in lines:
        if q_y is None and phrase_in_line(line.text, anchors.QUESTION_PHRASES, QUESTION_RE):
            q_y = line.y0
        if b_y is None and phrase_in_line(line.text, anchors.BOOKING_PHRASES, anchors.BOOKING_RE):
            b_y = line.y0
    if q_y is None and len(OPTION_ANCHOR_RE.findall(text)) >= 2:
        for line in lines:
            if OPTION_ANCHOR_RE.search(line.text):
                # the question sits right above the first option
                q_y = max(0, line.y0 - int(th * 1.6))
                break
    return q_y, b_y, th


def _downscale_to(big: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    from PIL import Image

    target = (shape[1], shape[0]) if big.shape[0] >= big.shape[1] else (shape[0], shape[1])
    return np.asarray(Image.fromarray(big).resize(target, Image.BILINEAR))


def quick_scan(doc, index: int, lang: str, angle_hint: int | None, force_words: bool = False) -> PageScan:
    page = doc[index]
    native = (page.get_text() or "").strip()
    if len(native) >= 50 and not force_words:
        has_q = bool(QUESTION_RE.search(native)) or len(OPTION_ANCHOR_RE.findall(native)) >= 2
        scan = PageScan(index, angle_hint or 0, "native", native, True, has_q)
        if not has_q:
            return scan
    gray = render(doc, index, QUICK_ZOOM)
    if force_words:
        # second look for pages where OSD misled the first pass: decide by readable words only
        big = render(doc, index, QUICK_ZOOM * 1.45)
        big, angle, how = upright(big, lang, trust_osd=False)
        gray = _downscale_to(big, gray.shape)
        how = "retry:" + how
    elif angle_hint is not None:
        gray, angle, how = rotate(gray, angle_hint), angle_hint, "hint"
    else:
        gray, angle, how = upright(gray, lang)
        if how.startswith("words:") and max(float(v) for v in how[6:].split("/")) < 0.08:
            # nothing readable at this size either way up: look again at twice the detail
            big = render(doc, index, QUICK_ZOOM * 1.45)
            big, angle, how = upright(big, lang)
            gray = _downscale_to(big, gray.shape)
            how = "big:" + how
    cleaned = clean_for_ocr(gray, 12)
    words = ocr_words(cleaned, lang)
    text = " ".join(w["text"] for w in words)
    q_y, b_y, th = _question_in(words, text)
    if q_y is None and len(OPTION_ANCHOR_RE.findall(text)) == 0:
        # layout analysis sometimes drops a narrow column; a uniform-block pass sees it
        words6 = ocr_words(cleaned, lang, psm=6)
        text6 = " ".join(w["text"] for w in words6)
        q6, b6, th6 = _question_in(words6, text6)
        if q6 is not None:
            words, text, q_y, b_y, th = words6, text6, q6, b6, th6
    scan = PageScan(index, angle, how, text, False, q_y is not None, words, q_y, b_y, th)
    return scan


def _crop_bounds(scan: PageScan, scale: float, height: int, th_quick: int) -> tuple[int, int]:
    th = th_quick * scale
    q = scan.question_y * scale
    # the booking question usually sits a few lines above; keep it even when its text was not read
    top = q - th * 9
    if scan.booking_y is not None and scan.booking_y < scan.question_y:
        top = min(top, scan.booking_y * scale - th * 1.5)
    bottom = q + max(th * 52, 820 * scale / 1.43)  # the quick text height underestimates small print; room under "Other"
    return int(max(0, top)), int(min(height, bottom))


def _hints(layout: Layout) -> set[str]:
    hints: set[str] = set()
    for hit in layout.hear:
        first = hit.words[0]
        # a glued "O", "D" or "[" is the printed box itself; only check-like glyphs are hints
        lead = re.sub(r"^[^A-Za-z]+", "", first["text"])[: max(0, len(first["text"]) - len(first["text"].lstrip()))]
        prefix_text = first["text"][: max(1, len(first["text"]) - len(hit.option.phrases[0]))] if hit.prefix_px > 0 else ""
        if _MARK_GLYPH_RE.search(first["text"]) or (hit.glyph is not None and _MARK_GLYPH_RE.search(hit.glyph["text"])) or (prefix_text and re.match(r"^[\(\[]?[xXvVyY✓✔]", prefix_text)):
            hints.add(hit.code)
            continue
        line = layout.lines[hit.line_index]
        prev = [w for w in line.words if w["x"] + w["w"] <= first["x"] and first["x"] - (w["x"] + w["w"]) < hit.text_h * 1.6]
        if prev and _MARK_GLYPH_RE.search(prev[-1]["text"]) and len(prev[-1]["text"]) <= 3:
            hints.add(hit.code)
    return hints


def read_block(doc, scan: PageScan, lang: str, want_debug: bool = False) -> Reading:
    """Read the marks on one page that carries the question."""
    zoom = 2.0
    family: Family = GENERIC
    for attempt in range(2):
        gray_full = rotate(render(doc, scan.index, zoom), scan.angle)
        scale = zoom / QUICK_ZOOM
        if scan.quick_words:
            top, bottom = _crop_bounds(scan, scale, gray_full.shape[0], scan.text_h)
        else:
            top, bottom = 0, gray_full.shape[0]
        gray = gray_full[top:bottom]
        words = ocr_words(clean_for_ocr(gray, 16), lang, psm=6)
        q_hint = scan.question_y * scale - top if scan.quick_words and scan.question_y is not None else None
        b_hint = scan.booking_y * scale - top if scan.quick_words and scan.booking_y is not None else None
        layout = analyse(words, GENERIC, question_y=q_hint, booking_y=b_hint, match=False)
        if layout.question_line is None:
            if attempt == 0 and scan.quick_words:
                # the quick scan found it; widen to the whole page once
                scan.quick_words = []
                continue
            reading = Reading(family="", page=scan.index)
            reading.source = "unreadable"
            reading.reasons = ["question_lost"]
            reading.debug = {"ocr_lines": [ln.text[:100] for ln in layout.lines[:40]], "attempt": attempt, "top": top}
            return reading
        family = detect_family(layout.block_text, scan.text)
        q_text = layout.lines[layout.question_line].text.lower()
        inline = sum(1 for w in ("doctor", "google", "social", "zocdoc", "walk", "flyer", "nearby") if w in q_text)
        if family.id in ("generic", "new_circle", "old_checkbox") and inline >= 2:
            family = anchors.TINY
        if family.zoom > zoom and attempt == 0:
            zoom = family.zoom
            continue
        break
    layout = analyse(words, family, question_y=q_hint, booking_y=b_hint)
    th = layout.text_h
    # the labels themselves give the surest text height; when the page median was pulled off by
    # handwriting or headers, analyse again with the label height
    label_h = sorted(h.text_h for h in layout.hear + layout.booking if not h.inferred)
    if len(label_h) >= 3:
        th_labels = label_h[len(label_h) // 2]
        if abs(th_labels - th) > 0.25 * th:
            layout = analyse(words, family, text_h=th_labels, question_y=q_hint, booking_y=b_hint)
            th = layout.text_h
    block_cut = False
    # R2: the option list may continue on the next page.
    last_codes = {hit.code for hit in layout.hear}
    expect_last = family.options[-1].code if family.options else "other"
    if layout.end_line is None and expect_last not in last_codes and bottom >= gray_full.shape[0] - th and scan.index + 1 < len(doc):
        nxt = rotate(render(doc, scan.index + 1, zoom), scan.angle)
        take = int(nxt.shape[0] * 0.4)
        stitched = np.vstack([gray, np.full((6, gray.shape[1]), 255, dtype=np.uint8), nxt[:take]])
        words2 = ocr_words(clean_for_ocr(stitched, 16), lang, psm=6)
        layout2 = analyse(words2, family, question_y=q_hint, booking_y=b_hint)
        if layout2.question_line is not None and len(layout2.hear) >= len(layout.hear):
            gray, words, layout = stitched, words2, layout2
            th = layout.text_h
        else:
            block_cut = expect_last not in last_codes
    elif layout.end_line is None and expect_last not in last_codes and len(layout.hear) < 3:
        block_cut = True
    level = normalize_contrast(gray)
    raw_ink = binarize(level, 165)
    ink = mask_lines(raw_ink, th)
    erased = raw_ink & ~ink  # printed rules and binder lines, painted out before strip OCR
    locate = mask_lines(binarize(level, 205), th)  # faint print still shows its boxes; 220 lets paper shading join them
    # one search over both questions: the booking circles share the hear column, so a booking
    # label whose own circle is hidden by a check still gets measured at the right place
    all_controls = find_controls(ink, layout.hear + layout.booking, th, family, locate, words)
    hear_controls = [c for c in all_controls if c.extra["hit"].group == "hear"]
    booking_controls = [c for c in all_controls if c.extra["hit"].group == "booking"]
    score_controls(hear_controls, family)
    score_controls(booking_controls, family)
    writeins = []
    tries: list = []
    all_hits = layout.hear + layout.booking
    for hit in layout.hear:
        kind = hit.option.writein
        if not kind:
            continue
        bounds = _neighbour_bounds(hit, all_hits, th)
        found = detect_writein(ink, level, words, hit, kind, th, lang, bounds=bounds, erase=erased, trace=tries, family=family)
        if found is not None:
            writeins.append(found)
    reading = decide(
        family.id,
        scan.index,
        hear_controls,
        booking_controls,
        writeins,
        _hints(layout),
        block_found=True,
        block_cut=block_cut,
    )
    stray = None
    if layout.question_line is not None and reading.source not in ("unreadable", "no_question"):
        q_line = layout.lines[layout.question_line]
        last = max((h.y1 for h in layout.hear), default=int(q_line.y1))
        region_ink = np.zeros_like(ink)
        y_lo = max(0, int(q_line.y0))
        y_hi = min(ink.shape[0], int(last + th * 1.5))
        region_ink[y_lo:y_hi] = ink[y_lo:y_hi]
        areas = [tuple(t[2]) for t in tries] if tries else []
        stray = stray_ink(region_ink, words, hear_controls + booking_controls, areas, layout.hear + layout.booking, th)
        if stray is not None:
            reading.reasons.append("stray_ink")
            reading.needs_review = True
    reading.debug = {
        "block_text": layout.block_text[:300],
        "layout_lines": [(i, ("Q" if i == layout.question_line else "B" if i == layout.booking_line else "E" if i == layout.end_line else " "), ln.text[:90]) for i, ln in enumerate(layout.lines)],
        "zoom": zoom,
        "angle": scan.angle,
        "how": scan.how,
        "top": top,
        "text_h": th,
        "labels": [(h.code, h.group, h.x0, h.y0, h.x1, h.y1, round(h.score, 2), h.left_limit, h.right_limit, " ".join(w["text"] for w in h.words)) for h in layout.hear + layout.booking],
        "attempt": attempt,
        "controls": [
            (c.code, c.x0, c.y0, c.x1, c.y1, c.found, round(c.interior, 3), round(c.outside, 3), round(c.ring, 3), round(c.score, 2), c.marked, c.reason)
            for c in hear_controls + booking_controls
        ],
        "writeins": [(w.code, w.kind, w.text, round(w.conf, 1), w.mapped, w.ink, w.area) for w in writeins],
        "places": [(c.code, c.extra.get("place", ""), c.extra.get("overlap"), c.extra.get("pixels")) for c in hear_controls + booking_controls],
        "writein_tries": tries,
        "notes": layout.notes[:20],
        "stray": stray,
    }
    if want_debug:
        reading.debug["gray"] = gray
        reading.debug["cands"] = [(("hear:" if c in hear_controls else "booking:") + c.code, c.extra.get("cands", [])) for c in hear_controls + booking_controls]
        reading.debug["_hits"] = layout.hear + layout.booking
        reading.debug["_level"] = level
    return reading


def _neighbour_bounds(hit, hits, th: int) -> tuple[int | None, int | None, int | None]:
    """Bottom of the printed label above, top of the one below in the same column, and the start
    of the next label on the same line."""
    above = None
    below = None
    right_stop = None
    for other in hits:
        if other is hit:
            continue
        if abs(other.cy - hit.cy) <= th * 0.6 and other.x0 > hit.x1:
            right_stop = other.x0 if right_stop is None else min(right_stop, other.x0)
    for other in hits:
        if other is hit:
            continue
        same_column = abs(other.x0 - hit.x0) <= th * 8 or (other.x0 < hit.x1 and other.x1 > hit.x0)
        if not same_column:
            continue
        if other.cy < hit.cy - th * 0.6:
            above = other.y1 if above is None else max(above, other.y1)
        elif other.cy > hit.cy + th * 0.6:
            below = other.y0 if below is None else min(below, other.y0)
    return above, below, right_stop


def read_intake(path: str, lang: str = "eng+spa", max_pages: int = MAX_PAGES, want_debug: bool = False) -> IntakeResult:
    import fitz

    doc = fitz.open(path)
    scanned: list[PageScan] = []
    readings: list[Reading] = []
    texts: list[str] = []
    kinds: set[str] = set()
    angle_hint: int | None = None
    try:
        limit = min(len(doc), max_pages)
        for index in range(limit):
            scan = quick_scan(doc, index, lang, angle_hint)
            scanned.append(scan)
            if not scan.native and scan.how != "hint":
                angle_hint = scan.angle
            if scan.text.strip():
                kinds.add("native" if scan.native else "ocr")
            if index < 3 or scan.has_question:
                texts.append(scan.text)
            if not scan.has_question:
                continue
            if scan.native and not scan.quick_words:
                # text layer found the question; we still need an image scan for coordinates
                gray = render(doc, index, QUICK_ZOOM)
                gray, angle, how = (rotate(gray, angle_hint), angle_hint, "hint") if angle_hint is not None else upright(gray, lang)
                words = ocr_words(clean_for_ocr(gray, 12), lang)
                q_y, b_y, th = _question_in(words, " ".join(w["text"] for w in words))
                scan.angle, scan.how, scan.quick_words, scan.question_y, scan.booking_y, scan.text_h = angle, how, words, q_y, b_y, th
                if q_y is None:
                    scan.quick_words = []
            reading = read_block(doc, scan, lang, want_debug)
            readings.append(reading)
            if reading.source not in ("unreadable", "no_question"):
                break  # the question was read on this page; later pages are other documents
            if len(readings) >= 2:
                break
        if all(r.source in ("unreadable", "no_question") for r in readings) and any(not s.native for s in scanned):
            # R1 fallback: a weak OSD verdict may have turned the form upside down; look again at
            # the first pages deciding the orientation by readable words only
            for index in range(min(2, len(doc))):
                scan = quick_scan(doc, index, lang, None, force_words=True)
                if not scan.has_question:
                    continue
                scanned[index] = scan
                texts.append(scan.text)
                reading = read_block(doc, scan, lang, want_debug)
                if reading.source not in ("unreadable", "no_question"):
                    readings = [reading]
                    break
    finally:
        doc.close()
    if readings:
        reading = merge_readings(readings)
    else:
        reading = Reading(source="no_question", reasons=["no_question"], confidence=0.9, needs_review=False)
        if not any(s.text.strip() for s in scanned):
            reading.source = "unreadable"
            reading.reasons = ["ocr_empty"]
            reading.confidence = 0.0
            reading.needs_review = True
    if "ocr" in kinds and "native" in kinds:
        method = "mixed"
    elif "ocr" in kinds:
        method = "ocr"
    elif "native" in kinds:
        method = "native_text"
    else:
        method = "empty"
    return IntakeResult(reading, "\n".join(texts), method, len(scanned), scanned)
