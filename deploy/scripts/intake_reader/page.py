"""Page images: rendering, orientation (R1), binarization and line masks (R6).

Everything downstream works on an upright grayscale numpy array.
"""

from __future__ import annotations

import re

import numpy as np

try:  # scipy is present in the scraper image and locally; tests can run without it only for pure logic
    from scipy import ndimage
except Exception:  # pragma: no cover
    ndimage = None

_EIGHT = np.ones((3, 3), dtype=bool)

# Words that appear on every intake layout; used to judge whether OCR text is upright.
_KNOWN = frozenset(
    """
    the and you your name date birth address phone insurance patient please how did hear about us
    doctor google social media word mouth other city state zip email signature information
    emergency contact referral zocdoc event outreach walk check what applies book appointment
    website text primary secondary member group number policy holder relationship self spouse
    child yes with for from this that have been received physical therapy year elsewhere
    nombre fecha nacimiento direccion dirección telefono teléfono seguro paciente como cómo
    google medico médico firma correo ciudad estado codigo número numero contacto emergencia
    informacion información referencia personal evento otro otros redes sociales cita
    """.split()
)


def render(doc, index: int, zoom: float) -> np.ndarray:
    """Grayscale page as uint8 array (rows, cols)."""
    import fitz

    pix = doc[index].get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()


def rotate(gray: np.ndarray, angle: int) -> np.ndarray:
    """Rotate clockwise by `angle` degrees (0, 90, 180, 270)."""
    k = (angle // 90) % 4
    if k == 0:
        return gray
    return np.ascontiguousarray(np.rot90(gray, k=-k))


def known_word_ratio(text: str) -> float:
    tokens = re.findall(r"[a-záéíóúñü]{3,}", text.lower())
    if len(tokens) < 8:
        return 0.0
    return sum(1 for tok in tokens if tok in _KNOWN) / len(tokens)


def osd_angle(gray: np.ndarray, lang_hint: str = "eng") -> tuple[int, float] | None:
    """Tesseract orientation detection. Returns (clockwise angle to apply, confidence) or None."""
    import pytesseract
    from PIL import Image

    try:
        out = pytesseract.image_to_osd(Image.fromarray(gray), config="--psm 0")
    except Exception:
        return None
    rot = re.search(r"Rotate:\s*(\d+)", out)
    conf = re.search(r"Orientation confidence:\s*([\d.]+)", out)
    if not rot:
        return None
    return int(rot.group(1)) % 360, float(conf.group(1)) if conf else 0.0


def ocr_text(gray: np.ndarray, lang: str, psm: int = 3) -> str:
    import pytesseract
    from PIL import Image

    try:
        return pytesseract.image_to_string(Image.fromarray(gray), lang=lang, config=f"--psm {psm}") or ""
    except Exception:
        if lang != "eng":
            return ocr_text(gray, "eng", psm)
        return ""


def ocr_words(gray: np.ndarray, lang: str, psm: int = 3) -> list[dict]:
    """OCR word boxes: dicts with text, x, y, w, h, conf, line (tesseract block/para/line key)."""
    import pytesseract
    from PIL import Image

    try:
        data = pytesseract.image_to_data(
            Image.fromarray(gray), lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT
        )
    except Exception:
        if lang != "eng":
            return ocr_words(gray, "eng", psm)
        return []
    words: list[dict] = []
    for index, raw in enumerate(data["text"]):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][index])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            continue
        words.append(
            {
                "text": text,
                "x": int(data["left"][index]),
                "y": int(data["top"][index]),
                "w": int(data["width"][index]),
                "h": int(data["height"][index]),
                "conf": conf,
                "line": (int(data["block_num"][index]), int(data["par_num"][index]), int(data["line_num"][index])),
            }
        )
    return words


def upright(gray: np.ndarray, lang: str, quick_zoom_ratio: float = 0.7) -> tuple[np.ndarray, int, str]:
    """Return (upright image, applied clockwise angle, how it was decided).

    Intake forms are portrait pages: a landscape render means a 90/270 rotation, and only those
    two are compared. Tesseract reads sideways text surprisingly well, so the known-word ratio
    is used only to pick between the two candidates OSD leaves open, never to overrule the
    portrait/landscape fact.
    """
    height, width = gray.shape
    landscape = width > height * 1.1
    osd = osd_angle(gray)
    small = _downscale(gray, quick_zoom_ratio)

    def ratio(angle: int) -> float:
        return known_word_ratio(ocr_text(rotate(small, angle), lang))

    if landscape:
        scores = {a: ratio(a) for a in (90, 270)}
        if osd is not None and osd[0] in (90, 270) and osd[1] >= 1.0 and scores[osd[0]] >= 0.15:
            return rotate(gray, osd[0]), osd[0], f"osd:{osd[1]:.1f}"
        best = max(scores, key=scores.get)
        return rotate(gray, best), best, f"words90:{scores[90]:.2f}/{scores[270]:.2f}"
    if osd is not None and osd[1] >= 1.0 and osd[0] in (0, 180):
        return rotate(gray, osd[0]), osd[0], f"osd:{osd[1]:.1f}"
    scores = {a: ratio(a) for a in (0, 180)}
    best = max(scores, key=scores.get)
    return rotate(gray, best), best, f"words:{scores[0]:.2f}/{scores[180]:.2f}"


