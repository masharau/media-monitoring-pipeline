
import gzip

def cc_fetch_html(filename: str, offset, length, timeout: int = 30) -> str:
    """Читает WARC-запись Common Crawl по HTTP Range с поддержкой распаковки gzip."""
    start, end = int(offset), int(offset) + int(length) - 1
    req = urllib.request.Request(
        f"https://data.commoncrawl.org/{filename}",
        headers={"Range": f"bytes={start}-{end}", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        decompressed = gzip.decompress(raw)
    except Exception:
        decompressed = raw

    parts = decompressed.split(b"\r\n\r\n", 2)
    if len(parts) < 3:
        raise ValueError("Неожиданный формат WARC: менее 3 частей")
    body = parts[2]
    if b"content-encoding: gzip" in parts[1].lower() or b"content-encoding:gzip" in parts[1].lower():
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    return body.decode("utf-8", "replace")

import re, time, json, urllib.parse, urllib.request, urllib.error, urllib.robotparser, xml.etree.ElementTree as ET
from pipeline_config import USER_AGENT, TARGET_YEAR, TARGET_START_DAY, TARGET_END_DAY, CONFIG
from pipeline_storage import db_log_audit
from pipeline_analysis import validate_and_normalize_url, is_listing_or_feed

class StrictRobotsAuditor:
    def __init__(self, domain: str, year: int = TARGET_YEAR):
        self.domain = domain; self.year = year
        self.rp = urllib.robotparser.RobotFileParser()
        self.allow_all = False; self.disallow_all = False
        self.load_rules()

    def load_rules(self):
        url = f"https://{self.domain}/robots.txt"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                self.rp.parse(r.read().decode("utf-8", errors="ignore").splitlines())
                db_log_audit("ROBOTS_TXT", "INSPECTION", self.year, self.domain, url, 1, str(r.status), "Parsed OK")
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500: self.allow_all = True
            else: self.disallow_all = True
        except: self.allow_all = True

    def is_allowed(self, url: str):
        if self.allow_all: return True
        if self.disallow_all: return False
        try: return self.rp.can_fetch(USER_AGENT, url)
        except: return False

# ==================== ФАЗА 1: КАРТЫ, РУБРИКИ, КАЛЕНДАРИ ====================
def discover_belta_calendar_and_sitemap(page, auditor: StrictRobotsAuditor, year: int):
    urls = []
    # Обход обеих архивных карт 2021 года по всему сентябрю (2021-09-)
    sitemap_files = [
        f"https://belta.by/dump_sitemap/sitemap_news_archive_{year}_440000.xml",
        f"https://belta.by/dump_sitemap/sitemap_news_archive_{year}_480000.xml",
        f"https://www.belta.by/sitemap_index.xml"
    ]
    for sm_url in sitemap_files:
        try:
            req = urllib.request.Request(sm_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=12) as r:
                root = ET.fromstring(r.read())
                for un in root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
                    loc = un.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}loc") or ""
                    mod = un.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod") or ""
                    if f"{year}-09-" in mod or f"-{year}/" in loc:
                        norm = validate_and_normalize_url(loc, "belta.by")
                        if norm and not is_listing_or_feed(norm):
                            urls.append(norm)
        except: pass
    return list(dict.fromkeys(urls))

def _old_belta_stub(page, auditor, year):
    urls = []
    # 1. Прямой обход календаря архива БелТА за каждый день 10-24 сентября 2021
    for day in range(TARGET_START_DAY, TARGET_END_DAY + 1):
        cal_url = f"https://www.belta.by/all-news/?date={year}-09-{day:02d}"
        try:
            resp = page.goto(cal_url, timeout=12000, wait_until="commit")
            time.sleep(0.5)
            links = page.eval_on_selector_all("a[href*='/view/']", "els => els.map(e => e.href)")
            for l in links:
                norm = validate_and_normalize_url(l, "belta.by")
                if norm and not is_listing_or_feed(norm) and auditor.is_allowed(norm):
                    urls.append(norm)
        except: pass

    # 2. Архивный sitemap
    try:
        req = urllib.request.Request("https://www.belta.by/sitemap_index.xml", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            root = ET.fromstring(r.read())
            for sm in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}sitemap"):
                loc = sm.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}loc") or ""
                if f"sitemap_news_archive_{year}" in loc:
                    s_req = urllib.request.Request(loc, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(s_req, timeout=12) as sr:
                        s_root = ET.fromstring(sr.read())
                        for un in s_root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
                            ul = un.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}loc") or ""
                            um = un.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod") or ""
                            if f"{year}-09-" in um:
                                try:
                                    d_val = int(um.split(f"{year}-09-")[1][:2])
                                    if TARGET_START_DAY <= d_val <= TARGET_END_DAY:
                                        norm = validate_and_normalize_url(ul, "belta.by")
                                        if norm and not is_listing_or_feed(norm) and auditor.is_allowed(norm):
                                            urls.append(norm)
                                except: pass
    except: pass
    return list(dict.fromkeys(urls))

