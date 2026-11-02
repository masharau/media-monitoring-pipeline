# -*- coding: utf-8 -*-
import os
import json
from datetime import date

TARGET_YEAR = int(os.environ.get("TARGET_YEAR", 2026))
TARGET_MONTH = int(os.environ.get("TARGET_MONTH", 9))
TARGET_START_DATE = date(TARGET_YEAR, TARGET_MONTH, 1)
TARGET_END_EXCLUSIVE = date(TARGET_YEAR, TARGET_MONTH + 1 if TARGET_MONTH < 12 else 1, 1)

SQLITE_DB = os.environ.get("SQLITE_DB", f"pipeline_state_{TARGET_YEAR}.db")
TARGET_QUOTA_PER_DOMAIN = int(os.environ.get("TARGET_QUOTA_PER_DOMAIN", 0))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"

# Загружаем эталонные маркеры и селекторы из sources.json
with open("sources.json", "r", encoding="utf-8") as f:
    _SRC = json.load(f)

CONFIG = {
    "markers": _SRC.get("markers", []),
    "stub_indicators": _SRC.get("stub_indicators", []),
    "domains": _SRC.get("domains", {})
}

CSV_HEADERS = [
    "domain", "date_published", "author", "title",
    "found_markers", "context_analysis", "quote",
    "canonical_url", "pdf_path"
]
