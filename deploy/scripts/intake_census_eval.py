"""Golden-set evaluation of the intake reader.

Reads `golden_map.json` (code -> PDF path, host only) and `golden_labels.tsv` (code -> truth,
in the repo), runs `intake_reader.read_intake` on every file and compares. Writes
`eval_report.json` plus annotated boards of the mismatches so they can be checked by eye.
Exit status 1 when any confident label mismatches.

Usage (inside the scraper container):
  python intake_census_eval.py --map /data/exports/intake_census_v2/golden_map.json \
      --out /data/exports/intake_census_v2/eval [--codes U000,U001] [--workers 4]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HERE = Path(__file__).resolve().parent
DEFAULT_LABELS = HERE / "intake_reader" / "tests" / "golden_labels.tsv"
PREFIX_BUCKET = {
    "U": "unmarked_options",
    "D": "doctor",
    "G": "google",
    "F": "friend_family",
    "Z": "zocdoc",
    "I": "insurance",
    "E": "event",
    "O": "other",
    "S": "social_media",
    "W": "walk_in",
    "P": "phone",
    "B": "website",
    "K": "blank",
    "R": "unreadable",
    "T": "first_rows",
}


def load_labels(path: Path) -> dict[str, tuple[str, str]]:
    labels: dict[str, tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = (line.split("\t") + ["", ""])[:3]
        labels[parts[0]] = (parts[1].strip(), parts[2].strip())
    return labels


TEXT_CODES = {"friend_family", "walk_in", "google", "insurance", "doctor", "zocdoc", "event", "website", "social_media", "lives_nearby"}


def compare(truth: str, reading) -> tuple[str, str]:
    """Return (verdict, detail). verdict: ok | miss | soft | skip."""
    got = reading.source
    marks = set(reading.marks)
    reasons = set(getattr(reading, "reasons", []) or [])
    low_conf = truth.endswith("?") and truth != "?"
    base = truth[:-1] if low_conf else truth
    if truth == "?":
        return "skip", "unlabelled"
    if base == "upside_down":
        ok = got not in ("unreadable",)
        return ("ok" if ok else ("soft" if low_conf else "miss")), f"rotated->{got}"
    if base == "no_question":
        ok = got == "no_question"
    elif base == "unmarked":
        ok = got == "unmarked"
    elif base.startswith("multi:"):
        expected = set(base[6:].split("+"))
        ok = got == "multiple" and marks == expected
        if not ok and got == "multiple" and marks & expected:
            return ("soft", f"partial multi {sorted(marks)} vs {sorted(expected)}")
        if not ok and got in expected and len(expected) == 2:
            return ("soft", f"one of two marks: {got}")
    else:
        ok = got == base
        if not ok and got == "multiple" and base in marks:
            return ("soft", f"extra marks {sorted(marks)}")
        if not ok and got == "other" and "other_unmapped" in reasons and base in TEXT_CODES:
            return ("soft", "handwritten answer kept for review")
    if ok:
        return "ok", got
    return ("soft" if low_conf else "miss"), f"got={got} marks={sorted(marks)}"


def _run_one(payload: tuple[str, str, str]) -> dict:
    code, path, lang = payload
    os.environ["OMP_NUM_THREADS"] = "1"
    from intake_reader import read_intake

    started = time.monotonic()
    try:
        result = read_intake(path, lang=lang, want_debug=True)
        reading = result.reading
        debug = reading.debug
        gray = debug.pop("gray", None)
        row = {
            "code": code,
            "source": reading.source,
            "marks": reading.marks,
            "booking": reading.booking,
            "other_text": reading.other_text,
            "family": reading.family,
            "page": reading.page,
            "confidence": reading.confidence,
            "needs_review": reading.needs_review,
            "reasons": reading.reasons,
            "angles": [s.angle for s in result.scanned],
            "how": [s.how for s in result.scanned],
            "method": result.method,
            "secs": round(time.monotonic() - started, 1),
            "debug": {k: v for k, v in debug.items() if k != "gray" and not k.startswith("_")},
        }
        if gray is not None:
            row["_gray"] = gray
        elif reading.source in ("no_question", "unreadable"):
            # nothing was read: show the first page so the board explains the miss
            try:
                import fitz

                from intake_reader.page import render, rotate

                doc = fitz.open(path)
                if len(doc):
                    page0 = render(doc, 0, 0.9)
                    angle = result.scanned[0].angle if result.scanned else 0
                    row["_gray"] = rotate(page0, angle) if angle else page0
                doc.close()
            except Exception:
                pass
        return row
    except Exception as exc:  # keep the eval going
        return {"code": code, "source": "error", "marks": [], "error": f"{type(exc).__name__}: {exc}", "secs": round(time.monotonic() - started, 1)}


def render_board(items: list[dict], dest: Path, tile_w: int = 560) -> None:
    from PIL import Image, ImageDraw, ImageFont

    tiles = []
    for item in items:
        gray = item.get("_gray")
        if gray is None:
            continue
        image = Image.fromarray(gray).convert("RGB")
        draw = ImageDraw.Draw(image)
        debug = item.get("debug", {})
        for code, group, x0, y0, x1, y1, score, *_rest in debug.get("labels", []):
            draw.rectangle((x0, y0, x1, y1), outline=(40, 90, 220) if group == "hear" else (150, 150, 150), width=1)
        for control in debug.get("controls", []):
            code, x0, y0, x1, y1, found, interior, outside, ring, score, marked, reason = control
            color = (0, 170, 0) if marked else (220, 0, 0)
            draw.rectangle((x0, y0, x1, y1), outline=color, width=2 if marked else 1)
            draw.text((x1 + 2, y0 - 2), f"{score:+.1f}", fill=color)
        for code, kind, text, conf, mapped, ink, area in debug.get("writeins", []):
            draw.rectangle(tuple(area), outline=(255, 140, 0), width=2)
        scale = tile_w / image.width
        image = image.resize((tile_w, max(1, int(image.height * scale))))
        bar = 44
        tile = Image.new("RGB", (tile_w, image.height + bar), "white")
        tile.paste(image, (0, bar))
        d = ImageDraw.Draw(tile)
        d.rectangle((0, 0, tile_w, bar - 1), fill=(30, 30, 60))
        try:
            fnt = ImageFont.load_default(size=15)
        except Exception:
            fnt = ImageFont.load_default()
        head = f"{item['code']} truth={item.get('truth')} got={item['source']} {','.join(item.get('marks', []))} conf={item.get('confidence')}"
        d.text((6, 3), head[:90], fill="white", font=fnt)
        d.text((6, 22), (item.get("detail", "") + " " + (item.get("other_text") or ""))[:95], fill=(255, 220, 120), font=fnt)
        tiles.append(tile)
    if not tiles:
        return
    cols = 2
    heights = [0] * cols
    for i, t in enumerate(tiles):
        heights[i % cols] += t.height + 8
    board = Image.new("RGB", (tile_w * cols + 8, max(heights)), (170, 170, 170))
    ys = [0] * cols
    for i, t in enumerate(tiles):
        c = i % cols
        board.paste(t, (c * (tile_w + 8), ys[c]))
        ys[c] += t.height + 8
    board.save(dest, "JPEG", quality=42, optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", default="/data/exports/intake_census_v2/golden_map.json")
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--out", default="/data/exports/intake_census_v2/eval")
    parser.add_argument("--codes", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--lang", default="eng+spa")
    parser.add_argument("--boards", default="miss", choices=["none", "miss", "all"])
    parser.add_argument("--tile", type=int, default=560, help="board tile width in px")
    parser.add_argument("--per-board", type=int, default=4, help="crops per board image")
    args = parser.parse_args()

    mapping = json.loads(Path(args.map).read_text(encoding="utf-8"))
    labels = load_labels(Path(args.labels))
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or sorted(mapping)
    codes = [c for c in codes if c in mapping and c in labels]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"EVAL_START codes={len(codes)} workers={args.workers}", flush=True)
    rows: list[dict] = []
    payloads = [(code, mapping[code], args.lang) for code in codes]
    if args.workers <= 1:
        for payload in payloads:
            rows.append(_run_one(payload))
            print(f"done {rows[-1]['code']} {rows[-1].get('source')} {rows[-1].get('secs')}s", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(_run_one, payload) for payload in payloads]
            for future in as_completed(futures):
                rows.append(future.result())
                if len(rows) % 20 == 0:
                    print(f"progress {len(rows)}/{len(codes)}", flush=True)
    rows.sort(key=lambda r: r["code"])

    verdicts: Counter[str] = Counter()
    per_bucket: dict[str, Counter[str]] = defaultdict(Counter)
    report = []
    misses = []
    softs = []
    for row in rows:
        truth, note = labels[row["code"]]
        row["truth"] = truth
        row["note"] = note
        if row.get("source") == "error":
            verdict, detail = "miss", row.get("error", "error")
        else:
            from intake_reader import Reading

            reading = Reading(source=row["source"], marks=list(row["marks"]), reasons=list(row.get("reasons", [])))
            verdict, detail = compare(truth, reading)
        row["verdict"] = verdict
        row["detail"] = detail
        verdicts[verdict] += 1
        per_bucket[PREFIX_BUCKET.get(row["code"][0], row["code"][0])][verdict] += 1
        if verdict == "miss":
            misses.append(row)
        elif verdict == "soft":
            softs.append(row)
        report.append({k: v for k, v in row.items() if k != "_gray"})
    (out / "eval_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    if args.boards != "none":
        chosen = rows if args.boards == "all" else misses + softs
        per = max(1, args.per_board)
        for index in range(0, len(chosen), per):
            render_board(chosen[index : index + per], out / f"miss_{index // per:02d}.jpg", tile_w=args.tile)
    total = sum(verdicts.values()) - verdicts["skip"]
    print("EVAL_SUMMARY " + " ".join(f"{k}={v}" for k, v in sorted(verdicts.items())) + f" accuracy={(verdicts['ok'] / total if total else 0):.3f}", flush=True)
    for bucket, counts in sorted(per_bucket.items()):
        print(f"bucket {bucket:18s} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
    for row in misses:
        print(f"MISS {row['code']} truth={row['truth']} {row['detail']} fam={row.get('family')} conf={row.get('confidence')} reasons={';'.join(row.get('reasons', []))} note={row['note']}", flush=True)
    for row in softs:
        print(f"SOFT {row['code']} truth={row['truth']} {row['detail']} note={row['note']}", flush=True)
    secs = [r.get("secs", 0) for r in rows]
    if secs:
        print(f"timing mean={sum(secs) / len(secs):.1f}s max={max(secs):.1f}s", flush=True)
    print("EVAL_DONE", flush=True)
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
