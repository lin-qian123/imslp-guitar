from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfWriter


ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_text_fixture(relative: str) -> str:
    return (ROOT / "tests" / "fixtures" / relative).read_text(encoding="utf-8")


def make_file_template(filename: str, file_id: str = "1") -> str:
    return "{{#fte:imslpfile\n|File Name 1=" + filename + "\n|File ID=" + file_id + "\n}}"


def make_wikitext(files_body: str, instrumentation: str = "") -> str:
    return "| *****FILES*****\n" + files_body + "\n| *****WORK INFO*****\n|Instrumentation=" + instrumentation


def write_minimal_pdf(path: Path, width: float = 72, height: float = 72) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=height)
    with path.open("wb") as output:
        writer.write(output)
    return path
