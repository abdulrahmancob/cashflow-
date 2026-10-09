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


@dataclass
class _Cand:
    x0: int
    y0: int
    x1: int
    y1: int
    real: bool  # control-sized, squarish and box-like (ink on three or four sides)
    merged: bool  # recovered from a component that ran into the label
    pixels: int


def _refine_box(mask: np.ndarray, cx0: int, cy0: int, cx1: int, cy1: int, nominal: int) -> tuple[int, int, int, int] | None:
    """Tight bounds of a printed box glued to its label: the first full-height column after the
    left edge is the right edge; rows covered between the edges are the top and bottom."""
    sub = mask[cy0:cy1, cx0:cx1]
    if sub.size == 0 or sub.shape[0] < 4:
        return None
    col_cov = sub.mean(axis=0)
    lo = max(1, int(nominal * 0.5))
    hi = min(sub.shape[1], int(nominal * 1.6) + 1)
    right = None
    for x in range(lo, hi):
        if col_cov[x] >= 0.55:
            right = x
            break
    if right is None:
        return None
    row_cov = sub[:, : right + 1].mean(axis=1)
    rows = np.where(row_cov >= 0.55)[0]
    if len(rows) < 2 or rows[-1] - rows[0] < nominal * 0.5:
        return None
    return cx0, cy0 + int(rows[0]), cx0 + right + 1, cy0 + int(rows[-1]) + 1


def _window(hit: LabelHit, th: int, words: list[dict] | None) -> tuple[float, float]:
    """x range where this label's control can sit. When OCR attached only the tail of a label
    ("referral" without "Doctor"), the loose words just left of it on the same line are part of
    the label and the window starts before them."""
    x_ref = hit.anchor_x
    if hit.prefix_px > 0:
        return hit.x0 - th * 0.6, hit.x0 + hit.prefix_px + th * 1.3
    start = x_ref
    if words and hit.left_limit == 0:
        same_line = [
            w for w in words
            if abs((w["y"] + w["h"] / 2) - hit.cy) <= th * 0.5 and w["x"] + w["w"] <= hit.x0 + 1 and w["x"] >= hit.x0 - th * 5.0
            and len(w.get("text", "").strip()) >= 1
        ]
        if same_line:
            start = min(start, min(w["x"] for w in same_line))
    return start - th * 5.0, x_ref - max(1, th * 0.05)


def _candidates(source, labels, slices, hit: LabelHit, th: int, family: Family, words: list[dict] | None = None) -> list[_Cand]:
    """Components near the label that could be its control, with a box-likeness verdict."""
    bullet = family.control == "bullet"
    lo, hi = family.size
    x_ref = hit.anchor_x
    y_c = hit.cy
    search_lo, search_hi = _window(hit, th, words)
    nominal = int(round(th * (0.6 if bullet else 1.1)))
    out: list[_Cand] = []
    for index, sl in enumerate(slices, 1):
        if sl is None:
            continue
        cy0, cy1 = sl[0].start, sl[0].stop
        cx0, cx1 = sl[1].start, sl[1].stop
        w = cx1 - cx0
        h = cy1 - cy0
        center = (cy0 + cy1) / 2
        if abs(center - y_c) > th * 0.9:
            continue
        if cx0 < hit.left_limit + 1:
            continue
        merged = False
        if cx1 > search_hi:
            # a control touching or glued to its label is one component with the text:
            # keep it when it starts where a control would and runs into the label
            too_wide = w > 2.0 * th if bullet else False
            if cx0 < x_ref - th * 0.3 and cx0 >= search_lo and w > h * 1.3 and h <= 2.0 * th and not too_wide:
                merged = True
                refined = None if bullet else _refine_box(source, cx0, cy0, cx1, cy1, nominal)
                if refined is not None:
                    cx0, cy0, cx1, cy1 = refined
                else:
                    cx1 = min(cx1, cx0 + max(h, nominal))
                w = cx1 - cx0
                h = cy1 - cy0
            else:
                continue
        if cx1 < search_lo:
            continue
        side = max(w, h)
        if side < lo * th or side > max(hi, 2.6) * th or w > 3.2 * th:
            continue
        if min(w, h) < max(3, 0.25 * th):
            continue
        pixels = int((labels[sl] == index).sum())
        if pixels < 0.6 * th:
            continue
        if bullet and w > 1.0 * th:
            cx1 = cx0 + max(4, int(th * 0.7))  # a filled bullet welded to its label
            w = cx1 - cx0
            merged = True
        real_side = max(0.3 if bullet else 0.7, lo) * th
        squarish = min(w, h) >= (0.55 if bullet else 0.72) * max(w, h)
        boxlike = bullet or _boxlike(source, cx0, cy0, cx1, cy1)
        real = side >= real_side and min(w, h) >= 0.6 * real_side and squarish and boxlike
        out.append(_Cand(cx0, cy0, cx1, cy1, real, merged, pixels))
    return out


