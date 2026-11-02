# -*- coding: utf-8 -*-
import sqlite3
import json
import csv
from pipeline_config import SQLITE_DB, CSV_HEADERS, TARGET_YEAR

class Status:
    DISCOVERED = "DISCOVERED"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    NO_MATCH_VERIFIED = "NO_MATCH_VERIFIED"
    DATE_UNVERIFIED = "DATE_UNVERIFIED"
    PDF_FAILED = "PDF_FAILED"
    COMPLETED = "COMPLETED"

def init_storage(db_path: str = SQLITE_DB):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            canonical_url TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            year INTEGER NOT NULL,
            date_published TEXT,
            title TEXT,
            author TEXT,
            full_text TEXT,
            found_markers TEXT,
            context_analysis TEXT,
            quote TEXT,
            speaker TEXT,
            pdf_path TEXT,
            recovery_source TEXT,
            recovery_snapshot_url TEXT,
            recovery_timestamp TEXT,
            status TEXT NOT NULL,
            fetch_date TEXT,
            error_message TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_art_status ON articles (status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_art_year ON articles (year)")
    conn.commit()
    conn.close()

def db_update_article_state(data: dict, db_path: str = SQLITE_DB):
    url = data.get("canonical_url")
    if not url: return

    year = data.get("year", TARGET_YEAR)
    markers = data.get("found_markers")
    if isinstance(markers, list):
        markers = json.dumps(markers, ensure_ascii=False)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT status, year FROM articles WHERE canonical_url = ?", (url,))
    existing = cur.fetchone()

    if existing and existing[1] != year and not data.get("force_status"):
        conn.close()
        return

    fields = [
        "domain", "year", "date_published", "title", "author", "full_text",
        "found_markers", "context_analysis", "quote", "speaker", "pdf_path",
        "recovery_source", "recovery_snapshot_url", "recovery_timestamp",
        "status", "fetch_date", "error_message"
    ]

    if existing:
        update_cols, params = [], []
        for f in fields:
            val = markers if f == "found_markers" else data.get(f)
            if val is not None or data.get("force_status"):
                update_cols.append(f"{f} = ?")
                params.append(val)
        params.append(url)
        cur.execute(f"UPDATE articles SET {', '.join(update_cols)} WHERE canonical_url = ?", params)
    else:
        all_cols = ["canonical_url"] + fields
        vals = [url] + [markers if f == "found_markers" else data.get(f) for f in fields]
        placeholders = ", ".join(["?"] * len(all_cols))
        cur.execute(f"INSERT INTO articles ({', '.join(all_cols)}) VALUES ({placeholders})", vals)

    conn.commit()
    conn.close()

def export_db_to_csv(db_path: str = SQLITE_DB, output_csv: str = None):
    if not output_csv:
        output_csv = f"corpus_{TARGET_YEAR}.csv"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("""
        SELECT domain, date_published, author, title, found_markers,
               context_analysis, quote, canonical_url, pdf_path
        FROM articles
        WHERE status = 'COMPLETED' AND year = ?
        ORDER BY date_published ASC
    """, (TARGET_YEAR,))
    rows = cur.fetchall()
    conn.close()

    def fmt_m(m):
        try:
            arr = json.loads(m)
            return ", ".join(arr) if isinstance(arr, list) else (m or "")
        except: return m or ""

    with open(output_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADERS)
        for r in rows:
            w.writerow([r[0], r[1], r[2], r[3], fmt_m(r[4]), r[5], r[6], r[7], r[8]])
    return output_csv
