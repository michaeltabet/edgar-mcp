"""Semantic search over filing text (LanceDB + fastembed).

The XBRL side answers "what is this number"; this answers "where does the
filing talk about X" — supply-chain concentration, going-concern language,
a specific lawsuit, revenue-recognition policy — even when the wording varies.
Self-contained and offline: fastembed runs a small ONNX embedding model (no
torch, no API key); LanceDB is an embedded vector store on disk. Nothing
leaves the machine.

Index at ~/.cache/edgar-mcp/lancedb. Each row is a chunk of one filing section
with full provenance (accession, company, form, section) so a hit is directly
citable and can be handed to read_section / explain_number for the hard data.
"""

from __future__ import annotations

import pathlib
import threading

from .util import filing_for

LANCE_DIR = pathlib.Path.home() / ".cache" / "edgar-mcp" / "lancedb"
TABLE = "filing_chunks"
MODEL_NAME = "BAAI/bge-small-en-v1.5"  # 384-dim, small ONNX

_lock = threading.Lock()
_embedder = None


def _embed_model():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        _embedder = TextEmbedding(model_name=MODEL_NAME)
    return _embedder


def _embed(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in _embed_model().embed(texts)]


def _connect():
    import lancedb

    LANCE_DIR.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(LANCE_DIR))


def _chunk(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    text = " ".join((text or "").split())
    if not text:
        return []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return out


def index_filing(accession: str, max_chars_per_section: int = 60_000) -> dict:
    """Extract every item/section of a filing, chunk + embed, upsert into the
    vector index. Idempotent per accession (existing chunks are replaced)."""
    accession = accession.strip()
    f = filing_for(accession)
    obj = f.obj()
    items = getattr(obj, "items", None)
    if items is None or callable(items):
        return {"accession": accession, "indexed": 0,
                "note": f"{f.form} has no item extraction (try full-text search)"}

    company = getattr(f, "company", None)
    form = getattr(f, "form", None)
    rows_meta: list[dict] = []
    chunks: list[str] = []
    for section in list(items):
        text = (obj[section] or "")[:max_chars_per_section]
        for ci, ch in enumerate(_chunk(text)):
            rows_meta.append({"accession": accession, "company": company,
                              "form": form, "section": str(section),
                              "chunk_id": f"{section}#{ci}", "text": ch})
            chunks.append(ch)
    if not chunks:
        return {"accession": accession, "indexed": 0, "note": "no text sections"}

    vectors = _embed(chunks)
    rows = [{**m, "vector": v} for m, v in zip(rows_meta, vectors)]

    with _lock:
        db = _connect()
        if TABLE in db.table_names():
            tbl = db.open_table(TABLE)
            tbl.delete(f"accession = '{accession}'")
            tbl.add(rows)
        else:
            tbl = db.create_table(TABLE, data=rows)
    return {"accession": accession, "company": company, "form": form,
            "sections": len(set(m["section"] for m in rows_meta)),
            "indexed": len(rows)}


def search(query: str, k: int = 8, company: str | None = None,
           accession: str | None = None) -> dict:
    """Semantic search over indexed filing text. Optional filters narrow to a
    company or a single filing. Returns chunks with provenance + distance."""
    with _lock:
        db = _connect()
        if TABLE not in db.table_names():
            return {"query": query, "hits": [],
                    "note": "nothing indexed yet — run index_filing first"}
        tbl = db.open_table(TABLE)
    qv = _embed([query])[0]
    q = tbl.search(qv).metric("cosine")
    clauses = []
    if company:
        clauses.append(f"company = '{company}'")
    if accession:
        clauses.append(f"accession = '{accession.strip()}'")
    if clauses:
        q = q.where(" AND ".join(clauses), prefilter=True)
    rows = q.limit(k).to_list()
    hits = [
        {"accession": r.get("accession"), "company": r.get("company"),
         "form": r.get("form"), "section": r.get("section"),
         "score": round(1 - r["_distance"], 4) if "_distance" in r else None,
         "text": r.get("text")}
        for r in rows
    ]
    return {"query": query, "count": len(hits), "hits": hits}


def status() -> dict:
    with _lock:
        db = _connect()
        if TABLE not in db.table_names():
            return {"lance_dir": str(LANCE_DIR), "indexed_chunks": 0,
                    "filings": []}
        tbl = db.open_table(TABLE)
        rows = tbl.to_pandas()[["accession", "company", "form"]]
    by = (rows.groupby(["accession", "company", "form"]).size()
          .reset_index(name="chunks"))
    return {"lance_dir": str(LANCE_DIR), "indexed_chunks": int(len(rows)),
            "filings": by.to_dict(orient="records")}
