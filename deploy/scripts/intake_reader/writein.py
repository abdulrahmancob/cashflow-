"""Handwritten answers next to "Other", the doctor-name line and other write-in lines (R7)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import numpy as np

from .labels import LabelHit, normalize, similarity
from .page import components

# Printed helper text under/after a label; never counted as handwriting.
_PRINTED_HINTS = (
    "type doctor's name/office",
    "typedoctor's name/office",
    "type doctor's name",
    "escriba el nombre del médico/oficina",
    "escriba el nombre del medico oficina",
    "please specify",
    "especifique",
)

_KEYWORDS: tuple[tuple[str, re.Pattern], ...] = (
    ("zocdoc", re.compile(r"zoc\s*doc|zocdoc", re.I)),
    ("social_media", re.compile(r"facebook|instagram|tik\s*tok|youtube|snapchat|twitter|\bsocial\b|\bredes\b", re.I)),
    (
        "insurance",
        re.compile(
            r"aetna|fidelis|health\s*first|healthfirst|medicare|medicaid|anthem|blue\s*cross|bcbs|united|"
            r"emblem|metro\s*plus|wellcare|humana|oscar|cigna|molina|1199|empire|oxford|insurance|seguro|"
            r"provider\s*list|approved\s*providers|list\s*of\s*providers|\bplan\b",
            re.I,
        ),
    ),
    (
        "friend_family",
        re.compile(
            r"sister|brother|mother|father|mom\b|dad\b|husband|wife|spouse|son\b|daughter|friend|family|"
            r"neighbou?rs?\b|co-?worker|colleague|cousin|aunt|uncle|grand|hijo|hija|esposo|esposa|amig|"
            r"familia|herman|madre|padre|vecin|conocid|word\s*of\s*mouth|referred\s*by\s*(?:my|a)\s",
            re.I,
        ),
    ),
    (
        "walk_in",
        re.compile(
            r"walk|pass(?:ed|ing)?\s*by|next\s*door|\blive|\blives|\barea\b|neighborhood|neighbourhood|"
            r"location|nearby|\bblock\b|saw\s*(?:the|your|a)\s*(?:sign|office|clinic)|drove|across|"
            r"camin|pas[oé]\s*por|cerca|vecindario|barrio",
            re.I,
        ),
    ),
    ("event", re.compile(r"flyer|flier|folleto|event|evento|fair|outreach|table|church|school|senior\s*center", re.I)),
    (
        "doctor",
        re.compile(
            r"\bdr\b|\bdr\.|doctor|\bmd\b|m\.d\.|\bdo\b|\bpt\b|physical\s*therap|clinic|hospital|referr|"
            r"ortho|\bent\b|surgeon|surgery|podiatr|neuro|chiro|urgent\s*care|primary\s*care|pcp|"
            r"physician|nurse|\bnp\b|\bpa\b|medical|center|m[eé]dico|referencia|hospital|cl[ií]nica",
            re.I,
        ),
    ),
    ("website", re.compile(r"web\s*site|website|your\s*site|our\s*site|sitio\s*web|p[aá]gina\s*web", re.I)),
    ("google", re.compile(r"google|search|internet|online|\bweb|site|yelp|\bai\b|chat\s*gpt|maps", re.I)),
    ("phone", re.compile(r"phone|call|tel[eé]fono|llam", re.I)),
)


@dataclass
class WriteIn:
    code: str  # label the text belongs to
    kind: str  # inline | below
    text: str
    conf: float
    mapped: str | None
    ink: int
    area: tuple[int, int, int, int]


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def map_text(text: str) -> str | None:
    """Keyword map of a handwritten answer; None when nothing matches."""
    folded = _fold(text)
    if len(re.sub(r"[^a-z]", "", folded)) < 2:
        return None
    for code, pattern in _KEYWORDS:
        if pattern.search(folded):
            return code
    return _fuzzy_map(folded)


# Stems (5+ letters) matched within one OCR error; "Eciend" is "friend", "hussband" is "husband".
_STEMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("zocdoc", ("zocdoc",)),
    ("social_media", ("facebook", "instagram", "tiktok", "youtube", "social")),
    ("insurance", ("insurance", "aetna", "fidelis", "healthfirst", "medicare", "medicaid", "cigna", "united", "emblem", "metroplus", "oxford", "seguro", "providers")),
    (
        "friend_family",
        ("friend", "friends", "husband", "wife", "sister", "brother", "mother", "father", "daughter", "family", "spouse", "cousin", "neighbor", "neighbour", "coworker", "esposo", "esposa", "amigo", "amiga", "familia", "hermano", "hermana"),
    ),
    ("walk_in", ("walking", "walked", "passing", "passed", "nearby", "neighborhood", "block", "lives", "around")),
    ("event", ("flyer", "flyers", "folleto", "event", "church", "school", "outreach")),
    ("doctor", ("doctor", "referral", "referred", "clinic", "hospital", "physician", "surgeon", "medico", "ortho")),
    ("google", ("google", "search", "internet", "online", "website", "yelp")),
)
_STOP = {"other", "otros", "otro", "please", "specify", "type", "name", "office", "staff", "clinic", "mouth", "word", "event", "outreach", "especifique"}


def _fuzzy_map(folded: str) -> str | None:
    from .labels import edit_distance

    tokens = [t for t in re.findall(r"[a-z]+", folded) if len(t) >= 5 and t not in _STOP]
    for code, stems in _STEMS:
        for token in tokens:
            for stem in stems:
                if len(stem) < 5 or abs(len(token) - len(stem)) > 2:
                    continue
                limit = 1 if len(stem) < 6 else 2
                if edit_distance(token, stem, limit) <= limit:
                    return code
    return None


def looks_like_name(text: str) -> bool:
    """Two or more alphabetic tokens with no keyword: probably a person/practice name."""
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{2,}", text)
    return len(tokens) >= 1 and map_text(text) is None


def writein_area(
    hit: LabelHit, kind: str, th: int, width: int, height: int, bounds: tuple | None = None
) -> tuple[int, int, int, int]:
    """Where handwriting for this label would sit. `bounds` are the bottom of the printed line
    above, the top of the printed line below and (optionally) the start of the next label on the
    same line, so the strip never reaches a neighbour's text."""
    above, below = (bounds[0], bounds[1]) if bounds else (None, None)
    right_stop = bounds[2] if bounds and len(bounds) > 2 else None
    if kind == "below":
        x0 = max(0, hit.x0 - int(th * 0.2))
        x1 = min(width, hit.x0 + int(th * 16))
        y0 = min(height, hit.y1 + int(th * 0.1))
        y1 = min(height, hit.y1 + int(th * 2.6))
        if below is not None:
            y1 = max(y0 + 1, min(y1, int(below - th * 0.15)))
    else:
        x0 = min(width, hit.x1 + int(th * 0.2))
        x1 = max(x0 + 1, width - int(th * 0.3))
        if right_stop is not None:
            x1 = max(x0 + 1, min(x1, int(right_stop - th * 0.3)))
        x1 = min(x1, x0 + int(th * 20))  # handwriting stays near the label; page borders do not count
        y0 = max(0, int(hit.cy - th * 0.8))
        y1 = min(height, int(hit.cy + th * 0.95))
        if above is not None:
            y0 = min(y1 - 1, max(y0, int(above + th * 0.1)))
        if below is not None:
            y1 = max(y0 + 1, min(y1, int(below - th * 0.1)))
    return x0, y0, x1, y1