def _downscale(gray: np.ndarray, ratio: float) -> np.ndarray:
    if ratio >= 0.999:
        return gray
    from PIL import Image

    image = Image.fromarray(gray)
    size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
    return np.asarray(image.resize(size, Image.BILINEAR))


def binarize(gray: np.ndarray, threshold: int = 150) -> np.ndarray:
    """Ink mask (True = ink). 150 drops yellow/pink highlighter and paper texture."""
    return gray < threshold


def components(ink: np.ndarray):
    """Label 8-connected components. Returns (labels, count, slices)."""
    labels, count = ndimage.label(ink, structure=_EIGHT)
    return labels, count, ndimage.find_objects(labels)


def mask_lines(ink: np.ndarray, text_h: int) -> np.ndarray:
    """Remove binder lines, solid rules and dotted write-in lines (R6).

    Solid rules: height <= 3 px and width >= 6 text heights. Binder lines: width <= 0.45 text
    heights (at least 8 px) and height >= 8 text heights. Dotted rules: a row of >= 6 short dashes spanning >= 6
    text heights.
    """
    if not ink.any():
        return ink
    th = max(8, int(text_h))
    ink = _mask_projections(ink, th)
    labels, count, slices = components(ink)
    if count == 0:
        return ink
    drop = np.zeros(count + 1, dtype=bool)
    dashes: list[tuple[int, int, int, int]] = []
    for index, sl in enumerate(slices, 1):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        if h <= 3 and w >= 6 * th:
            drop[index] = True
        elif w <= max(12, int(th * 0.6)) and h >= 8 * th:
            drop[index] = True
        elif h <= 3 and 3 <= w <= th:
            dashes.append(((sl[0].start + sl[0].stop) // 2, sl[1].start, sl[1].stop, index))
    dashes.sort()
    row: list[tuple[int, int, int, int]] = []

    def flush(group: list[tuple[int, int, int, int]]) -> None:
        if len(group) >= 6 and (max(g[2] for g in group) - min(g[1] for g in group)) >= 6 * th:
            for g in group:
                drop[g[3]] = True

    for dash in dashes:
        if row and dash[0] - row[-1][0] > 2:
            flush(row)
            row = []
        row.append(dash)
    flush(row)
    if not drop.any():
        return ink
    return ink & ~drop[labels]


def clean_for_ocr(gray: np.ndarray, text_h: int = 12) -> np.ndarray:
    """Whiten binder lines, rules and dotted lines so they do not break tesseract's layout."""
    ink = binarize(gray)
    kept = mask_lines(ink, text_h)
    removed = ink & ~kept
    if not removed.any():
        return gray
    out = gray.copy()
    out[removed] = 255
    return out


def _long_runs(ink: np.ndarray, length: int, axis: int, max_thick: int) -> np.ndarray:
    """Pixels of thin runs of ink at least `length` long along `axis`.

    Vertical runs may contain 1-2 px gaps (dashed or grey binder lines); horizontal runs may
    not, otherwise a line of text would read as a rule. A run thicker than `max_thick` across
    the other axis is a band or a letter, not a line.
    """
    if axis == 0:
        closed = ink | np.roll(ink, 1, axis=0) | np.roll(ink, -1, axis=0)
        runs = ndimage.binary_opening(closed, structure=np.ones((length, 1), dtype=bool))
        thick = ndimage.binary_opening(runs, structure=np.ones((1, max_thick + 1), dtype=bool))
    else:
        runs = ndimage.binary_opening(ink, structure=np.ones((1, length), dtype=bool))
        thick = ndimage.binary_opening(runs, structure=np.ones((max_thick + 1, 1), dtype=bool))
    return runs & ~thick


def _mask_projections(ink: np.ndarray, th: int) -> np.ndarray:
    """Whiten binder lines and long rules, and nothing else.

    A scanner line is a thin continuous vertical run of ink several text heights long, even
    when letters touch it (so the connected component is no longer thin) and even when it is
    grey or doubled; a rule is a thin long horizontal run. Only the run pixels plus a small
    halo are removed, so a circle on the same row as a dotted line keeps its shape.
    """
    height, width = ink.shape
    out = ink
    if height >= 4 * th:
        vertical = _long_runs(ink, int(3.5 * th), 0, max(12, int(th * 0.6)))
        if vertical.any():
            halo = ndimage.binary_dilation(vertical, structure=np.ones((3, 5), dtype=bool))
            out = out & ~halo
    if width >= 12 * th:
        horizontal = _long_runs(out, int(8 * th), 1, 4)
        if horizontal.any():
            halo = ndimage.binary_dilation(horizontal, structure=np.ones((3, 3), dtype=bool))
            out = out & ~halo
    return out


def median_text_height(words: list[dict], fallback: int = 18) -> int:
    heights = sorted(min(int(w["h"]), 60) for w in words if int(w["h"]) >= 6 and re.search(r"[A-Za-z]{2,}", w["text"]))
    if not heights:
        return fallback
    return max(8, heights[len(heights) // 2])
