# -*- coding: utf-8 -*-
import re
import html
import unicodedata
import urllib.parse
from datetime import datetime
from pipeline_config import TARGET_YEAR, TARGET_START_DATE, TARGET_END_EXCLUSIVE, CONFIG

def validate_and_normalize_url(url: str, allowed_domain: str):
    if not url or not isinstance(url, str): return None
    try:
        p = urllib.parse.urlsplit(url.strip())
        if p.scheme.lower() not in {"http", "https"}: return None
        netloc = p.netloc.lower().split(":")[0]
        if netloc.startswith("www."): netloc = netloc[4:]
        tgt = allowed_domain.lower()
        if tgt.startswith("www."): tgt = tgt[4:]
        if netloc != tgt and not netloc.endswith("." + tgt): return None
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "ysclid", "_openstat", "from"}]
        path = re.sub(r'%([0-9a-fA-F]{2})', lambda m: '%' + m.group(1).upper(), p.path or "/")
        if path != "/" and path.endswith("/"): path = path.rstrip("/")
        return urllib.parse.urlunsplit(("https", netloc, path, urllib.parse.urlencode(q), ""))
    except Exception: return None

def parse_date_string(date_raw: str):
    if not date_raw: return None
    ru = {"января":"01","февраля":"02","марта":"03","апреля":"04","мая":"05","июня":"06",
          "июля":"07","августа":"08","сентября":"09","октября":"10","ноября":"11","декабря":"12",
          "верасня":"09","верасень":"09"}
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", date_raw)
    if m: return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{1,2})\.(\d{2})\.(\d{4})", date_raw)
    if m: return f"{m.group(3)}-{m.group(2)}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})\s+([а-яА-ЯёЁa-zA-Z]+)\s+(\d{4})", date_raw)
    if m and ru.get(m.group(2).lower()):
        return f"{m.group(3)}-{ru.get(m.group(2).lower())}-{int(m.group(1)):02d}"
    return None

def is_date_in_target_period(date_str: str, target_year: int = TARGET_YEAR) -> bool:
    if not date_str: return False
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return TARGET_START_DATE <= dt < TARGET_END_EXCLUSIVE
    except Exception: return False

def normalize_text_for_search(text: str) -> str:
    if not text: return ""
    norm = unicodedata.normalize("NFKC", text).replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    norm = norm.replace("ё", "е").replace("Ё", "Е").replace("ў", "у").replace("Ў", "У")
    norm = re.sub(r"[\x27\u2019\u02bc\u0060\u2018]", "'", norm)
    return re.sub(r"\s+", " ", norm).strip().lower()

def detect_markers_in_article(text: str) -> list:
    norm_source = normalize_text_for_search(text)
    return [m for m in CONFIG.get("markers", []) if normalize_text_for_search(m) in norm_source]

def extract_article_content(html_content: str, current_url: str, domain: str):
    res = {"title": "UNKNOWN", "date_published": None, "author": "UNKNOWN",
           "canonical_url": current_url, "full_text": "", "is_stub": False, "stub_reason": ""}
    if not html_content or len(html_content.strip()) < 150:
        res["is_stub"] = True; res["stub_reason"] = "HTML < 150 байт"; return res

    for ind in CONFIG.get("stub_indicators", []):
        if ind in html_content.lower():
            res["is_stub"] = True; res["stub_reason"] = f"Заглушка: {ind}"; return res

    # Заголовок
    og_t = re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', html_content, re.I)
    if og_t: res["title"] = html.unescape(og_t.group(1)).strip()
    else:
        h1 = re.search(r'<h1[^>]*>(.*?)</h1>', html_content, re.I | re.S)
        if h1: res["title"] = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', h1.group(1)))).strip()

    # Дата
    for jb in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_content, re.I | re.S):
        try:
            jd = json.loads(jb.strip())
            for item in (jd if isinstance(jd, list) else [jd]):
                if item.get("datePublished"):
                    res["date_published"] = parse_date_string(item.get("datePublished")); break
        except: pass
        if res["date_published"]: break

    if not res["date_published"]:
        m = re.search(r'<meta[^>]*property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)["\']', html_content, re.I)
        if m: res["date_published"] = parse_date_string(m.group(1))
    if not res["date_published"]:
        t = re.search(r'<time[^>]*datetime=["\']([^"\']+)["\']', html_content, re.I)
        if t: res["date_published"] = parse_date_string(t.group(1))
    if not res["date_published"]:
        td = re.search(rf'(\d{{1,2}}\.09\.{TARGET_YEAR}|\d{{1,2}}\s+(?:сентября|верасня)\s+{TARGET_YEAR})', html_content, re.I)
        if td: res["date_published"] = parse_date_string(td.group(1))

    # Автор (au.group)
    au = re.search(r'<meta[^>]*name=["\']author["\'][^>]*content=["\']([^"\']+)["\']', html_content, re.I)
    if au: res["author"] = html.unescape(au.group(1)).strip()

    # Очистка мусора (виджеты, баннеры, соцсети)
    clean = re.sub(r'<(script|style|nav|footer|header|aside|form|iframe)[^>]*>.*?</\1>', '', html_content, flags=re.I | re.S)
    clean = re.sub(r'<div[^>]*class=["\'][^"\']*(sidebar|related|popular|widget|banner|teaser|adv|comments|social|read-also|tags)[^"\']*["\'][^>]*>.*?</div>', '', clean, flags=re.I | re.S)

    paras = re.findall(r'<p[^>]*>(.*?)</p>', clean, re.I | re.S)
    clean_paras = []
    for p in paras:
        txt = re.sub(r'<[^>]+>', ' ', p)
        txt = html.unescape(txt).strip()
        txt = re.sub(r'\s+', ' ', txt)
        if len(txt) > 25:
            clean_paras.append(txt)

    res["full_text"] = "\n\n".join(clean_paras)
    if len(res["full_text"]) < 120:
        res["is_stub"] = True; res["stub_reason"] = "Текст < 120 знаков"
    return res