def _tokens(phrases) -> set[str]:
    out: set[str] = set()
    for phrase in phrases:
        for token in re.split(r"[^a-z']+", _fold(phrase)):
            token = token.strip("'")
            if len(token) >= 3:
                out.add(token)
    return out


def _printed_boxes(words: list[dict], area: tuple[int, int, int, int], hit: LabelHit, kind: str = "inline", th: int = 0, family=None) -> list[tuple[int, int, int, int]]:
    """Word boxes inside the area that are printed helper text or the label itself.

    Only the helper phrases and this label's own words count as printed: a handwritten "friend"
    on the Other line must survive even though "Friends/Family" is a label elsewhere.
    """
    x0, y0, x1, y1 = area
    label_ids = {id(w) for w in hit.words}
    boxes: list[tuple[int, int, int, int]] = []
    inside = sorted((w for w in words if w["x"] < x1 and w["x"] + w["w"] > x0 and w["y"] < y1 and w["y"] + w["h"] > y0), key=lambda w: w["x"])
    joined = normalize(" ".join(w["text"] for w in inside))
    hint_hit = any(similarity(joined[: len(normalize(h)) + 4], normalize(h)) >= 0.6 for h in _PRINTED_HINTS) if joined else False
    tokens = _tokens(_PRINTED_HINTS) | _tokens(hit.option.phrases)
    other_tokens = {t for t in _tokens(p for o in (family.options + family.booking) for p in o.phrases) if len(t) >= 5} - tokens if family is not None else set()
    th = th or hit.text_h
    printed_boxes_x1: list[int] = []
    for w in inside:
        key = normalize(w["text"])
        printed = id(w) in label_ids
        on_label_line = abs((w["y"] + w["h"] / 2) - hit.cy) <= th * 0.5
        clip_x1 = None
        if not printed and kind == "inline" and on_label_line and w["x"] < hit.x1 + th * 0.35:
            printed = True  # the rest of a label whose OCR split in two ("Clinic" | "staff")
            if w["x"] + w["w"] > hit.x1 + th * 1.5:
                clip_x1 = int(hit.x1 + th * 0.35)  # the word ran on into the handwriting
        if not printed and kind == "below" and w["x"] > hit.x0 + th * 11:
            continue  # right of the helper line: handwriting, never print
        if not printed and len(key) >= 4:
            printed = any((key in tk or tk in key) for tk in tokens if len(tk) >= 4) or any(similarity(key, tk) >= 0.75 for tk in tokens if len(tk) >= 4)
        if not printed and len(key) == 3:
            printed = key in tokens
        if not printed and len(key) >= 5 and float(w.get("conf", 0)) >= 70:
            printed = any(similarity(key, tk) >= 0.85 for tk in other_tokens)  # "Outreach" from the row above
        if not printed and len(key) <= 3:
            near_printed = any(abs(w["x"] - px1) <= th * 0.6 for px1 in printed_boxes_x1)
            printed = hint_hit or near_printed  # bracket and punctuation scraps of the printed helper line
        if printed:
            x_end = min(w["x"] + w["w"], clip_x1) if clip_x1 is not None else w["x"] + w["w"]
            if kind == "below":
                x_end = min(x_end, int(hit.x0 + th * 11))  # the helper line is at most this wide
            if x_end > w["x"]:
                boxes.append((w["x"], w["y"], x_end, w["y"] + w["h"]))
                printed_boxes_x1.append(x_end)
    return boxes


