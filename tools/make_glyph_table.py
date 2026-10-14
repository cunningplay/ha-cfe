"""Build custom_components/cfe/glyphs.json from local Arial TrueType fonts.

CFE's bill PDFs embed subset Arial fonts whose ToUnicode maps are wrong, so the
integration recognizes each glyph by its outline instead. This dev script hashes
every glyph outline of Arial, Arial Bold, Arial Black and Arial Italic and
writes ``outline hash -> character``. No font data ends up in the table.

    python tools/make_glyph_table.py [font.ttf ...]

Default fonts: macOS ``/System/Library/Fonts/Supplemental/Arial*.ttf``.
Needs fonttools. The hash must match ``pdfbill.glyph_hash``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "custom_components" / "cfe" / "glyphs.json"
FONT_DIR = Path("/System/Library/Fonts/Supplemental")
FONTS = ["Arial.ttf", "Arial Bold.ttf", "Arial Black.ttf", "Arial Italic.ttf"]
EXTRA = "–—‘’“”•¡¿°€…"
CHARS = [chr(c) for c in range(0x20, 0x250) if chr(c).isprintable()] + list(EXTRA)


def glyph_hash(glyph_set, name: str) -> str | None:
    """md5 of the decomposed outline (None for an empty glyph)."""
    pen = DecomposingRecordingPen(glyph_set)
    glyph_set[name].draw(pen)
    if not pen.value:
        return None
    return hashlib.md5(repr(pen.value).encode()).hexdigest()


def main(paths: list[Path]) -> None:
    table: dict[str, str] = {}
    for path in paths:
        font = TTFont(path)
        cmap = font.getBestCmap()
        glyph_set = font.getGlyphSet()
        added = 0
        for char in CHARS:  # ASCII first, so it wins over look-alikes
            name = cmap.get(ord(char))
            if name is None:
                continue
            digest = glyph_hash(glyph_set, name)
            if digest is None or digest in table:
                continue
            table[digest] = char
            added += 1
        print(f"{path.name}: {added} glyphs")
    OUT.write_text(
        json.dumps(dict(sorted(table.items())), ensure_ascii=False, indent=0) + "\n",
        encoding="utf-8",
    )
    print(f"{OUT}: {len(table)} hashes")


if __name__ == "__main__":
    main([Path(p) for p in sys.argv[1:]] or [FONT_DIR / f for f in FONTS])
