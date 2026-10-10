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
    index: int = 0  # component label


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


def _window(hit: LabelHit, th: int, words: list[dict] | None, family: Family | None = None) -> tuple[float, float]:
    """x range where this label's control can sit. When OCR attached only the tail of a label
    ("referral" without "Doctor"), the loose words just left of it on the same line are part of
    the label and the window starts before them. On the one-row tiny form the box sits right
    before its label, so the window is short and never reaches the previous option's box."""
    x_ref = hit.anchor_x
    if hit.prefix_px > 0:
        return hit.x0 - th * 0.6, hit.x0 + hit.prefix_px + th * 1.3
    if family is not None and family.id == "tiny":
        return x_ref - th * 2.5, x_ref - max(1, th * 0.05)
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
    search_lo, search_hi = _window(hit, th, words, family)
    nominal = int(round(th * (0.6 if bullet else 1.1)))
    out: list[_Cand] = []
    for index, sl in slices:
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
        if boxlike and family.control == "box" and not merged:
            boxlike = _straight_sides(source, cx0, cy0, cx1, cy1)
        if merged and family.control == "circle":
            boxlike = False  # a circle glued to its label cannot be cut free; the column places it
        real = side >= real_side and min(w, h) >= 0.6 * real_side and squarish and boxlike
        out.append(_Cand(cx0, cy0, cx1, cy1, real, merged, pixels, index))
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