def detect(
    ink: np.ndarray,
    gray: np.ndarray,
    words: list[dict],
    hit: LabelHit,
    kind: str,
    th: int,
    lang: str,
    bounds: tuple[int | None, int | None] | None = None,
    erase: np.ndarray | None = None,
    trace: list | None = None,
    family=None,
) -> WriteIn | None:
    """Return a WriteIn when real ink sits in the write-in area, else None.

    Printed rules and their fragments (skewed scans defeat the long-run mask) never count;
    an answer needs either readable letters or clearly handwriting-sized ink.
    """
    height, width = ink.shape
    area = writein_area(hit, kind, th, width, height, bounds)
    x0, y0, x1, y1 = area
    if x1 - x0 < th or y1 - y0 < th * 0.5:
        if trace is not None:
            trace.append((hit.code, kind, area, 0, 0, 0.0, "", 0.0, False))
        return None
    region = ink[y0:y1, x0:x1].copy()
    boxes = _printed_boxes(words, area, hit, kind, th, family)
    for bx0, by0, bx1, by1 in boxes:
        rx0 = max(0, bx0 - 2 - x0)
        rx1 = min(region.shape[1], bx1 + 2 - x0)
        ry0 = max(0, by0 - 2 - y0)
        ry1 = min(region.shape[0], by1 + 2 - y0)
        if rx1 > rx0 and ry1 > ry0:
            region[ry0:ry1, rx0:rx1] = False
    if not region.any():
        if trace is not None:
            trace.append((hit.code, kind, area, 0, 0, 0.0, "", 0.0, False))
        return None
    labels, count, slices = components(region)
    tall = 0
    total = 0
    # dashes of a dotted write-in line: small flat pieces whose centres share a row
    dashes: list[tuple[int, int]] = []
    for index, sl in enumerate(slices, 1):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        if h <= max(4, th * 0.22) and w <= th * 1.2 and w >= 2:
            dashes.append(((sl[0].start + sl[0].stop) // 2, index))
    dotted: set[int] = set()
    dashes.sort()
    row: list[tuple[int, int]] = []
    for cy, index in dashes + [(10**9, -1)]:
        if row and cy - row[-1][0] > 3:
            if len(row) >= 4:
                dotted |= {i for _, i in row}
            row = []
        row.append((cy, index))
    for index, sl in enumerate(slices, 1):
        if sl is None:
            continue
        if index in dotted:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        pixels = int((labels[sl] == index).sum())
        if pixels < 4:
            continue
        if (h <= max(4, 0.12 * w) and w >= 2.5 * th) or (h <= 3 and w >= th):
            continue  # a printed rule or a fragment of one
        total += pixels
        if w <= 3 and h >= th * 0.45:
            continue  # a dash of a printed border or a binder line, not a letter
        if (h >= th * 0.45 and h >= 0.25 * w) or (w >= th * 1.5 and h >= th * 0.3):
            tall += 1
    def note(text: str, conf: float, accepted: bool) -> None:
        if trace is not None:
            trace.append((hit.code, kind, area, tall, int(total), round(total / (th * th), 2), text[:40], round(conf, 1), accepted))

    if tall == 0 or total < th * th * 0.6:
        note("", 0.0, False)
        return None
    text, conf = ocr_strip(gray, area, lang, erase, boxes)
    if _is_printed_hint(text):
        note(text, conf, False)
        return None  # the strip read the printed helper line, not handwriting
    letters = re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", text)
    strong = len(letters) >= 3 and conf >= 45
    some = len(letters) >= 3 and conf >= 30
    if strong:
        pass
    elif kind == "inline" and some and total >= th * th * 1.0:
        pass
    elif kind == "inline" and tall >= 2 and total >= th * th * 1.0:
        pass
    elif kind == "below" and tall >= 3 and total >= th * th * 1.0:
        pass  # the helper line under the doctor option OCRs into junk; only real ink counts there
    elif kind == "below" and some and tall >= 2 and total >= th * th * 1.0:
        pass
    else:
        note(text, conf, False)
        return None
    note(text, conf, True)
    return WriteIn(hit.code, kind, text, conf, map_text(text), total, area)


def stray_ink(ink: np.ndarray, words: list[dict], controls, areas: list[tuple[int, int, int, int]], hits, th: int) -> tuple[int, int] | None:
    """Handwriting that sits nowhere the reader looks: not on a label, not in a control window,
    not on a write-in line. Returns (tall components, ink pixels) when there is enough of it to
    be an answer ("AI search" floating between the columns), else None (R14)."""
    region = ink.copy()
    height, width = region.shape
    pad = max(2, int(th * 0.3))
    for w in words:
        if len(w.get("text", "").strip()) < 1:
            continue
        x0, y0 = max(0, w["x"] - pad), max(0, w["y"] - pad)
        x1, y1 = min(width, w["x"] + w["w"] + pad), min(height, w["y"] + w["h"] + pad)
        region[y0:y1, x0:x1] = False
    for c in controls:
        grow = int(th * 0.9)
        region[max(0, c.y0 - grow) : min(height, c.y1 + grow), max(0, c.x0 - grow) : min(width, c.x1 + grow)] = False
    for x0, y0, x1, y1 in areas:
        region[max(0, y0 - pad) : min(height, y1 + pad), max(0, x0 - pad) : min(width, x1 + pad)] = False
    for h in hits:
        region[max(0, h.y0 - pad) : min(height, h.y1 + pad), max(0, h.x0 - pad) : min(width, h.x1 + int(th * 0.5))] = False
    # the block's own margins carry binder holes, page edges and the question text's remnants
    region[:, : int(th * 1.0)] = False
    region[:, width - int(th * 1.0) :] = False
    region[: int(th * 0.8), :] = False
    region[height - int(th * 0.8) :, :] = False
    if not region.any():
        return None
    labels, count, slices = components(region)
    tall = 0
    total = 0
    for index, sl in enumerate(slices, 1):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        pixels = int((labels[sl] == index).sum())
        if pixels < 6:
            continue
        if (h <= max(4, 0.12 * w) and w >= 2.5 * th) or (w <= 3 and h >= th * 2) or h >= th * 6 or w >= th * 12:
            continue  # rules, border dashes, frames
        total += pixels
        if th * 0.45 <= h <= th * 3 and h >= 0.25 * w:
            tall += 1
    if tall >= 4 and total >= th * th * 1.5:
        return tall, total
    return None


def _is_printed_hint(text: str) -> bool:
    """True when OCR of the write-in strip is (mostly) the printed helper text itself."""
    key = normalize(text)
    if len(key) < 5:
        return False
    for hint in _PRINTED_HINTS:
        h = normalize(hint)
        if not h:
            continue
        window = key[: len(h) + 3]
        if similarity(window, h) >= 0.6 or (len(key) >= 8 and h.endswith(key[-8:])):
            return True
        # a long helper tail like "ctorsname" or "nameoffice" inside the strip
        for part in ("doctorsname", "nameoffice", "typedoctor", "nombredelmedico", "medicooficina"):
            if part in key and len(key) <= len(part) + 10:
                return True
    return False


def ocr_strip(
    gray: np.ndarray,
    area: tuple[int, int, int, int],
    lang: str,
    erase: np.ndarray | None = None,
    boxes: list[tuple[int, int, int, int]] | None = None,
) -> tuple[str, float]:
    """OCR the handwriting strip. Printed rules (`erase`, pixels the line mask removed) and the
    printed word boxes are painted out first so dots and helper text do not become letters."""
    import pytesseract
    from PIL import Image

    x0, y0, x1, y1 = area
    crop = gray[y0:y1, x0:x1].copy()
    if crop.size == 0:
        return "", 0.0
    if erase is not None:
        sub = erase[y0:y1, x0:x1]
        if sub.shape == crop.shape and sub.any():
            labels, count, slices = components(sub)
            th_guess = max(8, (y1 - y0) // 2)
            keep = np.zeros(count + 1, dtype=bool)
            for index, sl in enumerate(slices, 1):
                if sl is None:
                    continue
                h = sl[0].stop - sl[0].start
                w = sl[1].stop - sl[1].start
                if h <= max(4, th_guess * 0.22) and w <= th_guess * 1.2:
                    keep[index] = True  # a dash of a dotted line
            dashes = keep[labels]
            crop[dashes] = 255
    for bx0, by0, bx1, by1 in boxes or ():
        rx0, rx1 = max(0, bx0 - 1 - x0), min(crop.shape[1], bx1 + 1 - x0)
        ry0, ry1 = max(0, by0 - 1 - y0), min(crop.shape[0], by1 + 1 - y0)
        if rx1 > rx0 and ry1 > ry0:
            crop[ry0:ry1, rx0:rx1] = 255
    # upscale the grey strip first (binarising a small crop and scaling the result gives jagged
    # letters), pad it with paper, then try a fixed and an adaptive threshold: pencil and faint
    # pen sit well above 165 while dark pen is fine either way
    big = Image.fromarray(crop).resize((crop.shape[1] * 3, crop.shape[0] * 3), Image.BICUBIC)
    arr = np.asarray(big)
    pad = 12
    canvas = np.full((arr.shape[0] + 2 * pad, arr.shape[1] + 2 * pad), 255, dtype=np.uint8)
    canvas[pad:-pad, pad:-pad] = arr
    thresholds = [165]
    otsu = _otsu(canvas)
    if otsu is not None and abs(otsu - 165) > 12:
        thresholds.append(int(min(205, max(120, otsu))))
    images = [Image.fromarray(np.where(canvas < thr, 0, 255).astype(np.uint8)) for thr in thresholds]

    def run(psm: int, image: Image.Image) -> tuple[str, float]:
        try:
            data = pytesseract.image_to_data(image, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
        except Exception:
            try:
                data = pytesseract.image_to_data(image, lang="eng", config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
            except Exception:
                return "", 0.0
        tokens: list[str] = []
        confs: list[float] = []
        for raw, conf in zip(data["text"], data["conf"]):
            text = (raw or "").strip()
            try:
                value = float(conf)
            except (TypeError, ValueError):
                value = -1.0
            if not text or value < 0:
                continue
            tokens.append(text)
            confs.append(value)
        text = re.sub(r"[_\-–—.]{2,}", " ", " ".join(tokens)).strip()
        return text, (sum(confs) / len(confs) if confs else 0.0)

    def letters_of(text: str) -> int:
        return len(re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", text))

    def score(text: str, conf: float) -> float:
        value = conf * min(letters_of(text), 8)
        if map_text(text) is not None:
            value += 150  # a readable keyword is what the strip is for
        return value

    best_text, best_conf = "", 0.0
    best_image = images[0]
    for image in images:
        text, conf = run(7, image)
        if score(text, conf) > score(best_text, best_conf):
            best_text, best_conf, best_image = text, conf, image
    if best_conf < 50 or letters_of(best_text) < 3 or map_text(best_text) is None:
        alt, alt_conf = run(8, best_image)
        if score(alt, alt_conf) > score(best_text, best_conf):
            best_text, best_conf = alt, alt_conf
    return best_text, best_conf


def _otsu(gray: np.ndarray) -> int | None:
    """Otsu threshold of a grey strip; None when the strip is (nearly) blank."""
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return None
    dark = (gray < 200).sum()
    if dark < 0.005 * total:
        return None
    omega = np.cumsum(hist)
    mu = np.cumsum(hist * np.arange(256))
    mu_t = mu[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma = (mu_t * omega - mu) ** 2 / (omega * (total - omega))
    sigma[~np.isfinite(sigma)] = -1
    return int(np.argmax(sigma))
