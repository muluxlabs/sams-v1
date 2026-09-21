#!/usr/bin/env python3
"""
የፊደል ቅንብር — build the report font.

Noto Sans Ethiopic covers Ethiopic and nothing else: no digits, no Latin
letters, no '.', no '%'. Used alone in a PDF it renders every Amharic word
perfectly and every NUMBER as blank space — and ReportLab raises no error
while doing it. A monthly report would come out with names and empty hour
columns, which is worse than an obviously broken one.

This merges the Ethiopic font with Noto Sans so one font file covers
Ethiopic, Latin, digits and punctuation. Both are 1000 units per em and
share a design, so the merge needs no scaling and the digits sit correctly
against the Amharic. Run once; the output is committed to assets/fonts and
shipped in the installer.

    python tools/build_font.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "assets" / "fonts"

# Ethiopic first so it wins on any overlap; DejaVu fills the Latin gaps.
SOURCES = {
    "SAMSAmharic-Regular.ttf": [
        "NotoSansEthiopic-Regular.ttf",
        "NotoSans-Regular.ttf",
    ],
    "SAMSAmharic-Bold.ttf": [
        "NotoSansEthiopic-Bold.ttf",
        "NotoSans-Bold.ttf",
    ],
}

SEARCH = [
    OUT_DIR,
    Path("/usr/share/fonts/truetype/noto"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("C:/Windows/Fonts"),
]

REQUIRED = "0123456789.,%-:/() የሰራተኛ ስም ሰዓት ጳጉሜ"


def find(name: str) -> Path:
    for base in SEARCH:
        p = base / name
        if p.exists():
            return p
    raise SystemExit(f"font not found: {name}")


def _strip_layout(src: Path, dest: Path) -> Path:
    """
    Drop OpenType layout tables before merging.

    fontTools refuses to merge two fonts whose GSUB/GPOS script records
    disagree, and Ethiopic needs no shaping — every syllable is a single
    codepoint, so there are no ligatures or reordering rules to lose.
    Dropping them costs Latin kerning in the PDF and nothing else.
    """
    from fontTools.ttLib import TTFont

    f = TTFont(str(src))
    for tag in ("GSUB", "GPOS", "GDEF", "BASE", "JSTF", "MATH"):
        if tag in f:
            del f[tag]
    f.save(str(dest))
    f.close()
    return dest


def build() -> None:
    import tempfile

    from fontTools.merge import Merger
    from fontTools.ttLib import TTFont

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    for out_name, sources in SOURCES.items():
        paths = [
            str(_strip_layout(find(s), tmp / f"{i}-{s}"))
            for i, s in enumerate(sources)
        ]
        merged = Merger().merge(paths)

        # Normalise the name table so ReportLab and Windows both see one
        # coherent family rather than the first source's name.
        family = out_name.replace(".ttf", "").replace("-", " ")
        for record in merged["name"].names:
            if record.nameID in (1, 3, 4, 6):
                record.string = family

        out = OUT_DIR / out_name
        merged.save(str(out))
        merged.close()

        check = TTFont(str(out))
        cmap = check.getBestCmap()
        missing = sorted({c for c in REQUIRED if c.strip() and ord(c) not in cmap})
        check.close()
        if missing:
            raise SystemExit(f"{out_name} is still missing glyphs: {missing}")
        print(f"✓ {out_name}  ({out.stat().st_size / 1024:.0f} KB) — "
              f"Ethiopic + Latin + digits")


if __name__ == "__main__":
    build()