def _offset_vote(hits: list[LabelHit], cands_list: list[list[_Cand]], th: int) -> tuple[int, int, int] | None:
    """(offset, side, members): the distance from a label's text to its own box shared by at
    least two labels. Used on the tiny form, where boxes sit in a row rather than a column."""
    pairs: list[tuple[int, int]] = []
    for hit, cands in zip(hits, cands_list):
        real = [c for c in cands if c.real]
        if not real:
            continue
        c = max(real, key=lambda c: c.x1)
        pairs.append((hit.anchor_x - c.x0, max(c.x1 - c.x0, c.y1 - c.y0)))
    if len(pairs) < 2:
        return None
    pairs.sort()
    best: list[tuple[int, int]] = []
    for i in range(len(pairs)):
        group = [p for p in pairs if abs(p[0] - pairs[i][0]) <= th * 0.6]
        if len(group) > len(best):
            best = group
    if len(best) < 2:
        return None
    offs = sorted(p[0] for p in best)
    sides = sorted(p[1] for p in best)
    return offs[len(offs) // 2], sides[len(sides) // 2], len(best)


def _cluster_for(
    hit: LabelHit, clusters: list[tuple[int, int, int]], th: int, words: list[dict] | None, family: Family | None = None
) -> tuple[int, int, int] | None:
    lo, hi = _window(hit, th, words, family)
    for cluster in clusters:
        if lo - th * 0.3 <= cluster[0] <= hi:
            return cluster
    # a label whose first words OCR lost sits further right than its siblings, and a label whose
    # OCR box swallowed the check mark starts left of its own box: a column shared by three or
    # more labels still applies when it is within reach
    for cluster in clusters:
        if cluster[2] >= 3 and lo - th * 4.5 <= cluster[0] <= max(hi, hit.x0 + th * 1.2):
            return cluster
    return None


def _cluster_offset(hits: list[LabelHit], cands_list: list[list[_Cand]], cluster: tuple[int, int, int], th: int) -> int | None:
    """Median distance from the label text to the box for the labels that sit on this column."""
    offs = []
    for hit, cands in zip(hits, cands_list):
        for c in cands:
            if c.real and abs(c.x0 - cluster[0]) <= th * 0.8:
                offs.append(hit.anchor_x - c.x0)
                break
    if not offs:
        return None
    offs.sort()
    return offs[len(offs) // 2]


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
    if labels is not None:
        # paper shading and text produce thousands of components; only control-sized ones matter
        _lo, _hi = family.size
        max_side = max(_hi, 2.6) * th
        sized = [
            (index, sl)
            for index, sl in enumerate(slices, 1)
            if sl is not None
            and (sl[0].stop - sl[0].start) <= max(2.0 * th, max_side)
            and (sl[1].stop - sl[1].start) <= max(3.2 * th, 8 * th)
            and min(sl[0].stop - sl[0].start, sl[1].stop - sl[1].start) >= max(3, 0.25 * th)
        ]
    else:
        sized = []
    cands_list = [_candidates(source, labels, sized, hit, th, family, words) if labels is not None else [] for hit in hits]
    # printed columns of controls voted over every label; the tiny form is one row, so its boxes
    # are voted by their distance to the label instead
    tiny = family.id == "tiny"
    clusters = _column_vote(cands_list, th) if not tiny else []
    offsets = {c: _cluster_offset(hits, cands_list, c, th) for c in clusters}
    row_vote = _offset_vote(hits, cands_list, th) if tiny else None
    main = clusters[0] if clusters else None
    used_cluster = False
    controls: list[Control | None] = []

    def blank_at(x0: float, side: int, cy: float) -> bool:
        ya = _clip(cy - side * 0.55, 0, height)
        yb = _clip(cy + side * 0.55, 0, height)
        xa = _clip(x0, 0, width)
        xb = _clip(x0 + side, 0, width)
        there = float(source[ya:yb, xa:xb].mean()) if yb > ya and xb > xa else 0.0
        return there < 0.04

    def on_text_at(wx: float, side: int, y_c: float) -> bool:
        # a window placed by geometry must not sit on printed words
        if not words:
            return False
        for w in words:
            if len(w.get("text", "").strip()) < 2:
                continue
            ox = max(0, min(wx + side, w["x"] + w["w"]) - max(wx, w["x"]))
            oy = max(0, min(y_c + side * 0.5, w["y"] + w["h"]) - max(y_c - side * 0.5, w["y"]))
            if ox * oy > 0.3 * side * side:
                return True
        return False

    for hit, cands in zip(hits, cands_list):
        cluster = _cluster_for(hit, clusters, th, words, family)
        used_cluster = used_cluster or cluster is not None
        chosen: _Cand | None = None
        reason = ""
        real = [c for c in cands if c.real]

        def on_text(wx: float, side: int, _y=hit.cy) -> bool:
            return on_text_at(wx, side, _y)

        if cluster is not None:
            cx, side, _members = cluster
            near = [c for c in real if abs(c.x0 - cx) <= th * 0.8]
            if near:
                chosen = min(near, key=lambda c: (abs(c.x0 - cx), abs((c.y0 + c.y1) / 2 - hit.cy), abs(max(c.x1 - c.x0, c.y1 - c.y0) - side)))
            elif real and blank_at(cx, side, hit.cy):
                # an indented sub-option keeps its own box when nothing is printed at the column,
                # provided the box sits at the column's label-to-box distance (a letter does not)
                off = offsets.get(cluster)
                fitting = [
                    c for c in real
                    if (off is None or abs((hit.anchor_x - c.x0) - off) <= th * 0.8) and not on_text(c.x0, max(c.x1 - c.x0, c.y1 - c.y0))
                ]
                if fitting:
                    chosen = max(fitting, key=lambda c: c.x1)
                    reason = "off_column"
        elif real:
            if row_vote is not None:
                off, side, _n = row_vote
                near = [c for c in real if abs((hit.anchor_x - c.x0) - off) <= th * 0.8]
                chosen = min(near, key=lambda c: abs((hit.anchor_x - c.x0) - off)) if near else None
            else:
                chosen = max(real, key=lambda c: c.x1)  # nearest to the label
        if chosen is not None:
            control = Control(hit.code, chosen.x0, chosen.y0, chosen.x1, chosen.y1, True)
            control.extra["merged"] = chosen.merged
            control.extra["pixels"] = chosen.pixels
            control.extra["place"] = reason or ("near" if cluster is not None else ("row" if row_vote is not None else "nearest"))
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
        cluster = _cluster_for(hit, clusters, th, words, family)
        offset_window = None

        def on_text(wx: float, side: int, _y=y_c) -> bool:
            return on_text_at(wx, side, _y)

        if cluster is not None:
            cx, side, _members = cluster
            off = offsets.get(cluster)
            column_blank = blank_at(cx, side, y_c) and blank_at(cx, side, y_c - th * 0.5) and blank_at(cx, side, y_c + th * 0.5)
            if off is not None and column_blank and hit.anchor_x - off > cx + th * 0.8 and not glued and not on_text(hit.anchor_x - off, side):
                # nothing printed at the column: an indented row keeps the column's label offset
                offset_window = (hit.anchor_x - off, side, "offset")
            else:
                offset_window = (cx, side, "column")
        elif main is not None and offsets.get(main) is not None and not glued:
            # no column in reach, but the sheet has one: its label-to-box distance places the box
            cx, side, _members = main
            off = offsets[main]
            if hit.anchor_x - off > hit.left_limit and not on_text(hit.anchor_x - off, side):
                offset_window = (hit.anchor_x - off, side, "offset")
        elif row_vote is not None and not glued:
            off, side, _n = row_vote
            if hit.anchor_x - off > hit.left_limit and not on_text(hit.anchor_x - off, side):
                offset_window = (hit.anchor_x - off, side, "offset")
        if offset_window is not None:
            wx, side, reason = offset_window
            x0 = _clip(wx, 0, width)
            x1 = _clip(wx + side, 0, width)
            half = max(side, th * 0.5) * 0.55  # a bullet column keeps bullet-sized windows
            y0 = _clip(y_c - half, 0, height)
            y1 = _clip(y_c + half, 0, height)
            snapped = _snap(labels, slices, x0, y0, x1, y1, side) if labels is not None else None
            if snapped is not None:
                x0, y0, x1, y1 = snapped  # the printed control sits a little off the label's row
            control = Control(hit.code, x0, y0, x1, y1, False)
            control.reason = reason
            control.extra["place"] = reason
            if labels is not None:
                # a check that merged with the box into one large component: the component
                # covering this window is far bigger than a box but stops before the label
                grown = _overlap_growth(labels, slices, x0, y0, x1, y1, side, hit.anchor_x, th)
                if grown is not None:
                    control.extra["overlap"] = grown
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
            control.extra["place"] = "glued" if glued else "loose"
        control.extra["cands"] = [(c.x0, c.y0, c.x1, c.y1, int(c.real), int(c.merged)) for c in cands[:6]]
        controls[index] = control
    for control, hit in zip(controls, hits):
        x_ref = hit.anchor_x
        if hit.prefix_px > 0 and not control.found:
            x_ref = control.x1 + 1
        _measure(ink, control, max(x_ref, control.x1 + 1), hit.left_limit, th)
        if locate is not None and locate is not ink:
            # a faint pen stroke on faint print vanishes from the dark mask but not from the
            # light one; siblings are measured the same way so the relative rule still holds
            light = Control(control.code, control.x0, control.y0, control.x1, control.y1, control.found)
            _measure(locate, light, max(x_ref, control.x1 + 1), hit.left_limit, th)
            control.extra["interior2"] = light.interior
            control.extra["outside2"] = light.outside
        control.ring = _ring(ink, hit, th, words, control)
        control.extra["hit"] = hit
    if not used_cluster and family.id != "tiny":
        _align_columns(ink, controls, th)
    sides = sorted(max(c.x1 - c.x0, c.y1 - c.y0) for c in controls if c.found)
    typical = sides[len(sides) // 2] if sides else th
    for control in controls:
        if (control.y0 <= 1 or control.y1 >= height - 2) and (control.y1 - control.y0) < 0.8 * typical:
            control.extra["edge"] = True  # clipped by the crop: half a circle looks filled
    if found_n == 0 and len(controls) >= 4 and family.id in ("new_circle", "es_circle", "old_checkbox", "es_checkbox"):
        for control in controls:
            control.extra["no_controls"] = True  # a typed or text-only copy of the form: nothing to measure
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


def _overlap_growth(labels, slices, x0: int, y0: int, x1: int, y1: int, side: int, x_ref: int, th: int) -> float | None:
    """Area ratio of the one component that covers most of this window when it is much larger
    than a control and ends before the label: a check that ran out of the box and merged with it.
    Lines (binder lines, rules) and words glued to the box are excluded."""
    region = labels[y0:y1, x0:x1]
    if region.size == 0:
        return None
    ids, counts = np.unique(region, return_counts=True)
    best = None
    for index, count in zip(ids, counts):
        if index == 0:
            continue
        if best is None or count > best[1]:
            best = (index, count)
    if best is None or best[1] < 0.10 * region.size:
        return None
    sl = slices[best[0] - 1]
    if sl is None:
        return None
    cy0, cy1, cx0, cx1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
    w, h = cx1 - cx0, cy1 - cy0
    if w <= th * 0.3 or h <= th * 0.3 or h >= th * 6 or w >= th * 8:
        return None  # a line or a huge blob, not a check
    if cx1 > x_ref + th * 0.3:
        return None  # runs into the label: text glued to the box
    ratio = (w * h) / max(1.0, float((x1 - x0) * (y1 - y0)))
    return ratio if ratio >= 1.8 else None


def _snap(labels, slices, x0: int, y0: int, x1: int, y1: int, side: int) -> tuple[int, int, int, int] | None:
    """Move a window placed by column geometry onto the one control-sized component it overlaps,
    so a hollow box or bullet is measured on its own bounds and its rim stays out of the interior."""
    m = max(2, int(side * 0.45))
    ya, yb = max(0, y0 - m), min(labels.shape[0], y1 + m)
    xa, xb = max(0, x0 - m), min(labels.shape[1], x1 + m)
    if yb <= ya or xb <= xa:
        return None
    ids = np.unique(labels[ya:yb, xa:xb])
    best = None
    best_overlap = 0
    for index in ids:
        if index == 0:
            continue
        sl = slices[index - 1]
        if sl is None:
            continue
        cy0, cy1, cx0, cx1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        w, h = cx1 - cx0, cy1 - cy0
        if not (0.75 * side <= w <= 1.5 * side and 0.75 * side <= h <= 1.5 * side):
            continue
        ox = max(0, min(x1, cx1) - max(x0, cx0))
        oy = max(0, min(y1, cy1) - max(y0, cy0))
        overlap = ox * oy
        if overlap > best_overlap:
            best_overlap = overlap
            best = (cx0, cy0, cx1, cy1)
    if best is None or best_overlap < 0.3 * max(1, (x1 - x0) * (y1 - y0)):
        return None
    return best


def _straight_sides(mask: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> bool:
    """A printed square has straight left and right edges that run the whole height; "D", "O",
    "e" and the other round letters that are box-sized do not. Two outer columns are pooled so a
    slightly skewed scan still passes; rounded corners are tolerated by measuring the middle 70%."""
    w = x1 - x0
    h = y1 - y0
    if w < 6 or h < 6:
        return False
    band = max(2, w // 8)
    # the full height: a box edge runs top to bottom, the bowl of a "D" or "O" only the middle
    left = float(mask[y0:y1, x0 : x0 + band].any(axis=1).mean())
    right = float(mask[y0:y1, x1 - band : x1].any(axis=1).mean())
    mx0 = x0 + int(w * 0.15)
    mx1 = x1 - int(w * 0.15)
    vband = max(2, h // 8)
    top = float(mask[y0 : y0 + vband, mx0:mx1].any(axis=0).mean()) if mx1 > mx0 else 0.0
    bottom = float(mask[y1 - vband : y1, mx0:mx1].any(axis=0).mean()) if mx1 > mx0 else 0.0
    return left >= 0.85 and right >= 0.85 and top >= 0.6 and bottom >= 0.6


def _cornered(mask: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> bool:
    """A printed square has ink in at least three of its four corners; a circle or a round
    letter leaves the corners of its bounding box empty."""
    w = x1 - x0
    h = y1 - y0
    if w < 6 or h < 6:
        return False
    cell = max(3, min(w, h) // 5)
    corners = (
        mask[y0 : y0 + cell, x0 : x0 + cell].mean(),
        mask[y0 : y0 + cell, x1 - cell : x1].mean(),
        mask[y1 - cell : y1, x0 : x0 + cell].mean(),
        mask[y1 - cell : y1, x1 - cell : x1].mean(),
    )
    return sum(1 for v in corners if v >= 0.2) >= 3


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


def _ring(ink: np.ndarray, hit: LabelHit, th: int, words: list[dict] | None = None, control: Control | None = None) -> float:
    """Ink around the label words, excluding every printed word box in the region (circled text,
    R6b). A pen circle drawn around a label leaves ink on all four sides; an underline, a binder
    line or the neighbouring row touches one or two sides, so the score is the weakest side.
    """
    height, width = ink.shape
    dx = int(th * 0.9)
    dy = int(th * 0.7)
    x0 = _clip(hit.x0 - dx, 0, width)
    x1 = _clip(hit.x1 + dx, 0, width)
    y0 = _clip(hit.y0 - dy, 0, height)
    y1 = _clip(hit.y1 + dy, 0, height)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    region = ink[y0:y1, x0:x1].copy()
    if control is not None:
        cx0 = _clip(control.x0 - 2 - x0, 0, region.shape[1])
        cx1 = _clip(control.x1 + 2 - x0, 0, region.shape[1])
        cy0 = _clip(control.y0 - 2 - y0, 0, region.shape[0])
        cy1 = _clip(control.y1 + 2 - y0, 0, region.shape[0])
        region[cy0:cy1, cx0:cx1] = False  # the printed box or circle is not a pen ring
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
    # a pen ring crosses the whole width above and below the label and the whole height at its
    # sides: measure how much of each band's span carries ink, not how dark the band is, so the
    # value does not depend on the band size or the pen width
    bands = [
        float(top_band.any(axis=0).mean()) if top_band.size else 0.0,
        float(bottom_band.any(axis=0).mean()) if bottom_band.size else 0.0,
        float(left_band.any(axis=1).mean()) if left_band.size else 0.0,
        float(right_band.any(axis=1).mean()) if right_band.size else 0.0,
    ]
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
    found_px = [c.extra["pixels"] for c in controls if c.found and c.extra.get("pixels") is not None]
    med_px = median(found_px) if found_px else 0
    light = [c.extra["interior2"] for c in controls if c.extra.get("interior2") is not None]
    med_i2 = median(light) if len(light) == len(controls) and light else None
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
            base = max(med_i, 0.05)
            a = (c.interior - base - 0.15) / 0.20
            if c.interior < 2.0 * max(med_i, 0.05):
                a = min(a, -0.01)
            b = (c.outside - med_o - 0.08) / 0.10
            if c.outside < 2.0 * max(med_o, 0.03):
                b = min(b, -0.01)
            g = -9.0
            if c.found and not c.extra.get("merged") and c.side >= 1.25 * med_side and (c.interior >= med_i + 0.03 or c.outside >= med_o + 0.03):
                g = (c.side / med_side - 1.25) / 0.3  # a check drawn over a bullet grows the component
            px = c.extra.get("pixels")
            if px is not None and med_px > 0 and c.found:
                g = max(g, (px / med_px - 1.4) / 0.4)  # a bullet with a pen stroke carries more ink
        else:
            place = c.extra.get("place", "")
            tight = c.found or place in ("column", "offset")
            # a box measured on its own bounds is clean (99% of empty boxes stay under 0.02 above
            # the median); a window placed by geometry may catch a bit of rim; a loose window
            # catches rim and neighbour ink
            margin = 0.04 if c.found else (0.08 if tight else 0.14)
            a = (c.interior - med_i - margin) / 0.12
            if c.interior < 2.5 * max(med_i, 0.015):
                a = min(a, -0.01)
            b = (c.outside - med_o - 0.10) / 0.12
            if c.outside < 2.0 * max(med_o, 0.03):
                b = min(b, -0.01)
            if tight and c.extra.get("interior2") is not None and med_i2 is not None and c.interior >= med_i + 0.015:
                # a faint pen stroke leaves a trace on the dark mask and a clear mark on the light
                # one; paper shading leaves nothing on the dark mask
                a2 = (c.extra["interior2"] - med_i2 - margin - 0.02) / 0.12
                if c.extra["interior2"] < 2.5 * max(med_i2, 0.02):
                    a2 = min(a2, -0.01)
                a = max(a, a2)
            g = -9.0
            grown = c.extra.get("overlap")
            if grown is not None and (c.interior >= med_i + 0.02 or c.outside >= med_o + 0.02):
                g = (grown - 1.8) / 0.6  # the box merged with a check that ran out of it
            if c.found and not c.extra.get("merged") and c.side >= 1.3 * med_side and (c.interior >= med_i + 0.03 or c.outside >= med_o + 0.05):
                g = (c.side / med_side - 1.3) / 0.3
        r = (c.ring - max(med_r, 0.1) - 0.25) / 0.2
        if c.ring < 0.45 or c.ring < med_r + 0.25:
            r = min(r, -0.01)
        c.score = max(a, b, g, r)
        if c.extra.get("edge"):
            c.score = -9.0
            c.reason = "edge"
        if c.extra.get("no_controls"):
            c.extra["unverified"] = True  # measured on windows only: the reading goes to review
        c.marked = c.score > 0.02
        if c.marked:
            c.reason = {a: "fill", b: "spill", g: "swollen", r: "circled"}[max((a, b, g, r), key=lambda v: v)]
        c.extra.update({"a": round(a, 2), "b": round(b, 2), "g": round(g, 2), "r": round(r, 2)})
