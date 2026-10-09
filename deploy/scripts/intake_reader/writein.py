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
    return None


def looks_like_name(text: str) -> bool:
    """Two or more alphabetic tokens with no keyword: probably a person/practice name."""
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{2,}", text)
    return len(tokens) >= 1 and map_text(text) is None


def writein_area(hit: LabelHit, kind: str, th: int, width: int, height: int) -> tuple[int, int, int, int]:
    if kind == "below":
        x0 = max(0, hit.x0 - int(th * 0.2))
        x1 = min(width, hit.x0 + int(th * 16))
        y0 = min(height, hit.y1 + int(th * 0.1))
        y1 = min(height, hit.y1 + int(th * 2.6))
    else:
        x0 = min(width, hit.x1 + int(th * 0.2))
        x1 = max(x0 + 1, min(width, hit.right_limit - int(th * 0.3) if hit.right_limit > hit.x1 + th else width - int(th * 0.3)))
        y0 = max(0, int(hit.cy - th * 0.95))
        y1 = min(height, int(hit.cy + th * 0.8))
    return x0, y0, x1, y1


def _printed_boxes(words: list[dict], area: tuple[int, int, int, int], hit: LabelHit) -> list[tuple[int, int, int, int]]:
    """Word boxes inside the area that are printed helper text or the label itself."""
    x0, y0, x1, y1 = area
    label_ids = {id(w) for w in hit.words}
    boxes: list[tuple[int, int, int, int]] = []
    inside = [w for w in words if w["x"] < x1 and w["x"] + w["w"] > x0 and w["y"] < y1 and w["y"] + w["h"] > y0]
    joined = normalize(" ".join(w["text"] for w in inside))
    hint_hit = any(similarity(joined[: len(normalize(h)) + 4], normalize(h)) >= 0.72 for h in _PRINTED_HINTS) if joined else False
    hint_tokens = {t for h in _PRINTED_HINTS for t in re.split(r"[^a-záéíóúñ']+", h.lower()) if len(t) >= 4}
    for w in inside:
        if id(w) in label_ids:
            boxes.append((w["x"], w["y"], w["x"] + w["w"], w["y"] + w["h"]))
            continue
        key = normalize(w["text"])
        if not key:
            continue
        printed = any(key in normalize(h) and len(key) >= 4 for h in _PRINTED_HINTS)
        if not printed and len(key) >= 4:
            printed = any(similarity(key, normalize(t)) >= 0.75 for t in hint_tokens)
        if not printed and hint_hit and len(key) <= 3:
            printed = True  # bracket and punctuation scraps of the printed helper line
        if printed:
            boxes.append((w["x"], w["y"], w["x"] + w["w"], w["y"] + w["h"]))
    return boxes


def detect(ink: np.ndarray, gray: np.ndarray, words: list[dict], hit: LabelHit, kind: str, th: int, lang: str) -> WriteIn | None:
    """Return a WriteIn when real ink sits in the write-in area, else None."""
    height, width = ink.shape
    area = writein_area(hit, kind, th, width, height)
    x0, y0, x1, y1 = area
    if x1 - x0 < th or y1 - y0 < th * 0.5:
        return None
    region = ink[y0:y1, x0:x1].copy()
    for bx0, by0, bx1, by1 in _printed_boxes(words, area, hit):
        rx0 = max(0, bx0 - 2 - x0)
        rx1 = min(region.shape[1], bx1 + 2 - x0)
        ry0 = max(0, by0 - 2 - y0)
        ry1 = min(region.shape[0], by1 + 2 - y0)
        if rx1 > rx0 and ry1 > ry0:
            region[ry0:ry1, rx0:rx1] = False
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
        if pixels < 4:
            continue
        total += pixels
        if h >= th * 0.45 or w >= th * 1.5:
            tall += 1
    if tall == 0 or total < th * th * 1.0:
        return None
    text, conf = ocr_strip(gray, area, lang)
    letters = re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", text)
    if len(letters) < 2 and total < th * th * 1.5:
        return None  # a stroke of the underline or a stray mark, not an answer
    if conf < 20 and total < th * th * 3:
        return None  # dotted line or scanner noise read as nonsense
    return WriteIn(hit.code, kind, text, conf, map_text(text), total, area)


def ocr_strip(gray: np.ndarray, area: tuple[int, int, int, int], lang: str) -> tuple[str, float]:
    import pytesseract
    from PIL import Image

    x0, y0, x1, y1 = area
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return "", 0.0
    image = Image.fromarray(np.where(crop < 165, 0, 255).astype(np.uint8))
    image = image.resize((image.width * 3, image.height * 3), Image.BICUBIC)
    try:
        data = pytesseract.image_to_data(image, lang=lang, config="--psm 7", output_type=pytesseract.Output.DICT)
    except Exception:
        try:
            data = pytesseract.image_to_data(image, lang="eng", config="--psm 7", output_type=pytesseract.Output.DICT)
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
