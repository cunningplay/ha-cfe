"""Load the pure parser module without importing Home Assistant."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cfe_parser = _load("cfe_parser", ROOT / "custom_components" / "cfe" / "parser.py")
cfe_mail = _load("cfe_mail", ROOT / "custom_components" / "cfe" / "mail.py")


@pytest.fixture
def bill_xml() -> bytes:
    return (FIXTURES / "bill_anon.xml").read_bytes()
