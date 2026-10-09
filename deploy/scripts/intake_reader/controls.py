"""Boxes, circles and bullets next to the option labels (R5, R6, R6b).

A control is marked when it is clearly darker than its siblings on the same question, never
because it crosses a fixed number. Interior ink catches checks and filled dots; ink just outside
the rim catches checks that spill over; a swollen bounding box catches checks that merged with
the box; a ring of ink around the label catches circled text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

import numpy as np

from .anchors import Family
from .labels import LabelHit
from .page import components


@dataclass
class Control:
    code: str
    x0: int
    y0: int
    x1: int
    y1: int
    found: bool
    interior: float = 0.0
    outside: float = 0.0
    side: float = 0.0
    ring: float = 0.0  # ink around the label words (circled text)
    score: float = -9.0
    marked: bool = False
    reason: str = ""
    extra: dict = field(default_factory=dict)


def _clip(v: float, lo: int, hi: int) -> int:
    return int(max(lo, min(hi, round(v))))


def find_controls(ink: np.ndarray, hits: list[LabelHit], text_h: int, family: Family) -> list[Control]:
    """Locate the control for every label (component first, window fallback)."""
    if not hits:
        return []
    height, width = ink.shape
    labels, count, slices = components(ink) if ink.any() else (None, 0, [])
    th = max(8, int(text_h))
    lo, hi = family.size
    controls: list[Control] = []
    for hit in hits:
        x_ref = hit.anchor_x
        y_c = hit.cy
        glued = hit.prefix_px > 0
        if glued:
            # the control is glued into the first OCR word: look inside its leading part
            search_lo = hit.x0 - th * 0.6
            search_hi = hit.x0 + hit.prefix_px + th * 1.3
        else:
            search_lo = x_ref - th * 4.5
            search_hi = x_ref - max(1, th * 0.05)
        best = None
        best_rank = (False, -1)
        for index, sl in enumerate(slices, 1):
            if sl is None:
                continue
            cy0, cy1 = sl[0].start, sl[0].stop
            cx0, cx1 = sl[1].start, sl[1].stop
            w = cx1 - cx0
            h = cy1 - cy0
            if cx1 > search_hi or cx1 < search_lo:
                continue
            if cx0 < hit.left_limit + 1:
                continue
            center = (cy0 + cy1) / 2
            if abs(center - y_c) > th * 0.9:
                continue
            side = max(w, h)
            if side < lo * th or side > max(hi, 2.6) * th or w > 3.2 * th:
                continue
            if min(w, h) < max(3, 0.25 * th):
                continue
            pixels = int((labels[sl] == index).sum())
            if pixels < 0.6 * th:
                continue
            # Nearest wins, but a control-sized component beats a speck that happens to sit closer.
            real = max(0.3 if family.control == "bullet" else 0.7, lo) * th
            squarish = min(w, h) >= 0.55 * max(w, h)
            rank = (side >= real and min(w, h) >= 0.6 * real and squarish, cx1)
            if best is None or rank > best_rank:
                best_rank = rank
                best = (cx0, cy0, cx1, cy1)
        if best is not None and not best_rank[0]:
            best = None  # a speck is not a control; measure a window instead
        if best is not None:
            control = Control(hit.code, *best, True)
        elif glued:
            x0 = _clip(hit.x0 - th * 0.1, 0, width)
            x1 = _clip(hit.x0 + max(hit.prefix_px, th * 1.0) + th * 0.4, 0, width)
            y0 = _clip(y_c - th * 0.6, 0, height)
            y1 = _clip(y_c + th * 0.6, 0, height)
            control = Control(hit.code, x0, y0, x1, y1, False)
            x_ref = x1 + 1
        else:
            reach = 1.15 if family.control == "bullet" else 1.6
            x1 = _clip(x_ref - th * 0.15, 0, width)
            x0 = _clip(max(hit.left_limit + 1, x1 - th * reach), 0, width)
            y0 = _clip(y_c - th * 0.6, 0, height)
            y1 = _clip(y_c + th * 0.6, 0, height)
            control = Control(hit.code, x0, y0, x1, y1, False)
        _measure(ink, control, max(x_ref, control.x1 + 1), hit.left_limit, th)
        control.ring = _ring(ink, hit, th)
        control.extra["hit"] = hit
        controls.append(control)
    _align_columns(ink, controls, th)
    for control in controls:
        if control.y0 <= 1 or control.y1 >= height - 2:
            control.extra["edge"] = True  # clipped by the crop: half a circle looks filled
    return controls


def _align_columns(ink: np.ndarray, controls: list[Control], th: int) -> None:
    """Controls of one printed column share an x position. A control that strays from its column
    (a speck, a letter of a garbled label) is re-measured at the column position (R5).
    Columns are clusters of control x positions; a lone stray snaps to the nearest real column."""
    if len(controls) < 3:
        return
    height, width = ink.shape
    ordered = sorted(controls, key=lambda c: c.x0)
    clusters: list[list[Control]] = []
    for c in ordered:
        if clusters and c.x0 - clusters[-1][-1].x0 <= th * 6:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    big = [g for g in clusters if len(g) >= 3]
    if not big:
        return

    def realign(c: Control, med_x0: int, med_w: int) -> None:
        hit = c.extra.get("hit")
        y_c = hit.cy if hit is not None else (c.y0 + c.y1) / 2
        c.x0 = _clip(med_x0, 0, width)
        c.x1 = _clip(med_x0 + med_w, 0, width)
        c.y0 = _clip(y_c - max(med_w, th) * 0.55, 0, height)
        c.y1 = _clip(y_c + max(med_w, th) * 0.55, 0, height)
        c.found = False
        c.reason = "realigned"
        _measure(ink, c, c.x1 + 1, 0, th)

    stats = []
    for group in big:
        xs = sorted(c.x0 for c in group)
        med_x0 = xs[len(xs) // 2]
        sides = sorted(c.x1 - c.x0 for c in group if c.found) or [int(th * 1.1)]
        med_w = sides[len(sides) // 2]
        stats.append((med_x0, med_w))
        for c in group:
            if abs(c.x0 - med_x0) > th * 1.2:
                realign(c, med_x0, med_w)
    for group in clusters:
        if len(group) >= 3:
            continue
        for c in group:
            med_x0, med_w = min(stats, key=lambda s: abs(s[0] - c.x0))
            if abs(c.x0 - med_x0) <= th * 15:
                realign(c, med_x0, med_w)


def _measure(ink: np.ndarray, c: Control, x_ref: int, left_limit: int, th: int) -> None:
    height, width = ink.shape
    w = max(1, c.x1 - c.x0)
    h = max(1, c.y1 - c.y0)
    c.side = max(w, h) / th
    px = max(1, int(round(w * 0.22)))
    py = max(1, int(round(h * 0.22)))
    inner = ink[c.y0 + py : max(c.y0 + py + 1, c.y1 - py), c.x0 + px : max(c.x0 + px + 1, c.x1 - px)]
    c.interior = float(inner.mean()) if inner.size else 0.0
    grow = max(2, int(round(max(w, h) * 0.4)))
    gx0 = _clip(max(left_limit + 1, c.x0 - grow), 0, width)
    gx1 = _clip(min(x_ref - 1, c.x1 + grow), 0, width)
    gy0 = _clip(c.y0 - grow, 0, height)
    gy1 = _clip(c.y1 + grow, 0, height)
    if gx1 <= gx0 or gy1 <= gy0:
        c.outside = 0.0
        return
    window = ink[gy0:gy1, gx0:gx1]
    mask = np.ones(window.shape, dtype=bool)
    mask[c.y0 - gy0 : c.y1 - gy0, max(0, c.x0 - gx0) : max(0, c.x1 - gx0)] = False
    ring = window[mask]
    c.outside = float(ring.mean()) if ring.size else 0.0
    c.extra["window"] = (gx0, gy0, gx1, gy1)


def _ring(ink: np.ndarray, hit: LabelHit, th: int) -> float:
    """Ink around the label words, excluding the printed glyph boxes (circled text, R6b).

    A circle drawn around a label leaves ink above and below it (and usually on both sides); a
    binder line or a neighbouring word touches one side only, so the score is the second-best band.
    """
    height, width = ink.shape
    dx = int(th * 0.6)
    dy = int(th * 0.45)
    x0 = _clip(hit.x0 - dx, 0, width)
    x1 = _clip(hit.x1 + dx, 0, width)
    y0 = _clip(hit.y0 - dy, 0, height)
    y1 = _clip(hit.y1 + dy, 0, height)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    region = ink[y0:y1, x0:x1].copy()
    for word in hit.words:
        wx0 = _clip(word["x"] - 1 - x0, 0, region.shape[1])
        wx1 = _clip(word["x"] + word["w"] + 1 - x0, 0, region.shape[1])
        wy0 = _clip(word["y"] - 1 - y0, 0, region.shape[0])
        wy1 = _clip(word["y"] + word["h"] + 1 - y0, 0, region.shape[0])
        region[wy0:wy1, wx0:wx1] = False
    top_band = region[: max(1, hit.y0 - y0), :]
    bottom_band = region[min(region.shape[0] - 1, hit.y1 - y0) :, :]
    left_band = region[:, : max(1, hit.x0 - x0)]
    right_band = region[:, min(region.shape[1] - 1, hit.x1 - x0) :]
    bands = sorted((float(b.mean()) if b.size else 0.0) for b in (top_band, bottom_band, left_band, right_band))
    return bands[-2]


def score_controls(controls: list[Control], family: Family) -> None:
    """Relative decision: a control is marked when it stands out from its siblings."""
    if not controls:
        return
    n = len(controls)
    interiors = [c.interior for c in controls]
    outsides = [c.outside for c in controls]
    rings = [c.ring for c in controls]
    found_sides = [c.side for c in controls if c.found]
    med_i = median(interiors)
    med_o = median(outsides)
    med_r = median(rings)
    med_side = median(found_sides) if found_sides else 1.1
    bullet = family.control == "bullet"
    for c in controls:
        if n <= 2:
            if bullet:
                a = (c.interior - 0.50) / 0.2
                b = -9.0
            else:
                a = (c.interior - 0.30) / 0.15
                b = (c.outside - 0.25) / 0.15
            g = -9.0
        elif bullet:
            base = max(med_i, 0.12)
            a = (c.interior - base - 0.25) / 0.25
            b = -9.0
            g = -9.0
        else:
            a = (c.interior - med_i - 0.06) / 0.12
            if c.interior < 2.5 * max(med_i, 0.02):
                a = min(a, -0.01)
            b = (c.outside - med_o - 0.10) / 0.12
            if c.outside < 2.0 * max(med_o, 0.03):
                b = min(b, -0.01)
            g = -9.0
            if c.found and c.side >= 1.3 * med_side and (c.interior >= med_i + 0.03 or c.outside >= med_o + 0.05):
                g = (c.side / med_side - 1.3) / 0.3
        r = (c.ring - max(med_r, 0.02) - 0.08) / 0.08
        if c.ring < 0.12 or c.ring < 2.5 * max(med_r, 0.02):
            r = min(r, -0.01)
        c.score = max(a, b, g, r)
        if c.extra.get("edge"):
            c.score = -9.0
            c.reason = "edge"
        c.marked = c.score >= 0
        if c.marked:
            c.reason = {a: "fill", b: "spill", g: "swollen", r: "circled"}[max((a, b, g, r), key=lambda v: v)]
        c.extra.update({"a": round(a, 2), "b": round(b, 2), "g": round(g, 2), "r": round(r, 2)})
