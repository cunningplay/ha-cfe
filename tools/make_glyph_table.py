"""Build custom_components/cfe/glyphs.json from local Arial TrueType fonts.

CFE's bill PDFs embed subset Arial fonts whose ToUnicode maps are wrong, so the
integration recognizes each glyph by its outline instead. This dev script hashes
every glyph outline of Arial, Arial Bold, Arial Black and Arial Italic and
writes ``outline hash -> character``. No font data ends up in the table.

    python tools/make_glyph_table.py [font.ttf ...]

Default fonts: macOS ``/System/Library/Fonts/Supplemental/Arial*.ttf``.
Needs fonttools. The hash is ``pdfbill.glyph_hash`` (imported, not copied).

The table holds one-way md5 hashes of glyph outlines and the characters they
stand for: no outlines, metrics or other font data.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "custom_components" / "cfe"
OUT = PACKAGE_DIR / "glyphs.json"
FONT_DIR = Path("/System/Library/Fonts/Supplemental")
FONTS = ["Arial.ttf", "Arial Bold.ttf", "Arial Black.ttf", "Arial Italic.ttf"]
EXTRA = "–—‘’“”•¡¿°€…"
CHARS = [chr(c) for c in range(0x20, 0x250) if chr(c).isprintable()] + list(EXTRA)


def _load_pdfbill():
    """pdfbill without the integration's __init__ (which imports Home Assistant)."""
    package = types.ModuleType("cfe_pure")
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules["cfe_pure"] = package
    for name in ("parser", "pdfbill"):
        spec = importlib.util.spec_from_file_location(f"cfe_pure.{name}", PACKAGE_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


glyph_hash = _load_pdfbill().glyph_hash


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