def discover_general_sitemaps_and_feeds(auditor: StrictRobotsAuditor, domain: str, year: int):
    urls = []
    targets = [f"https://{domain}/sitemap.xml", f"https://{domain}/sitemap_index.xml", f"https://{domain}/rss", f"https://{domain}/feed", f"https://{domain}/ru.xml", f"https://{domain}/ru/documents.xml"]
    for ep in targets:
        if not auditor.is_allowed(ep): continue
        try:
            req = urllib.request.Request(ep, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as r:
                root = ET.fromstring(r.read())
                for node in root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
                    loc = node.text or ""
                    if str(year) in loc:
                        norm = validate_and_normalize_url(loc, domain)
                        if norm and not is_listing_or_feed(norm): urls.append(norm)
                for item in root.findall(".//item"):
                    l = item.findtext("link") or ""
                    norm = validate_and_normalize_url(l, domain)
                    if norm and not is_listing_or_feed(norm): urls.append(norm)
        except: pass
    return list(dict.fromkeys(urls))

# ==================== ФАЗА 2: 5 ПОИСКОВИКОВ ====================
def search_yandex(page, domain: str, query: str):
    urls = []
    try:
        page.goto(f"https://ya.ru/search/?text={urllib.parse.quote(f'site:{domain} \"{query}\"')}", timeout=10000, wait_until="commit")
        time.sleep(0.8)
        for l in page.eval_on_selector_all("a[href]", "el => el.map(e => e.href)"):
            n = validate_and_normalize_url(l, domain)
            if n and not is_listing_or_feed(n): urls.append(n)
    except: pass
    return urls

def search_google(page, domain: str, query: str):
    urls = []
    try:
        page.goto(f"https://www.google.com/search?q={urllib.parse.quote(f'site:{domain} \"{query}\"')}", timeout=10000, wait_until="commit")
        time.sleep(0.8)
        for l in page.eval_on_selector_all("a[href]", "el => el.map(e => e.href)"):
            n = validate_and_normalize_url(l, domain)
            if n and not is_listing_or_feed(n): urls.append(n)
    except: pass
    return urls

def search_mailru(page, domain: str, query: str):
    urls = []
    try:
        page.goto(f"https://go.mail.ru/search?q={urllib.parse.quote(f'site:{domain} \"{query}\"')}", timeout=10000, wait_until="commit")
        time.sleep(0.8)
        for l in page.eval_on_selector_all("a[href]", "el => el.map(e => e.href)"):
            n = validate_and_normalize_url(l, domain)
            if n and not is_listing_or_feed(n): urls.append(n)
    except: pass
    return urls

def search_rambler(page, domain: str, query: str):
    urls = []
    try:
        page.goto(f"https://nova.rambler.ru/search?query={urllib.parse.quote(f'site:{domain} \"{query}\"')}", timeout=10000, wait_until="commit")
        time.sleep(0.8)
        for l in page.eval_on_selector_all("a[href]", "el => el.map(e => e.href)"):
            n = validate_and_normalize_url(l, domain)
            if n and not is_listing_or_feed(n): urls.append(n)
    except: pass
    return urls

def search_duckduckgo(domain: str, query: str):
    urls = []
    try:
        req = urllib.request.Request(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(f'site:{domain} \"{query}\"')}", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode("utf-8", errors="ignore")
            for m in re.finditer(r'<a[^>]*class=["\']result__url["\'][^>]*href=["\']([^"\']+)["\']', body):
                raw = m.group(1)
                uddg = re.search(r'uddg=([^&]+)', raw)
                real = urllib.parse.unquote(uddg.group(1)) if uddg else raw
                n = validate_and_normalize_url(real, domain)
                if n and not is_listing_or_feed(n): urls.append(n)
    except: pass
    return urls

def search_all_5_engines_with_markers(page, domain: str, markers: list):
    found = []
    for m in markers:
        found.extend(search_yandex(page, domain, m))
        found.extend(search_google(page, domain, m))
        found.extend(search_mailru(page, domain, m))
        found.extend(search_rambler(page, domain, m))
        found.extend(search_duckduckgo(domain, m))
    return list(dict.fromkeys(found))

# ==================== ФАЗА 3: 3 ИНТЕРНЕТ-АРХИВА ====================
def query_archive_wayback(target_url: str):
    try:
        req = urllib.request.Request(f"https://archive.org/wayback/available?url={urllib.parse.quote(target_url)}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status == 200:
                d = json.loads(r.read().decode("utf-8")).get("archived_snapshots", {}).get("closest")
                if d and d.get("available") and d.get("status") == "200":
                    return {"source": "Wayback Machine", "snapshot_url": re.sub(r'/web/(\d+)/', r'/web/\1id_/', d.get("url")), "timestamp": d.get("timestamp", "")}
    except: pass
    return None

def query_archive_arquivo(target_url: str):
    try:
        req = urllib.request.Request(f"https://arquivo.pt/wayback/wayback/xmlsearch?url={urllib.parse.quote(target_url)}&maxItems=1", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status == 200:
                m = re.search(r'<link>([^<]+)</link>', r.read().decode("utf-8", errors="ignore"))
                if m: return {"source": "Arquivo.pt", "snapshot_url": m.group(1), "timestamp": ""}
    except: pass
    return None

def query_archive_commoncrawl(target_url: str):
    try:
        req = urllib.request.Request(f"https://index.commoncrawl.org/CC-MAIN-2021-43-index?url={urllib.parse.quote(target_url)}&output=json", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status == 200:
                line = r.readline().decode("utf-8")
                if line:
                    d = json.loads(line)
                    return {"source": "Common Crawl", "snapshot_url": f"https://data.commoncrawl.org/{d.get('filename')}", "timestamp": d.get("timestamp", "")}
    except: pass
    return None

def recover_article_three_archives(target_url: str, year: int, domain: str):
    res = []
    wb = query_archive_wayback(target_url)
    res.append("Wayback Machine: HIT" if wb else "Wayback Machine: MISS")
    best = wb
    ar = query_archive_arquivo(target_url)
    res.append("Arquivo.pt: HIT" if ar else "Arquivo.pt: MISS")
    if not best: best = ar
    cc = query_archive_commoncrawl(target_url)
    res.append("Common Crawl: HIT" if cc else "Common Crawl: MISS")
    if not best: best = cc
    for le in res:
        db_log_audit("WEB_ARCHIVE", le.split(":")[0], year, domain, target_url, 1 if "HIT" in le else 0, "200" if "HIT" in le else "404", le)
    return best, res

def discover_articles_from_cdx(domain: str, year: int):
    articles = []
    try:
        url = f"https://web.archive.org/cdx/search/cdx?url={domain}/*&from={year}09{TARGET_START_DAY}&to={year}09{TARGET_END_DAY}&output=json&limit=150&filter=statuscode:200&collapse=urlkey"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
            for row in data[1:]:
                norm = validate_and_normalize_url(row[2], domain)
                if norm and not is_listing_or_feed(norm) and not norm.endswith((".jpg", ".png", ".pdf", ".css", ".js")):
                    articles.append(norm)
    except: pass
    return list(dict.fromkeys(articles))
