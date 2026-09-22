"""VW's data dictionary for continuous data.

VW publishes a "List of Continuous Data" PDF describing every data point the
portal can deliver. ``tools/parse_data_dictionary.py`` turns it into
``data_dictionary.json``, keyed by the same UUID that identifies a point in a
delivered dataset. It gives each point a description, a data type, the
thematic clusters it belongs to and, for about one in ten, a unit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DICTIONARY_PATH = Path(__file__).resolve().parent.parent / "data_dictionary.json"


@dataclass(frozen=True, slots=True)
class DictionaryEntry:
    """What VW documents about one data point."""

    name: str
    description: str | None = None
    unit: str | None = None
    #: One of string, int, float, boolean, enum, datetime or object.
    type: str | None = None
    clusters: tuple[str, ...] = ()


type DataDictionary = dict[str, DictionaryEntry]


def load_data_dictionary(path: Path = DICTIONARY_PATH) -> DataDictionary:
    """Read the dictionary. This does blocking I/O."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: DictionaryEntry(
            name=row["name"],
            description=row.get("description"),
            unit=row.get("unit"),
            type=row.get("type"),
            clusters=tuple(row.get("clusters") or ()),
        )
        for key, row in rows.items()
    }
