"""Turn VW's EU Data Act data dictionary PDF into the JSON the integration ships.

The dictionary ("List of Continuous Data") documents every data point a
continuous data request can deliver, as a table of::

    Key | Data Point Name | Description | Measurement Unit | Tech. Data Type |
    Data Cluster

The key is the same UUID that appears as ``key`` in delivered datasets. The
output maps it to the rest of the row::

    {"<key>": {"name": ..., "description": ..., "unit": ..., "type": ...,
               "clusters": [...]}}

Only this dev tool needs pdfplumber; the integration reads the JSON. The PDF
itself is not committed. Download it from the portal's data catalogue.

Usage::

    pip install pdfplumber
    python tools/parse_data_dictionary.py <dictionary.pdf>
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

import pdfplumber

OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "vwg_eu_data_act"
    / "data_dictionary.json"
)

_KEY_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")

#: Encoding damage in the PDF's text: UTF-8 read as Latin-1, and characters
#: that were already lost to U+FFFD before the PDF was made.
_MOJIBAKE = (
    ("ï¿½C", "°C"),
    ("Â°", "°"),
    ("ï¿½", "'"),
    ("Â", " "),
)

#: The type column spells the same type several ways.
_TYPES = {
    "string": "string",
    "int": "int",
    "unsignedint": "int",
    "float": "float",
    "boolean": "boolean",
    "enum": "enum",
    "datetime": "datetime",
    "object": "object",
}


def _text(cell: str | None) -> str:
    """Collapse a wrapped cell into one line and repair its encoding."""
    text = html.unescape(cell or "")
    for broken, fixed in _MOJIBAKE:
        text = text.replace(broken, fixed)
    return re.sub(r"\s+", " ", text).strip()


def _name(cell: str | None) -> str:
    """Join a wrapped data point name.

    Identifiers such as ``remaining_climatisation_time`` are wrapped
    mid-word, so their line breaks are dropped. Friendly names such as
    ``Remaining Charge Time Complete`` already contain spaces, so theirs
    become spaces too.
    """
    lines = [line.strip() for line in (cell or "").splitlines() if line.strip()]
    separator = " " if any(" " in line for line in lines) else ""
    return _text(separator.join(lines))


def _unit(cell: str | None) -> str | None:
    """Return the unit without the brackets some rows put around it."""
    unit = _text(cell).strip("'").strip()
    if unit.startswith("(") and unit.endswith(")"):
        unit = unit[1:-1].strip()
    return unit if unit and unit != "-" else None


def _type(cell: str | None) -> str | None:
    return _TYPES.get(_text(cell).strip("'").lower())


def parse(pdf_path: Path) -> dict[str, dict]:
    """Return the dictionary rows, keyed by data point key."""
    rows: dict[str, dict] = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if len(row) != 6:
                        continue
                    key = re.sub(r"\s+", "", row[0] or "")
                    if not _KEY_RE.match(key):
                        continue
                    clusters = [c.strip() for c in _text(row[5]).split(",")]
                    rows[key] = {
                        "name": _name(row[1]),
                        "description": _text(row[2]) or None,
                        "unit": _unit(row[3]),
                        "type": _type(row[4]),
                        "clusters": [c for c in clusters if c and c != "All Data"],
                    }
    return rows


def main() -> int:
    """Parse the PDF given on the command line and write the JSON."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    rows = parse(args.pdf)
    if not rows:
        print("No data points found; has the PDF layout changed?", file=sys.stderr)
        return 1
    args.output.write_text(
        json.dumps(dict(sorted(rows.items())), indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(rows)} data points to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
