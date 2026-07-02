"""Official concept definitions from the FASB us-gaap / SEC dei / srt taxonomies.

Filings ship terse/standard labels but almost never the documentation labels,
so "what does this tag actually mean" needs the authoritative taxonomy files.
We download them once, parse the documentation labels, and cache to SQLite.
"""

from __future__ import annotations

import pathlib
import sqlite3
import threading
import xml.etree.ElementTree as ET

from .util import norm_concept, sec_get

CACHE_DIR = pathlib.Path.home() / ".cache" / "edgar-mcp"
DB_PATH = CACHE_DIR / "taxonomy.sqlite"

# Latest published taxonomy year; concept definitions are stable across years.
SOURCES = {
    "us-gaap": [
        "https://xbrl.fasb.org/us-gaap/2025/elts/us-gaap-doc-2025.xml",
        "https://xbrl.fasb.org/us-gaap/2024/elts/us-gaap-doc-2024.xml",
    ],
    "srt": [
        "https://xbrl.fasb.org/srt/2025/elts/srt-doc-2025.xml",
        "https://xbrl.fasb.org/srt/2024/elts/srt-doc-2024.xml",
    ],
    "dei": [
        "https://xbrl.sec.gov/dei/2025/dei-doc-2025.xml",
        "https://xbrl.sec.gov/dei/2024/dei-doc-2024.xml",
    ],
}

LINK = "{http://www.xbrl.org/2003/linkbase}"
XLINK = "{http://www.w3.org/1999/xlink}"
DOC_ROLE = "http://www.xbrl.org/2003/role/documentation"

_init_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS defs("
        " concept TEXT PRIMARY KEY, definition TEXT, source TEXT)"
    )
    return conn


def _parse_doc_labels(xml_bytes: bytes) -> dict[str, str]:
    """A -doc- linkbase: loc(href#concept) -> labelArc -> label[documentation]."""
    root = ET.fromstring(xml_bytes)
    loc_to_concept: dict[str, str] = {}
    arc_from_to: dict[str, str] = {}
    label_text: dict[str, str] = {}
    for link in root.iter(f"{LINK}labelLink"):
        for loc in link.iter(f"{LINK}loc"):
            href = loc.get(f"{XLINK}href", "")
            frag = href.split("#")[-1]
            loc_to_concept[loc.get(f"{XLINK}label", "")] = frag
        for arc in link.iter(f"{LINK}labelArc"):
            arc_from_to[arc.get(f"{XLINK}from", "")] = arc.get(f"{XLINK}to", "")
        for lab in link.iter(f"{LINK}label"):
            if lab.get(f"{XLINK}role") == DOC_ROLE and lab.text:
                label_text[lab.get(f"{XLINK}label", "")] = lab.text.strip()
    out = {}
    for loc_label, concept in loc_to_concept.items():
        to = arc_from_to.get(loc_label)
        if to and to in label_text:
            out[concept] = label_text[to]
    return out


def _populate(conn: sqlite3.Connection) -> None:
    for source, urls in SOURCES.items():
        n = conn.execute(
            "SELECT COUNT(*) FROM defs WHERE source=?", (source,)
        ).fetchone()[0]
        if n:
            continue
        for url in urls:
            try:
                defs = _parse_doc_labels(sec_get(url).content)
            except Exception:
                continue
            if defs:
                conn.executemany(
                    "INSERT OR REPLACE INTO defs VALUES(?,?,?)",
                    [(c, d, source) for c, d in defs.items()],
                )
                conn.commit()
                break


def definition(concept: str) -> str | None:
    """Official documentation text for a concept, e.g. 'us-gaap:Revenues'."""
    key = norm_concept(concept)
    with _init_lock:
        conn = _connect()
        try:
            _populate(conn)
            row = conn.execute(
                "SELECT definition FROM defs WHERE concept=?", (key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
