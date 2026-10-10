"""Synthetic intake blocks for tests that must run without tesseract.

`draw_block` paints labels with a real font and returns the word boxes it used, so the label
matcher, control finder, write-in detector and decision logic can be exercised on known ink.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def font(size: int = 22):
    for path in _FONT_CANDIDATES:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


@dataclass
class Spec:
    """One option row: label text, control kind and what to draw on it."""

    label: str
    mark: str = ""  # "", "check", "thin", "x", "slash", "dot", "spill", "scribble", "circle_label"
    writein: str = ""  # handwriting-like text after the label
    column: int = 0
    indent: int = 0  # extra x offset of control and label (sub-options such as the flyer rows)
    rule: str = ""  # "solid" | "dotted": a printed write-in line after the label, even when blank


@dataclass
class Block:
    image: Image.Image
    gray: np.ndarray
    words: list[dict]
    text_h: int
    rows: list[tuple[Spec, tuple[int, int, int, int]]] = field(default_factory=list)


def _words_for(draw, text: str, x: int, y: int, fnt, line_key) -> list[dict]:
    words: list[dict] = []
    cursor = x
    for token in text.split(" "):
        if not token:
            cursor += draw.textlength(" ", font=fnt)
            continue
        bbox = draw.textbbox((cursor, y), token, font=fnt)
        draw.text((cursor, y), token, fill=0, font=fnt)
        words.append({"text": token, "x": int(bbox[0]), "y": int(bbox[1]), "w": int(bbox[2] - bbox[0]), "h": int(bbox[3] - bbox[1]), "conf": 95.0, "line": line_key})
        cursor = bbox[2] + draw.textlength(" ", font=fnt)
    return words


def draw_block(
    question: str,
    specs: list[Spec],
    control: str = "box",
    booking: tuple[str, list[Spec]] | None = None,
    width: int = 900,
    size: int = 22,
    control_px: int | None = None,
    paper: int = 255,
    print_gray: int = 0,
    skew_deg: float = 0.0,
    highlighter: bool = False,
    binder_line: bool = False,
    dotted_writein: bool = False,
    column_x: tuple[int, ...] = (60, 480),
    gap: int = 12,
    border_dashes: bool = False,
    control_dy: int = 0,
) -> Block:
    fnt = font(size)
    rows_total = len(specs) + 2 + (len(booking[1]) + 2 if booking else 0)
    line_h = int(size * 1.9)
    height = rows_total * line_h + 60
    image = Image.new("L", (width, height), paper)
    draw = ImageDraw.Draw(image)
    side = control_px or int(size * 1.0)
    words: list[dict] = []
    rows: list[tuple[Spec, tuple[int, int, int, int]]] = []
    y = 20
    line_no = 0

    def draw_control(x: int, yc: int, spec: Spec) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = x, yc - side // 2, x + side, yc + side // 2
        if control == "box":
            draw.rectangle((x0, y0, x1, y1), outline=print_gray, width=2)
        elif control == "circle":
            draw.ellipse((x0, y0, x1, y1), outline=print_gray, width=2)
        else:  # bullet
            r = max(3, side // 3)
            draw.ellipse((x0 + side // 2 - r, yc - r, x0 + side // 2 + r, yc + r), outline=print_gray, width=1)
        pen = 0
        if spec.mark == "check":
            draw.line((x0 + 4, yc, x0 + side // 2, y1 - 3), fill=pen, width=3)
            draw.line((x0 + side // 2, y1 - 3, x1 + 2, y0 - 2), fill=pen, width=3)
        elif spec.mark == "thin":
            draw.line((x0 + 4, yc, x0 + side // 2, y1 - 4), fill=pen, width=1)
            draw.line((x0 + side // 2, y1 - 4, x1 - 3, y0 + 2), fill=pen, width=1)
        elif spec.mark == "x":
            draw.line((x0 + 3, y0 + 3, x1 - 3, y1 - 3), fill=pen, width=2)
            draw.line((x0 + 3, y1 - 3, x1 - 3, y0 + 3), fill=pen, width=2)
        elif spec.mark == "slash":
            draw.line((x0 + 2, y1 + 4, x1 + 6, y0 - 8), fill=pen, width=2)
        elif spec.mark == "dot":
            r = max(2, side // 3)
            draw.ellipse((x0 + side // 2 - r, yc - r, x0 + side // 2 + r, yc + r), fill=pen)
        elif spec.mark == "spill":
            draw.line((x0 + side // 2, yc, x1 + side // 2, y0 - side // 2), fill=pen, width=3)
        elif spec.mark == "scribble":
            for k in range(0, side, 3):
                draw.line((x0 + 2, y0 + k, x1 - 2, y0 + k + 2), fill=pen, width=1)
        return x0, y0, x1, y1

    def row(text: str, spec: Spec | None, x_label: int, x_ctrl: int | None):
        nonlocal y, line_no
        ws = _words_for(draw, text, x_label, y, fnt, (1, 1, line_no))
        words.extend(ws)
        yc = y + size // 2 + 2
        box = None
        if spec is not None and x_ctrl is not None:
            box = draw_control(x_ctrl, yc + control_dy, spec)
            rows.append((spec, box))
            if spec.mark == "circle_label":
                x0 = min(w["x"] for w in ws) - 8
                x1 = max(w["x"] + w["w"] for w in ws) + 8
                draw.ellipse((x0, y - 6, x1, y + size + 8), outline=0, width=2)
            if spec.writein or spec.rule:
                wx = max(w["x"] + w["w"] for w in ws) + 14
                if dotted_writein or spec.rule == "dotted":
                    for dx in range(wx, min(width - 20, wx + 260), 6):
                        draw.line((dx, y + size, dx + 3, y + size), fill=0, width=1)
                elif spec.rule == "solid":
                    draw.line((wx, y + size + 2, min(width - 20, wx + 300), y + size + 2), fill=0, width=2)
            if spec.writein:
                hw = font(size + 4)
                draw.text((wx + 6, y - 6), spec.writein, fill=0, font=hw)
        return ws

    row(question, None, column_x[0] - 30, None)
    y += line_h
    line_no += 1
    if booking:
        bq, bspecs = booking
        # booking goes above the hear question on the old forms: redraw order
    # draw hear options, two columns when requested
    col_rows: dict[int, list[Spec]] = {}
    for spec in specs:
        col_rows.setdefault(spec.column, []).append(spec)
    max_rows = max(len(v) for v in col_rows.values())
    for i in range(max_rows):
        y_row = y
        for col, items in col_rows.items():
            if i >= len(items):
                continue
            spec = items[i]
            x_ctrl = column_x[col] + spec.indent
            yy = y
            y = y_row
            row(spec.label, spec, x_ctrl + side + gap, x_ctrl)
            y = yy
        y += line_h
        line_no += 1
    if binder_line:
        draw.line((column_x[0] - 14, 0, column_x[0] - 14, height), fill=0, width=3)
    if border_dashes:
        for dy in range(0, height, 26):
            draw.line((width - 30, dy, width - 30, dy + 14), fill=0, width=2)
    if highlighter:
        band = Image.new("L", (width, line_h), 200)
        image.paste(Image.blend(image.crop((0, 10, width, 10 + line_h)), band, 0.45), (0, 10))
    if skew_deg:
        image = image.rotate(skew_deg, resample=Image.BILINEAR, fillcolor=paper, expand=False)
        # PIL rotates counter-clockwise about the centre; move the word and control boxes with it.
        import math

        cx, cy = width / 2, height / 2
        rad = math.radians(skew_deg)

        def turn(x: float, y: float) -> tuple[float, float]:
            dx, dy = x - cx, y - cy
            return cx + dx * math.cos(rad) + dy * math.sin(rad), cy - dx * math.sin(rad) + dy * math.cos(rad)

        def turn_box(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
            pts = [turn(x0, y0), turn(x1, y0), turn(x0, y1), turn(x1, y1)]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))

        for w in words:
            x0, y0, x1, y1 = turn_box(w["x"], w["y"], w["x"] + w["w"], w["y"] + w["h"])
            w.update({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0})
        rows = [(spec, turn_box(*box)) for spec, box in rows]
    gray = np.asarray(image).astype(np.uint8)
    return Block(image, gray, words, size, rows)
