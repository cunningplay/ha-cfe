"""Load the pure modules (parser, pdfbill, mail, gmail) without importing Home Assistant."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "custom_components" / "cfe"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A bare package (its __init__ imports Home Assistant and is skipped) so that
# pdfbill's relative import of parser works.
_package = types.ModuleType("cfe_pure")
_package.__path__ = [str(PACKAGE_DIR)]
sys.modules["cfe_pure"] = _package


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cfe_parser = _load("cfe_pure.parser", PACKAGE_DIR / "parser.py")
cfe_pdfbill = _load("cfe_pure.pdfbill", PACKAGE_DIR / "pdfbill.py")
cfe_mail = _load("cfe_mail", PACKAGE_DIR / "mail.py")
cfe_gmail = _load("cfe_gmail", PACKAGE_DIR / "gmail.py")


@pytest.fixture
def bill_xml() -> bytes:
    return (FIXTURES / "bill_anon.xml").read_bytes()


@pytest.fixture
def bill_pdf_text() -> str:
    return (FIXTURES / "bill_pdf_text_anon.txt").read_text(encoding="utf-8")
