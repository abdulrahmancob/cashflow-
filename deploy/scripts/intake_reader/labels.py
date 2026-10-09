"""OCR words → lines → option labels (R2, R3, R4).

Labels are matched against the family vocabulary with a spacing-insensitive edit distance, so
"WordofMouth_", "Cliniestaff", "@oogie" and "QZocdoc" still land on the right option.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .anchors import OPTION_ANCHOR_RE
from .anchors import (
    BOOKING_PHRASES,
    BOOKING_RE,
    Family,
    INSTRUCTION_RE,
    Option,
    QUESTION_PHRASES,
    QUESTION_RE,
    SECTION_END_RE,
)


@dataclass
class Line:
    words: list[dict]
    cy: float
    y0: int
    y1: int
    text: str
    segments: list[list[dict]] = field(default_factory=list)


@dataclass
class LabelHit:
    code: str
    option: Option
    words: list[dict]
    line_index: int
    group: str  # "hear" | "booking"
    score: float
    left_limit: int  # x where the previous word on the line ends (0 when none)
    right_limit: int  # x where the next word starts (segment end when none)
    glyph: dict | None = None  # a leading OCR glyph word ("O", "@", "(Y") that sits on the control
    prefix_px: int = 0  # width of junk glued to the first word ("Qcoctor" -> the Q)
    anchor: int | None = None  # explicit label start (inferred labels use the column position)
    inferred: bool = False
    line_cy: float = 0.0  # centre and typical height of the printed line the label sits on
    line_h: int = 0

    @property
    def x0(self) -> int:
        return min(w["x"] for w in self.words)

    @property
    def anchor_x(self) -> int:
        """Where the printed label really starts (after any glued glyph)."""
        if self.anchor is not None:
            return self.anchor
        return self.x0 + self.prefix_px

    @property
    def x1(self) -> int:
        return max(w["x"] + w["w"] for w in self.words)

    def _raw_box(self) -> tuple[int, int]:
        return min(w["y"] for w in self.words), max(w["y"] + w["h"] for w in self.words)

    @property
    def y0(self) -> int:
        raw0, raw1 = self._raw_box()
        if self.line_h and raw1 - raw0 > self.line_h * 1.6:
            return int(self.line_cy - self.line_h * 0.55)  # a word box stretched by scanner debris
        return raw0

    @property
    def y1(self) -> int:
        raw0, raw1 = self._raw_box()
        if self.line_h and raw1 - raw0 > self.line_h * 1.6:
            return int(self.line_cy + self.line_h * 0.55)
        return raw1

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def text_h(self) -> int:
        if self.line_h:
            return max(8, self.line_h)
        return max(8, int(sorted(w["h"] for w in self.words)[len(self.words) // 2]))


@dataclass
class Layout:
    lines: list[Line]
    text_h: int
    question_line: int | None
    booking_line: int | None
    end_line: int | None
    hear: list[LabelHit]
    booking: list[LabelHit]
    block_text: str


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]", "", text)


# Letter pairs tesseract swaps on scanned forms; substituting one for the other costs half.
_CONFUSABLE = {
    frozenset(p)
    for p in ("cd", "ce", "oe", "o0", "li", "l1", "i1", "s5", "bh", "nm", "uv", "gq", "ao", "tf", "rn", "cg", "ea", "ij", "yv", "dq", "pb", "hk", "ou", "zs", "x4", "ad")
}


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if frozenset((a, b)) in _CONFUSABLE:
        return 0.5
    return 1.0


def edit_distance(left: str, right: str, limit: int | None = None) -> float:
    if left == right:
        return 0
    if not left or not right:
        return len(left) + len(right)
    if limit is not None and abs(len(left) - len(right)) > limit:
        return limit + 1
    prev = list(range(len(right) + 1))
    for i, lch in enumerate(left, 1):
        cur = [float(i)]
        for j, rch in enumerate(right, 1):
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + _sub_cost(lch, rch)))
        prev = cur
    return prev[-1]


def similarity(window: str, key: str) -> float:
    if not window or not key:
        return 0.0
    best = 1 - edit_distance(window, key) / max(len(window), len(key))
    # OCR often glues a mark glyph to the first letter or drops it: "@oogie", "oogle", "qzocdoc".
    stripped = re.sub(r"^[^a-z]+", "", window)
    if stripped != window and stripped:
        best = max(best, 1 - edit_distance(stripped, key) / max(len(stripped), len(key)))
    if len(window) >= 4 and len(window) >= 0.6 * len(key):
        best = max(best, 1 - edit_distance(key[0] + window[1:], key) / max(len(window), len(key)) - 0.10)
        best = max(best, 1 - edit_distance(key[0] + window, key) / max(len(window) + 1, len(key)) - 0.10)
    # A label cut off by a binder line or the page edge ("Goog", "Zocd"): a clean prefix of the key.
    if len(window) >= 4 and len(window) >= 0.6 * len(key) and key.startswith(window):
        best = max(best, 0.76)
    # ... or its first letters ("ne / Text" for "Phone / Text"); short keys only, long ones share tails.
    if len(key) <= 12 and len(window) >= 4 and len(window) >= 0.6 * len(key) and key.endswith(window):
        best = max(best, 0.76)
    return best


def _threshold(key: str) -> float:
    if len(key) >= 12:
        return 0.70
    return 0.75


def group_lines(words: list[dict], text_h: int) -> list[Line]:
    """Cluster words into visual lines by vertical centre, then split each line into segments
    separated by a horizontal gap of 3 text heights (two-column layouts)."""
    ordered = sorted(words, key=lambda w: (w["y"] + w["h"] / 2, w["x"]))
    lines: list[Line] = []
    tol = max(5, int(text_h * 0.55))
    for word in ordered:
        center = word["y"] + word["h"] / 2
        if lines and abs(center - lines[-1].cy) <= tol:
            line = lines[-1]
            line.words.append(word)
            line.cy = sum(w["y"] + w["h"] / 2 for w in line.words) / len(line.words)
        else:
            lines.append(Line([word], center, word["y"], word["y"] + word["h"], ""))
    for line in lines:
        line.words.sort(key=lambda w: w["x"])
        line.y0 = min(w["y"] for w in line.words)
        line.y1 = max(w["y"] + w["h"] for w in line.words)
        line.text = " ".join(w["text"] for w in line.words)
        segments: list[list[dict]] = []
        gap = max(12, int(text_h * 3))
        for word in line.words:
            if segments and word["x"] - (segments[-1][-1]["x"] + segments[-1][-1]["w"]) <= gap:
                segments[-1].append(word)
            else:
                segments.append([word])
        line.segments = segments
    return lines


def phrase_in_line(line_text: str, phrases: tuple[str, ...], regex: re.Pattern | None) -> bool:
    if regex is not None and regex.search(line_text):
        return True
    key_line = normalize(line_text)
    for phrase in phrases:
        key = normalize(phrase)
        if not key or len(key_line) < len(key) * 0.6:
            continue
        best = 0.0
        step = max(1, len(key) // 6)
        for start in range(0, max(1, len(key_line) - len(key) + 4), step):
            window = key_line[start : start + len(key) + 2]
            best = max(best, similarity(window, key))
            if best >= 0.78:
                return True
    return False


def match_options(segment: list[dict], options: tuple[Option, ...], used: set[int]) -> list[tuple[float, Option, list[dict]]]:
    """Best non-overlapping option matches inside one segment."""
    candidates: list[tuple[float, int, int, Option]] = []
    for option in options:
        for phrase in option.phrases:
            key = normalize(phrase)
            if not key:
                continue
            tokens = max(1, len(phrase.split()))
            for start in range(len(segment)):
                if id(segment[start]) in used:
                    continue
                acc = ""
                for span in range(1, min(len(segment) - start, tokens + 2) + 1):
                    word = segment[start + span - 1]
                    if id(word) in used:
                        break
                    acc += normalize(word["text"])
                    if len(acc) < 3 or len(acc) > len(key) + 6:
                        continue
                    score = similarity(acc, key)
                    if score >= _threshold(key):
                        candidates.append((score, start, span, option, len(acc)))
    # Longest coverage of the key first, then score: "Doctor referral" beats the alias "referral".
    candidates.sort(key=lambda c: (-min(c[4], len(normalize(c[3].phrases[0]))), -c[0], c[1]))
    taken: set[int] = set()
    hits: list[tuple[float, Option, list[dict]]] = []
    for score, start, span, option, _chars in candidates:
        indexes = set(range(start, start + span))
        if indexes & taken:
            continue
        if any(hit[1].code == option.code for hit in hits):
            continue
        taken |= indexes
        hits.append((score, option, segment[start : start + span]))
    return hits


def _question_match_words(line: Line) -> set[int]:
    """Words that belong to the question phrase itself (so the tiny form's inline options stay)."""
    used: set[int] = set()
    match = QUESTION_RE.search(line.text)
    if not match:
        return used
    cursor = 0
    for word in line.words:
        end = cursor + len(word["text"])
        if cursor < match.end() and end > match.start():
            used.add(id(word))
        cursor = end + 1
    return used


def _line_stats(line: Line, th: int) -> tuple[float, int]:
    """Centre and typical height of the real words on a line.

    Word boxes stretched by scanner debris (a binder-line fragment glued to "Zocd") are ignored;
    when every word is stretched, the text is assumed to sit at the top of the box, where
    tesseract puts the letters and hangs the debris below.
    """
    texty = [w for w in line.words if len(re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", w["text"])) >= 2 and w["h"] <= th * 1.5]
    if texty:
        heights = sorted(w["h"] for w in texty)
        h = heights[len(heights) // 2]
        cy = sum(w["y"] + w["h"] / 2 for w in texty) / len(texty)
        return cy, max(8, h)
    top = min(w["y"] for w in line.words)
    return top + th * 0.55, th


def _nearest_line(lines: list[Line], y: float | None, th: int, prefer_above: bool = True) -> int | None:
    """Line closest to a known position; the question sits above its options, so a line at or
    above the hint wins a tie against one below."""
    if y is None or not lines:
        return None

    def letters(i: int) -> int:
        return sum(len(re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", w["text"])) for w in lines[i].words)

    def cost(i: int) -> float:
        delta = lines[i].y0 - y
        junk = th * 1.0 if letters(i) < 4 else 0  # "|" or a stray glyph is not the question line
        return abs(delta) + (th * 0.8 if (prefer_above and delta > th * 0.5) else 0) + junk

    index = min(range(len(lines)), key=cost)
    return index if abs(lines[index].y0 - y) <= th * 2.5 else None


def _glyph_prefix(first: dict, key: str) -> int:
    """Pixels of junk glued before the label's first letter ("Qcoctor" -> width of "Q").

    Leading non-letters always count. Leading letters count when dropping them makes the word
    look clearly more like the start of the label (a check through the circle often turns the
    circle plus the first letter into one or two stray letters).
    """
    raw = first["text"]
    norm = normalize(raw)
    if not norm or not key:
        return 0
    lead = len(raw) - len(raw.lstrip("".join(ch for ch in raw if not normalize(ch))))
    token = key[: max(3, min(len(key), len(norm)))]
    base = similarity(norm, token)
    best_k = 0
    best = base
    for k in (1, 2):
        if len(norm) - k < 3:
            break
        trimmed = norm[k:]
        score = 1 - edit_distance(trimmed, key[: len(trimmed)]) / max(len(trimmed), 1)
        if score >= best + 0.10:
            best, best_k = score, k
    chars = lead + best_k
    if chars <= 0:
        return 0
    if lead >= 4:
        return 0  # a run of letters that long is another word joined to the label, not a glyph
    return int(round(first["w"] * chars / max(1, len(raw))))


def analyse(
    words: list[dict],
    family: Family,
    text_h: int | None = None,
    max_lines: int = 18,
    question_y: float | None = None,
    booking_y: float | None = None,
) -> Layout:
    """Find the question, the booking question, the block end and every option label.

    `question_y`/`booking_y` are positions known from an earlier scan; they rescue a block whose
    question line OCR'd into garbage (R2).
    """
    from .page import median_text_height

    th = text_h or median_text_height(words)
    lines = group_lines(words, th)
    q_index = next((i for i, line in enumerate(lines) if phrase_in_line(line.text, QUESTION_PHRASES, QUESTION_RE)), None)
    b_index = next((i for i, line in enumerate(lines) if phrase_in_line(line.text, BOOKING_PHRASES, BOOKING_RE)), None)
    if q_index is None:
        q_index = _nearest_line(lines, question_y, th)
    if q_index is None:
        # the question text did not OCR but the options did: a virtual line just above the first
        # option row takes its place so the block still starts in the right place
        anchors_at = [i for i, line in enumerate(lines) if OPTION_ANCHOR_RE.search(line.text)]
        if len(anchors_at) >= 2 and (b_index is None or anchors_at[0] > b_index):
            first = lines[anchors_at[0]]
            virtual = Line([], first.y0 - th * 0.6, int(first.y0 - th * 1.1), int(first.y0 - th * 0.1), "")
            lines.insert(anchors_at[0], virtual)
            q_index = anchors_at[0]
            if b_index is not None and b_index >= q_index:
                b_index += 1
    if b_index is None and booking_y is not None:
        b_index = _nearest_line(lines, booking_y, th)
        if b_index is not None and b_index == q_index:
            b_index = None
    end_index: int | None = None
    if q_index is not None:
        # the option list never runs deeper than this below the question; a section header the
        # OCR garbled ("INSURANCE INFORMATION") must not feed labels
        reach = th * (13 if family.id in ("new_circle", "es_circle", "tiny", "generic") else 26)
        for i in range(q_index + 1, len(lines)):
            if SECTION_END_RE.search(lines[i].text) and not INSTRUCTION_RE.search(lines[i].text):
                end_index = i
                break
            if i - q_index > max_lines or lines[i].y0 - lines[q_index].y0 > reach:
                end_index = i
                break
    hear: list[LabelHit] = []
    booking: list[LabelHit] = []
    if q_index is None:
        return Layout(lines, th, None, b_index, end_index, hear, booking, "")

    def group_of(index: int) -> str | None:
        if index == q_index:
            return "hear"
        if end_index is not None and index >= end_index:
            return None
        if b_index is None:
            return "hear" if index > q_index else None
        if b_index < q_index:
            if index > q_index:
                return "hear"
            if index > b_index:
                return "booking"
            return None
        if index > b_index:
            return "booking"
        if index > q_index:
            return "hear"
        return None

    block_lines: list[str] = []
    for index, line in enumerate(lines):
        group = group_of(index)
        if group is None:
            continue
        if group == "hear":
            block_lines.append(line.text)
        used = _question_match_words(line) if index == q_index else set()
        if index == b_index:
            used |= set(id(w) for w in line.words)
        options = family.options if group == "hear" else family.booking
        if not options:
            continue
        for segment in line.segments:
            for score, option, matched in match_options(segment, options, used):
                glyph = None
                key0 = normalize(option.phrases[0])
                while len(matched) > 1:
                    lead = normalize(matched[0]["text"])
                    # "", "O", "e", "0", "fe)", "rot" in front of a label are the control or a mark, not text
                    if len(lead) <= 1 or (len(lead) <= 3 and not key0.startswith(lead[:2])):
                        glyph = matched[0]
                        matched = matched[1:]
                    else:
                        break
                # OCR sometimes joins "(Type doctor's name/office)" to the label row; the helper
                # words are not the label and sit on the row below
                while len(matched) > 1 and _HELPER_LINE_RE.search(re.sub(r"^[^A-Za-z]+", "", matched[0]["text"])):
                    matched = matched[1:]
                # a trailing scrap ("�", "|") stretches the label box over the next row; drop it
                while len(matched) > 1 and len(normalize(matched[-1]["text"])) <= 1:
                    matched = matched[:-1]
                first = matched[0]
                last = matched[-1]
                # Glyph-like words ("O", "@", "(Y") are controls or marks, not text that bounds the label.
                texty = [w for w in line.words if len(re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", w["text"])) >= 2]
                prev = [w for w in texty if w["x"] + w["w"] <= first["x"] and id(w) not in {id(m) for m in matched}]
                nxt = [w for w in texty if w["x"] >= last["x"] + last["w"] and id(w) not in {id(m) for m in matched}]
                left_limit = max((w["x"] + w["w"] for w in prev), default=0)
                if first["x"] - left_limit < th * 3:
                    left_limit = 0  # a scrap of "text" that close is the control or scanner noise
                right_limit = min((w["x"] for w in nxt), default=segment[-1]["x"] + segment[-1]["w"])
                prefix = _glyph_prefix(first, normalize(option.phrases[0]))
                hit = LabelHit(option.code, option, matched, index, group, score, left_limit, right_limit, glyph, prefix)
                hit.line_cy, hit.line_h = _line_stats(line, th)
                (hear if group == "hear" else booking).append(hit)
    hear = _dedupe(hear)
    booking = _dedupe(booking)
    hear = _infer_missing(lines, hear, family, th, group_of, q_index)
    hear = _infer_circle_rows(lines, hear, family, th, group_of)
    hear = _infer_tiny(lines, hear, family, th, group_of)
    hear = _infer_other(lines, hear, family, th, group_of)
    return Layout(lines, th, q_index, b_index, end_index, hear, booking, "\n".join(block_lines))


_HELPER_LINE_RE = re.compile(r"type\s*doctor|doctor.s\s*name|name\s*/?\s*office|escriba|nombre\s*del|oficina", re.IGNORECASE)


def _infer_missing(lines: list[Line], hits: list[LabelHit], family: Family, th: int, group_of, q_index: int | None = None) -> list[LabelHit]:
    """Fill gaps by row order (R4): when the unlabelled text segments inside a gap between two
    matched labels of a column are exactly as many as the options missing from that gap, take
    them in order. The control of an inferred label is looked up at the column position."""
    if family.id in ("tiny", "generic", "new_circle", "es_circle") or len(family.options) < 5 or not hits:
        return hits
    found = {h.code: h for h in hits}
    q_cy = lines[q_index].cy if q_index is not None and 0 <= q_index < len(lines) else None
    taken = {id(w) for h in hits for w in h.words}
    for column in sorted({o.column for o in family.options}):
        order = [o for o in family.options if o.column == column]
        col_hits = [found[o.code] for o in order if o.code in found]
        if not col_hits or all(o.code in found for o in order):
            continue
        col_x = sorted(h.x0 for h in col_hits)[len(col_hits) // 2]
        col_anchor = sorted(h.anchor_x for h in col_hits)[len(col_hits) // 2]
        other_cols = [h.x0 for h in hits if h.option.column != column]
        # gaps: (y_lo, y_hi, missing options in order)
        gaps: list[tuple[float, float, list[Option], bool]] = []
        pending: list[Option] = []
        prev_hit: LabelHit | None = None
        for option in order:
            if option.code in found:
                if pending:
                    leading = prev_hit is None
                    lo = prev_hit.y1 if prev_hit is not None else found[option.code].y0 - th * (2.6 * len(pending) + 2.2)
                    gaps.append((lo, found[option.code].y0, pending, leading))
                    pending = []
                prev_hit = found[option.code]
            else:
                pending.append(option)
        if pending and prev_hit is not None:
            gaps.append((prev_hit.y1, prev_hit.y1 + th * 2.6 * len(pending) + th, pending, False))
        for lo_y, hi_y, missing, leading in gaps:
            candidates = []
            for index, line in enumerate(lines):
                if group_of(index) != "hear" or index == q_index:
                    continue
                if q_cy is not None and line.cy <= q_cy + th * 0.8:
                    continue  # on the question row
                if line.y1 <= lo_y - th * 0.3 or line.y0 >= hi_y + th * 0.3:
                    continue
                if leading and _HELPER_LINE_RE.search(line.text):
                    continue  # "(Type doctor's name/office)" under the first option is not a label
                for segment in line.segments:
                    texty = [w for w in segment if len(re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", w["text"])) >= 2 and id(w) not in taken]
                    if not texty:
                        continue
                    letters = sum(len(re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", w["text"])) for w in texty)
                    span = texty[-1]["x"] + texty[-1]["w"] - texty[0]["x"]
                    if letters < 3 or span < th * 1.2:
                        continue  # a box glyph or a scrap, not a label
                    x0 = texty[0]["x"]
                    if abs(x0 - col_x) > th * 4:
                        continue
                    if other_cols and min(abs(x0 - x) for x in other_cols) < abs(x0 - col_x):
                        continue
                    candidates.append((line.cy, index, texty))
            candidates.sort()
            if leading and len(candidates) > len(missing):
                # the first option sits right under the question; a check glued to its box
                # garbles the whole line (and OCR may split the row into a glyph line and a text
                # line), so the first row's longest text is taken, then the next rows in order
                chosen: list = []
                rest = list(candidates)
                while rest and len(chosen) < len(missing):
                    top = rest[0][0]
                    row = [c for c in rest if abs(c[0] - top) <= th * 0.7]
                    best = max(row, key=lambda c: sum(len(re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", w["text"])) for w in c[2]))
                    chosen.append(best)
                    rest = [c for c in rest if c not in row]
                candidates = chosen
            if leading and not candidates and len(missing) == 1 and q_cy is not None:
                # the first option's line turned to junk ("pas al"): the first row of ink below the
                # question that is not the helper line is that option; its control sits at the column
                rows = [
                    (line.cy, index)
                    for index, line in enumerate(lines)
                    if group_of(index) == "hear" and index != q_index and line.words and line.cy > q_cy + th * 0.8
                    and line.cy < hi_y - th * 0.3 and not _HELPER_LINE_RE.search(line.text)
                ]
                if rows:
                    cy, index = min(rows)
                    pseudo = {"text": missing[0].phrases[0], "x": col_anchor, "y": int(cy - th / 2), "w": int(th * 0.55 * len(missing[0].phrases[0])), "h": th, "conf": 0.0, "synthetic": True}
                    hit = LabelHit(missing[0].code, missing[0], [pseudo], index, "hear", 0.45, 0, col_anchor + pseudo["w"], None, 0, col_anchor, True)
                    hit.line_cy, hit.line_h = _line_stats(lines[index], th)
                    hits.append(hit)
                    found[missing[0].code] = hit
                continue
            if len(candidates) != len(missing):
                continue
            for option, (_cy, index, words) in zip(missing, candidates):
                hit = LabelHit(option.code, option, words, index, "hear", 0.5, 0, words[-1]["x"] + words[-1]["w"], None, 0, col_anchor, True)
                hit.line_cy, hit.line_h = _line_stats(lines[index], th)
                hits.append(hit)
                found[option.code] = hit
                taken |= {id(w) for w in words}
    return sorted(hits, key=lambda h: (h.line_index, h.x0))


def _infer_circle_rows(lines: list[Line], hits: list[LabelHit], family: Family, th: int, group_of) -> list[LabelHit]:
    """The circle forms print their options on a fixed grid, so an option OCR dropped (a check
    through the circle often takes the whole line with it) is placed from its column neighbours."""
    if family.id not in ("new_circle", "es_circle") or len(hits) < 2:
        return hits
    found = {h.code: h for h in hits}
    added: list[LabelHit] = []
    for column in sorted({o.column for o in family.options}):
        order = [o for o in family.options if o.column == column and o.code != "other"]
        known = [(i, found[o.code]) for i, o in enumerate(order) if o.code in found]
        if len(known) < 1 or len(known) == len(order):
            continue
        if len(known) == 1 and not any(group_of(i) == "hear" for i in range(len(lines))):
            continue
        cys = [(i, h.line_cy or h.cy) for i, h in known]
        gaps = [(cy2 - cy1) / (i2 - i1) for (i1, cy1), (i2, cy2) in zip(cys, cys[1:]) if i2 > i1 and cy2 > cy1]
        other_cols = [h for h in hits if h.option.column != column]
        if gaps:
            spacing = sorted(gaps)[len(gaps) // 2]
        else:
            ocys = sorted(h.line_cy or h.cy for h in other_cols)
            ogaps = [b - a for a, b in zip(ocys, ocys[1:]) if b - a > th * 0.8]
            spacing = sorted(ogaps)[len(ogaps) // 2] if ogaps else th * 2.3
        if not th * 1.2 <= spacing <= th * 4.0:
            continue
        col_anchor = sorted(h.anchor_x for _i, h in known)[len(known) // 2]
        line_h = sorted(h.line_h or th for _i, h in known)[len(known) // 2]
        for i, option in enumerate(order):
            if option.code in found:
                continue
            ref_i, ref = min(known, key=lambda k: abs(k[0] - i))
            if abs(ref_i - i) > 2:
                continue
            exp_y = (ref.line_cy or ref.cy) + (i - ref_i) * spacing
            best = None
            taken = {id(w) for h in hits + added for w in h.words}
            for index, line in enumerate(lines):
                if group_of(index) != "hear":
                    continue
                free = [w for w in line.words if id(w) not in taken and len(re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", w["text"])) >= 2]
                if not free:
                    continue  # that row's text belongs to another label
                if abs(line.cy - exp_y) <= spacing * 0.4 and (best is None or abs(line.cy - exp_y) < abs(lines[best].cy - exp_y)):
                    best = index
            row_confirmed = any(abs((h.line_cy or h.cy) - exp_y) <= spacing * 0.3 for h in other_cols + added)
            between = known[0][0] < i < known[-1][0]
            if best is None and (len(known) < 2 or abs(ref_i - i) > 1 or not (between or row_confirmed or len(known) >= 3)):
                continue  # no text at that row and too few siblings to trust the grid
            line_index = best if best is not None else ref.line_index
            y_c = exp_y
            lh = line_h
            if best is not None:
                y_c, lh2 = _line_stats(lines[best], th)
                lh = lh2 or lh
            width_px = int(th * 0.55 * len(option.phrases[0]))
            pseudo = {"text": option.phrases[0], "x": col_anchor, "y": int(y_c - lh / 2), "w": width_px, "h": int(lh), "conf": 0.0, "synthetic": True}
            hit = LabelHit(option.code, option, [pseudo], line_index, "hear", 0.45, 0, col_anchor + width_px, None, 0, col_anchor, True)
            hit.line_cy, hit.line_h = float(y_c), int(lh)
            added.append(hit)
            found[option.code] = hit
    if not added:
        return hits
    return sorted(hits + added, key=lambda h: (h.line_index, h.x0))


_TINY_NEIGHBOURS = (
    # (missing, found neighbour, side of the neighbour the missing label sits on)
    ("doctor", "google", "left"),
    ("google", "doctor", "right"),
    ("google", "social_media", "left"),
    ("social_media", "google", "right"),
    ("zocdoc", "social_media", "right"),
    ("walk_in", "event", "left"),
    ("event", "walk_in", "right"),
    ("event", "friend_family", "left"),
    ("friend_family", "event", "right"),
)


def _infer_tiny(lines: list[Line], hits: list[LabelHit], family: Family, th: int, group_of) -> list[LabelHit]:
    """On the one-row form a check glued to a box turns "Doctor" into "(Dtos": the label is the
    text just beside a found neighbour, box glyph and all (R11)."""
    if family.id != "tiny" or not hits:
        return hits
    found = {h.code: h for h in hits}
    options = {o.code: o for o in family.options}
    taken = {id(w) for h in hits for w in h.words}
    for missing, neighbour, side in _TINY_NEIGHBOURS:
        if missing in found or neighbour not in found:
            continue
        ref = found[neighbour]
        line = lines[ref.line_index]
        # OCR may split one printed row into two lines; look at every line on that row
        row_lines = [(i, ln) for i, ln in enumerate(lines) if abs(ln.cy - (ref.line_cy or ref.cy)) <= th * 1.0 and group_of(i) == "hear"]
        words = [w for _i, ln in row_lines for w in ln.words if id(w) not in taken and len(re.sub(r"[^A-Za-z]", "", w["text"])) >= 2]
        if side == "left":
            cands = [w for w in words if w["x"] + w["w"] <= ref.x0 - th * 0.8 and w["x"] >= ref.x0 - th * 9]
            pick = max(cands, key=lambda w: w["x"]) if cands else None
        else:
            cands = [w for w in words if w["x"] >= ref.x1 + th * 0.8 and w["x"] <= ref.x1 + th * 9]
            pick = min(cands, key=lambda w: w["x"]) if cands else None
        if pick is None:
            continue
        # the printed box or its mark is often glued to the word ("(Dtos", "LAGcoote")
        key = normalize(options[missing].phrases[0])
        prefix = _glyph_prefix(pick, key)
        line_index = next((i for i, ln in row_lines if pick in ln.words), ref.line_index)
        hit = LabelHit(missing, options[missing], [pick], line_index, "hear", 0.5, 0, pick["x"] + pick["w"], None, prefix, None, True)
        hit.line_cy, hit.line_h = _line_stats(lines[line_index], th)
        hits.append(hit)
        found[missing] = hit
        taken.add(id(pick))
    # the two-row variant always prints Zocdoc after Social Media; when the word was swallowed by
    # a check the label is placed by geometry
    if "zocdoc" not in found and "social_media" in found and any(c in found for c in ("walk_in", "event", "friend_family")):
        ref = found["social_media"]
        line = lines[ref.line_index]
        beyond = [w for w in line.words if w["x"] >= ref.x1 + th * 0.8]
        if not beyond:
            x = int(ref.x1 + th * 2.9)
            pseudo = {"text": "zocdoc", "x": x, "y": int(ref.cy - (ref.line_h or th) / 2), "w": int(th * 3.3), "h": int(ref.line_h or th), "conf": 0.0, "synthetic": True}
            hit = LabelHit("zocdoc", options["zocdoc"], [pseudo], ref.line_index, "hear", 0.45, 0, x + pseudo["w"], None, 0, x, True)
            hit.line_cy, hit.line_h = ref.line_cy, ref.line_h
            hits.append(hit)
            found["zocdoc"] = hit
    return sorted(hits, key=lambda h: (h.line_index, h.x0))


def _infer_other(lines: list[Line], hits: list[LabelHit], family: Family, th: int, group_of) -> list[LabelHit]:
    """Place the "Other:" line by geometry when OCR lost it or read it badly (R4).

    On the circle forms it sits one row under the previous option of the first column; its dotted
    write-in line turns the OCR into junk ("Otherness", "AG") or swallows the handwriting, which
    then hides from the write-in detector. On the tiny form it is the word after the last option
    or the first word of the next row.
    """
    if family.id not in ("new_circle", "es_circle", "tiny") or not hits:
        return hits
    last = family.options[-1]
    if last.code != "other":
        return hits
    cur = next((h for h in hits if h.code == "other"), None)
    others = [h for h in hits if h.code != "other"]
    if not others:
        return hits
    if family.id == "tiny":
        last_hit = max(others, key=lambda h: (h.line_index, h.x1))
        fine = cur is not None and (
            cur.score >= 0.9 or cur.line_index > last_hit.line_index or (cur.line_index == last_hit.line_index and cur.x0 > last_hit.x1)
        )
        if cur is not None and not fine:
            hits = [h for h in hits if h is not cur]
            cur = None
        if cur is not None:
            if cur.x1 - cur.x0 > th * 3.5 and len(cur.words) == 1:
                # "Other:__hugi": the word swallowed the handwriting; keep only the printed part
                word = cur.words[0]
                short = dict(word)
                short["w"] = int(th * 3.0)
                short["text"] = word["text"][:6]
                hits = [h for h in hits if h is not cur]
                hit = LabelHit("other", last, [short], cur.line_index, "hear", cur.score, 0, short["x"] + short["w"] + th, None, cur.prefix_px, None, True)
                hit.line_cy, hit.line_h = cur.line_cy, cur.line_h
                hits.append(hit)
                return sorted(hits, key=lambda h: (h.line_index, h.x0))
            return hits
        row_x0 = min(h.x0 for h in others)
        for index in range(last_hit.line_index, min(len(lines), last_hit.line_index + 3)):
            if group_of(index) != "hear":
                continue
            line = lines[index]
            if index != last_hit.line_index and line.cy < last_hit.cy + th * 0.8:
                continue  # OCR split the option row; the Other line sits below it
            for word in line.words:
                if index == last_hit.line_index and word["x"] <= last_hit.x1:
                    continue
                if len(re.sub(r"[^A-Za-z]", "", word["text"])) < 2:
                    continue
                starts_row = index > last_hit.line_index and word is line.words[0] and word["x"] < row_x0 + th * 4
                if similarity(normalize(word["text"]), "other") >= 0.5 or starts_row:
                    hit = LabelHit("other", last, [word], index, "hear", 0.5, 0, word["x"] + word["w"] + th, None, 0, None, True)
                    hit.line_cy, hit.line_h = _line_stats(line, th)
                    hits.append(hit)
                    return sorted(hits, key=lambda h: (h.line_index, h.x0))
                break
        return hits
    column = sorted((h for h in others if h.option.column == last.column), key=lambda h: h.line_cy or h.cy)
    if not column:
        return hits
    cys = [h.line_cy or h.cy for h in column]
    gaps = [b - a for a, b in zip(cys, cys[1:]) if b - a > th * 0.8]
    spacing = sorted(gaps)[len(gaps) // 2] if gaps else th * 2.3
    exp_y = cys[-1] + spacing
    col_x = sorted(h.x0 for h in column)[len(column) // 2]
    col_anchor = sorted(h.anchor_x for h in column)[len(column) // 2]
    if cur is not None:
        if cur.score >= 0.9 and cur.x1 - cur.x0 <= th * 4.5:
            return hits  # a clean "Other:" label
        placed = abs(cur.x0 - col_x) <= th * 2.5 and abs((cur.line_cy or cur.cy) - exp_y) <= spacing * 0.6
        if cur.score >= 0.9 and not placed:
            return hits  # confident but somewhere else: leave it alone
        hits = [h for h in hits if h is not cur]
    prev = column[-1]
    line_index = prev.line_index
    y_c = exp_y
    line_h = prev.line_h or th
    best = None
    for index, line in enumerate(lines):
        if group_of(index) != "hear":
            continue
        if abs(line.cy - exp_y) <= spacing * 0.45 and (best is None or abs(line.cy - exp_y) < abs(lines[best].cy - exp_y)):
            best = index
    if best is not None:
        line_index = best
        y_c, lh = _line_stats(lines[best], th)
        line_h = lh or line_h
    width_px = int(th * (2.6 if family.id == "es_circle" else 3.0))
    if best is not None:
        # an OCR word that starts on the label ("Otherness", "Other:----") tells where the print ends
        for word in lines[best].words:
            if word["x"] <= col_anchor + th * 0.6 and word["x"] + word["w"] > col_anchor + width_px:
                width_px = int(min(word["x"] + word["w"] - col_anchor, th * 3.6))
    pseudo = {"text": last.phrases[0], "x": col_anchor, "y": int(y_c - line_h / 2), "w": width_px, "h": int(line_h), "conf": 0.0, "synthetic": True}
    hit = LabelHit("other", last, [pseudo], line_index, "hear", 0.45, 0, col_anchor + width_px, None, 0, col_anchor, True)
    hit.line_cy, hit.line_h = float(y_c), int(line_h)
    hits.append(hit)
    return sorted(hits, key=lambda h: (h.line_index, h.x0))


def _dedupe(hits: list[LabelHit]) -> list[LabelHit]:
    """One hit per code: keep the best score; a second copy of the same code (duplicate scan
    region) is dropped so sibling statistics are not skewed."""
    best: dict[str, LabelHit] = {}
    for hit in hits:
        cur = best.get(hit.code)
        if cur is None or hit.score > cur.score or (hit.score == cur.score and hit.line_index < cur.line_index):
            best[hit.code] = hit
    return sorted(best.values(), key=lambda h: (h.line_index, h.x0))