def _column_vote(cands_list: list[list[_Cand]], th: int) -> list[tuple[int, int, int]]:
    """Printed columns of controls: x positions shared by at least two labels' real candidates,
    as (x0, side, members), biggest first. Letters next to glued boxes also line up, so a tie
    goes to the left."""
    xs: list[tuple[int, int, int]] = []
    for i, cands in enumerate(cands_list):
        for c in cands:
            if c.real:
                xs.append((c.x0, i, max(c.x1 - c.x0, c.y1 - c.y0)))
    if len(xs) < 2:
        return []
    xs.sort()
    clusters: list[tuple[list[int], set[int], list[int]]] = []
    for x, i, side in xs:
        if clusters and x - clusters[-1][0][-1] <= th * 0.6 and x - clusters[-1][0][0] <= th * 1.0:
            clusters[-1][0].append(x)
            clusters[-1][1].add(i)
            clusters[-1][2].append(side)
        else:
            clusters.append(([x], {i}, [side]))
    out = []
    for xs_c, members, sides in clusters:
        if len(members) < 2:
            continue
        xs_b = sorted(xs_c)
        sides_b = sorted(sides)
        out.append((xs_b[len(xs_b) // 2], sides_b[len(sides_b) // 2], len(members)))
    out.sort(key=lambda c: (-c[2], c[0]))
    return out


def _cluster_for(hit: LabelHit, clusters: list[tuple[int, int, int]], th: int, words: list[dict] | None) -> tuple[int, int, int] | None:
    lo, hi = _window(hit, th, words)
    for cluster in clusters:
        if lo - th * 0.3 <= cluster[0] <= hi:
            return cluster
    # a label whose first words OCR lost sits further right than its siblings: a column shared by
    # three or more of them still applies when it is within reach
    for cluster in clusters:
        if cluster[2] >= 3 and lo - th * 4.5 <= cluster[0] <= hi:
            return cluster
    return None


def find_controls(
    ink: np.ndarray,
    hits: list[LabelHit],
    text_h: int,
    family: Family,
    locate: np.ndarray | None = None,
    words: list[dict] | None = None,
) -> list[Control]:
    """Locate the control for every label (component first, window fallback).

    `locate` is a lighter binarisation used only to find faint printed boxes and circles; marks
    are always measured on `ink`. Candidates are voted per printed column: the x position shared
    by most labels is the column of boxes, and a label whose own box was not recognised (a check
    ran into it) is measured at that column. When most boxes of a group cannot be found (very
    faint print) the fallback window spans the whole gutter so a pen check far from the label
    still counts.
    """
    hits = [h for h in hits if h.option.control]
    if not hits:
        return []
    height, width = ink.shape
    source = locate if locate is not None else ink
    labels, count, slices = components(source) if source.any() else (None, 0, [])
    th = max(8, int(text_h))
    cands_list = [_candidates(source, labels, slices, hit, th, family, words) if labels is not None else [] for hit in hits]
    # printed columns of controls voted over every label; the tiny form is one row and has none
    clusters = _column_vote(cands_list, th) if family.id != "tiny" else []
    used_cluster = False
    controls: list[Control | None] = []
    for hit, cands in zip(hits, cands_list):
        cluster = _cluster_for(hit, clusters, th, words)
        used_cluster = used_cluster or cluster is not None
        chosen: _Cand | None = None
        reason = ""
        real = [c for c in cands if c.real]
        if cluster is not None:
            cx, side, _members = cluster
            near = [c for c in real if abs(c.x0 - cx) <= th * 0.8]
            if near:
                chosen = min(near, key=lambda c: (abs(c.x0 - cx), -c.x1))
            elif real:
                # an indented sub-option keeps its own box when nothing is printed at the column
                y0 = _clip(hit.cy - side * 0.55, 0, height)
                y1 = _clip(hit.cy + side * 0.55, 0, height)
                x0 = _clip(cx, 0, width)
                x1 = _clip(cx + side, 0, width)
                there = float(source[y0:y1, x0:x1].mean()) if y1 > y0 and x1 > x0 else 0.0
                if there < 0.04:
                    chosen = max(real, key=lambda c: c.x1)
                    reason = "off_column"
        elif real:
            chosen = max(real, key=lambda c: c.x1)  # nearest to the label
        if chosen is not None:
            control = Control(hit.code, chosen.x0, chosen.y0, chosen.x1, chosen.y1, True)
            control.extra["merged"] = chosen.merged
            control.reason = reason
        else:
            control = None
        if control is not None:
            control.extra["cands"] = [(c.x0, c.y0, c.x1, c.y1, int(c.real), int(c.merged)) for c in cands[:6]]
        controls.append(control)
    found_n = sum(1 for c in controls if c is not None)
    wide = found_n * 2 < len(controls)  # most boxes invisible: scan the whole gutter
    for index, (hit, cands) in enumerate(zip(hits, cands_list)):
        if controls[index] is not None:
            continue
        y_c = hit.cy
        x_ref = hit.anchor_x
        glued = hit.prefix_px > 0
        cluster = _cluster_for(hit, clusters, th, words)
        if cluster is not None:
            cx, side, _members = cluster
            x0 = _clip(cx, 0, width)
            x1 = _clip(cx + side, 0, width)
            y0 = _clip(y_c - max(side, th) * 0.55, 0, height)
            y1 = _clip(y_c + max(side, th) * 0.55, 0, height)
            control = Control(hit.code, x0, y0, x1, y1, False)
            control.reason = "column"
        else:
            if glued:
                x0 = _clip(hit.x0 - th * 0.1, 0, width)
                if hit.prefix_px >= th * 0.5:
                    x1 = _clip(hit.x0 + hit.prefix_px + th * 0.15, 0, width)
                else:
                    x1 = _clip(hit.x0 + max(hit.prefix_px, th * 0.9), 0, width)
            else:
                reach = 1.15 if family.control == "bullet" else (4.5 if wide else 1.6)
                x1 = _clip(x_ref - th * 0.15, 0, width)
                x0 = _clip(max(hit.left_limit + 1, x1 - th * reach), 0, width)
            y0 = _clip(y_c - th * 0.6, 0, height)
            y1 = _clip(y_c + th * 0.6, 0, height)
            control = Control(hit.code, x0, y0, x1, y1, False)
            control.extra["wide"] = wide
        control.extra["cands"] = [(c.x0, c.y0, c.x1, c.y1, int(c.real), int(c.merged)) for c in cands[:6]]
        controls[index] = control
    for control, hit in zip(controls, hits):
        x_ref = hit.anchor_x
        if hit.prefix_px > 0 and not control.found:
            x_ref = control.x1 + 1
        _measure(ink, control, max(x_ref, control.x1 + 1), hit.left_limit, th)
        control.ring = _ring(ink, hit, th, words)
        control.extra["hit"] = hit
    if not used_cluster and family.id != "tiny":
        _align_columns(ink, controls, th)
    for control in controls:
        if control.y0 <= 1 or control.y1 >= height - 2:
            control.extra["edge"] = True  # clipped by the crop: half a circle looks filled
    return controls


def _align_columns(ink: np.ndarray, controls: list[Control], th: int) -> None:
    """Controls of one printed column share an x position. A control that strays from its column
    (a speck, a letter of a garbled label) is re-measured at the column position (R5).
    Columns are defined by the controls that were actually found; fallback windows never define
    a column, they only get pulled to one."""
    found = [c for c in controls if c.found]
    if len(found) < 2:
        return
    height, width = ink.shape
    ordered = sorted(found, key=lambda c: c.x0)
    clusters: list[list[Control]] = []
    for c in ordered:
        if clusters and c.x0 - clusters[-1][-1].x0 <= th * 6:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    stats = []
    for group in clusters:
        if len(group) < 2:
            continue
        xs = sorted(c.x0 for c in group)
        sides = sorted(c.x1 - c.x0 for c in group)
        stats.append((xs[len(xs) // 2], sides[len(sides) // 2], len(group)))
    if not stats:
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

    for c in controls:
        med_x0, med_w, size = min(stats, key=lambda s: abs(s[0] - c.x0))
        if abs(c.x0 - med_x0) <= th * 1.2:
            continue
        if c.found and size < 3:
            continue  # two found controls are not enough evidence to overrule a third
        if abs(c.x0 - med_x0) <= th * 15:
            realign(c, med_x0, med_w)


def _boxlike(mask: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> bool:
    """A printed box or circle has ink along the middle of all four sides of its bounding box;
    a letter or a speck does not."""
    w = x1 - x0
    h = y1 - y0
    if w < 6 or h < 6:
        return False
    band = max(2, min(w, h) // 5)
    mx0, mx1 = x0 + int(w * 0.3), x0 + int(w * 0.7) + 1
    my0, my1 = y0 + int(h * 0.3), y0 + int(h * 0.7) + 1
    sides = (
        mask[y0 : y0 + band, mx0:mx1].any(axis=0).mean(),
        mask[y1 - band : y1, mx0:mx1].any(axis=0).mean(),
        mask[my0:my1, x0 : x0 + band].any(axis=1).mean(),
        mask[my0:my1, x1 - band : x1].any(axis=1).mean(),
    )
    return sum(1 for s in sides if s >= 0.6) >= 3


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


def _ring(ink: np.ndarray, hit: LabelHit, th: int, words: list[dict] | None = None) -> float:
    """Ink around the label words, excluding every printed word box in the region (circled text,
    R6b). A pen circle drawn around a label leaves ink on all four sides; an underline, a binder
    line or the neighbouring row touches one or two sides, so the score is the weakest side.
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
    boxes = list(hit.words)
    if words:
        boxes += [w for w in words if w["x"] < x1 and w["x"] + w["w"] > x0 and w["y"] < y1 and w["y"] + w["h"] > y0]
    for word in boxes:
        wx0 = _clip(word["x"] - 1 - x0, 0, region.shape[1])
        wx1 = _clip(word["x"] + word["w"] + 1 - x0, 0, region.shape[1])
        wy0 = _clip(word["y"] - 1 - y0, 0, region.shape[0])
        wy1 = _clip(word["y"] + word["h"] + 1 - y0, 0, region.shape[0])
        region[wy0:wy1, wx0:wx1] = False
    top_band = region[: max(1, hit.y0 - y0), :]
    bottom_band = region[min(region.shape[0] - 1, hit.y1 - y0) :, :]
    left_band = region[:, : max(1, hit.x0 - x0)]
    right_band = region[:, min(region.shape[1] - 1, hit.x1 - x0) :]
    bands = [(float(b.mean()) if b.size else 0.0) for b in (top_band, bottom_band, left_band, right_band)]
    return min(bands)


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
            if c.found and not c.extra.get("merged") and c.side >= 1.3 * med_side and (c.interior >= med_i + 0.03 or c.outside >= med_o + 0.05):
                g = (c.side / med_side - 1.3) / 0.3
        r = (c.ring - max(med_r, 0.02) - 0.05) / 0.06
        if c.ring < 0.07 or c.ring < 2.5 * max(med_r, 0.015):
            r = min(r, -0.01)
        c.score = max(a, b, g, r)
        if c.extra.get("edge"):
            c.score = -9.0
            c.reason = "edge"
        c.marked = c.score >= 0
        if c.marked:
            c.reason = {a: "fill", b: "spill", g: "swollen", r: "circled"}[max((a, b, g, r), key=lambda v: v)]
        c.extra.update({"a": round(a, 2), "b": round(b, 2), "g": round(g, 2), "r": round(r, 2)})
