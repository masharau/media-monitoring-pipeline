# -*- coding: utf-8 -*-
import sys, os, re, time, json, sqlite3, urllib.request, urllib.parse, html, hashlib, unicodedata
from datetime import datetime, date
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# --- 1. ИНИЦИАЛИЗАЦИЯ И ПАРАМЕТРЫ СБОРА ---
BASE_DIR = Path(__file__).resolve().parent
TARGET_YEAR = int(os.environ.get("TARGET_YEAR", 2026))
TARGET_MONTH = int(os.environ.get("TARGET_MONTH", 9))
TARGET_START = date(TARGET_YEAR, TARGET_MONTH, 1)

if TARGET_MONTH == 12:
    TARGET_END_EXCLUSIVE = date(TARGET_YEAR + 1, 1, 1)
else:
    TARGET_END_EXCLUSIVE = date(TARGET_YEAR, TARGET_MONTH + 1, 1)

SQLITE_DB = os.environ.get("SQLITE_DB", str(BASE_DIR / f"pipeline_state_{TARGET_YEAR}.db"))
QUOTA = int(os.environ.get("TARGET_QUOTA_PER_DOMAIN", 0)) # 0 = ПОЛНЫЙ СБОР БЕЗ ОГРАНИЧЕНИЙ

# API Ключи
OR_KEY = "YOUR_OPENROUTER_API_KEY"
OR_MODEL = "recursive/flint-v1.0:free"
GEMINI_KEY = "YOUR_GEMINI_API_KEY"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BELTA_NAME = "БелТА"
BELTA_SELECTORS = ["div.content_margin", "div.news_text", "div.text_block", "article"]

# Загрузка и нормализация маркеров из sources.json
sources_path = BASE_DIR / "sources.json"
if not sources_path.exists():
    sys.exit("[✗] sources.json не найден!")

with open(sources_path, "r", encoding="utf-8") as s_f:
    SRC = json.load(s_f)

def normalize_text(t):
    if not t: return ""
    norm = unicodedata.normalize("NFKC", t).replace("\u00a0", " ").replace("ё", "е").replace("ў", "у")
    norm = re.sub(r"\s+", " ", norm).strip().lower()
    return norm

RAW_MARKERS = SRC.get("markers", [])
MARKERS = list(set([normalize_text(m) for m in RAW_MARKERS if len(m.strip()) > 3]))

PRIMARY_CALENDAR_MARKERS = [
    "17 сентября", "17 верасня",
    "день народного единства", "дзень народнага адзінства",
    "1939"
]

def log(msg, lvl="INFO"):
    t = time.strftime("%H:%M:%S")
    pfx = {"INFO": "[*]", "OK": "[✓]", "WARN": "[!]", "FAIL": "[✗]", "STEP": " ├──", "WAIT": "[⏳]"}.get(lvl, "[*]")
    print(f"[{t}] {pfx} [БелТА] {msg}", flush=True)

# --- 2. БАЗА ДАННЫХ И STATE TRACKING ---
def init_db():
    conn = sqlite3.connect(SQLITE_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            canonical_url TEXT PRIMARY KEY,
            recovery_snapshot_url TEXT,
            domain TEXT NOT NULL,
            year INTEGER NOT NULL,
            date_published TEXT,
            title TEXT,
            author TEXT,
            full_text TEXT,
            found_markers TEXT,
            context_analysis TEXT,
            analysis_source TEXT,
            quote TEXT,
            speaker TEXT,
            pdf_path TEXT,
            recovery_source TEXT,
            status TEXT NOT NULL,
            http_code INTEGER,
            fetch_date TEXT,
            error_message TEXT
        )
    """)
    conn.commit()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(articles)").fetchall()]
    if "recovery_snapshot_url" not in cols: cur.execute("ALTER TABLE articles ADD COLUMN recovery_snapshot_url TEXT")
    if "analysis_source" not in cols: cur.execute("ALTER TABLE articles ADD COLUMN analysis_source TEXT")
    if "http_code" not in cols: cur.execute("ALTER TABLE articles ADD COLUMN http_code INTEGER")
    conn.commit()
    conn.close()

def save_article(d):
    conn = sqlite3.connect(SQLITE_DB)
    cur = conn.cursor()
    cols = [
        "canonical_url", "recovery_snapshot_url", "domain", "year", "date_published",
        "title", "author", "full_text", "found_markers", "context_analysis",
        "analysis_source", "quote", "speaker", "pdf_path", "recovery_source",
        "status", "http_code", "fetch_date", "error_message"
    ]
    place = ", ".join(["?"] * len(cols))
    
    # Защищенный UPDATE: статус COMPLETED никогда не затирается ошибками; COALESCE защищает данные (Дефект A7)
    upd_parts = [
        "status = CASE WHEN articles.status = 'COMPLETED' THEN 'COMPLETED' ELSE excluded.status END",
        "pdf_path = COALESCE(articles.pdf_path, excluded.pdf_path)",
        "full_text = COALESCE(articles.full_text, excluded.full_text)",
        "context_analysis = COALESCE(articles.context_analysis, excluded.context_analysis)",
        "quote = COALESCE(articles.quote, excluded.quote)",
        "speaker = COALESCE(articles.speaker, excluded.speaker)",
        "author = CASE WHEN excluded.author != 'UNKNOWN' AND excluded.author IS NOT NULL THEN excluded.author ELSE articles.author END",
        "http_code = COALESCE(excluded.http_code, articles.http_code)",
        "fetch_date = excluded.fetch_date",
        "recovery_source = COALESCE(excluded.recovery_source, articles.recovery_source)",
        "recovery_snapshot_url = COALESCE(excluded.recovery_snapshot_url, articles.recovery_snapshot_url)"
    ]
    cur.execute(
        f"INSERT INTO articles ({', '.join(cols)}) VALUES ({place}) ON CONFLICT(canonical_url) DO UPDATE SET {', '.join(upd_parts)}",
        [d.get(c) for c in cols]
    )
    conn.commit()
    conn.close()

def is_in_target_period(date_str):
    if not date_str or date_str == "UNKNOWN": return False
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return TARGET_START <= dt < TARGET_END_EXCLUSIVE
    except Exception: return False

# --- 3. ИЗВЛЕЧЕНИЕ ДАТЫ И ТЕКСТА BS4 (ДЕФЕКТЫ A3, A14, A15 УСТРАНЕНЫ) ---
def parse_date_strict(date_raw):
    if not date_raw: return None
    ru = {
        "января":"01","февраля":"02","марта":"03","апреля":"04","мая":"05","июня":"06",
        "июля":"07","августа":"08","сентября":"09","октября":"10","ноября":"11","декабря":"12",
        "верасня":"09","верасень":"09"
    }
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", date_raw)
    if m: return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{1,2})\.(\d{2})\.(\d{4})", date_raw)
    if m: return f"{m.group(3)}-{m.group(2)}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})\s+([а-яА-ЯёЁa-zA-Z]+)\s+(\d{4})", date_raw)
    if m and ru.get(m.group(2).lower()):
        return f"{m.group(3)}-{ru.get(m.group(2).lower())}-{int(m.group(1)):02d}"
    return None

def clean_belta_html(raw_html):
    if not raw_html or len(raw_html) < 250:
        return "", "UNKNOWN", "UNKNOWN", "UNKNOWN"

    soup = BeautifulSoup(raw_html, "html.parser")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    if not title:
        og_t = soup.find("meta", property="og:title")
        title = og_t.get("content", "").strip() if og_t else "Без названия"

    # Дефект A3: автор извлекается из метаданных и сохраняется
    author = "UNKNOWN"
    for a_sel in [{"name": "author"}, {"name": "article:author"}, {"property": "article:author"}]:
        a_meta = soup.find("meta", attrs=a_sel)
        if a_meta and a_meta.get("content", "").strip():
            author = a_meta["content"].strip()
            break

    # Дефект C3: поддержка JSON-LD включая @graph
    date_pub = None
    for s_tag in soup.find_all("script", type="application/ld+json"):
        try:
            jd = json.loads(s_tag.string or "")
            items = jd.get("@graph", jd) if isinstance(jd, dict) else jd
            for item in (items if isinstance(items, list) else [items]):
                if item.get("datePublished"):
                    date_pub = parse_date_strict(item["datePublished"])
                    break
        except Exception: pass
        if date_pub: break

    if not date_pub:
        for m_name in ["article:published_time", "published_time"]:
            m_tag = soup.find("meta", property=m_name) or soup.find("meta", attrs={"name": m_name})
            if m_tag and m_tag.get("content"):
                date_pub = parse_date_strict(m_tag["content"])
                if date_pub: break

    if not date_pub:
        t_tag = soup.find("time")
        if t_tag:
            dt_attr = t_tag.get("datetime") or t_tag.get_text()
            m = re.search(r"\d{4}-\d{2}-\d{2}", dt_attr)
            if m: date_pub = m.group(0)

    if not date_pub: date_pub = "UNKNOWN"

    # Очистка мусора
    for unwanted in soup.find_all(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
        unwanted.decompose()

    garbage = re.compile(r"sidebar|widget|banner|adv|comments|social|read-also|tags|rc_item|main_news_inner|block_title|schema_org", re.I)
    for g_div in soup.find_all("div", class_=garbage):
        g_div.decompose()

    # Дефект A14: правильный поиск по тегам и классам
    ex_text = ""
    for sel in BELTA_SELECTORS:
        if sel == "article":
            box = soup.find("article")
        else:
            cls_name = sel.replace("div.", "").replace(".", "")
            box = soup.find(["div", "section"], class_=re.compile(cls_name))
        if box:
            txt = box.get_text(separator="\n\n", strip=True)
            if len(txt) > len(ex_text): ex_text = txt

    if len(ex_text) < 150:
        paras = []
        for p in soup.find_all("p"):
            p_text = p.get_text(strip=True)
            noise = ["читайте также", "фото:", "фото из архива", "все права защищены", "новости рубрики", "проект \"страна говорит\"", "общество", "политика", "культура", "регионы"]
            if len(p_text) > 35 and not any(k in p_text.lower() for k in noise):
                paras.append(p_text)
        ex_text = "\n\n".join(paras)

    return ex_text.strip(), title, date_pub, author

# --- НУЛЕВОЕ КОЛЬЦО: ПРОВЕРКА ЗАГОЛОВКА ---
def match_markers_in_title(title):
    norm_t = normalize_text(title)
    for pm in PRIMARY_CALENDAR_MARKERS:
        if pm in norm_t: return [pm]
    found = [m for m in MARKERS if m in norm_t]
    return found

# --- ПЕРВОЕ КОЛЬЦО: КОНТЕКСТНАЯ ВЕРИФИКАЦИЯ ИИ (OPENROUTER / GEMINI) ---
def analyze_context_llm(title, text, author="UNKNOWN"):
    url = "https://openrouter.ai/api/v1/chat/completions"
    user_payload = f"Заголовок: {title}\n\nТекст статьи:\n{text[:2600]}"
    
    system_inst = (
        "Ты эксперт медиа-исследователь темы 'День народного единства и события 17 сентября 1939 года в Беларуси'.\n"
        "Определи, относится ли статья по смыслу к теме 17 сентября / Дня народного единства / воссоединения Беларуси.\n\n"
        "Ответь СТРОГО в формате JSON:\n"
        "{\n"
        '  "is_relevant": true,\n'
        '  "category": "одобрение/поддержка" | "прямая цитата" | "критика/опровержение" | "нейтральное",\n'
        '  "speaker": "Имя спикера или Редакция",\n'
        '  "key_quote": "дословная ключевая цитата по теме",\n'
        '  "topics": ["тема1", "тема2"],\n'
        '  "summary": "Краткое аналитическое резюме (1 предложение)"\n'
        "}"
    )

    payload = {
        "model": OR_MODEL,
        "messages": [
            {"role": "system", "content": system_inst},
            {"role": "user", "content": user_payload}
        ]
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OR_KEY}",
                "HTTP-Referer": "http://localhost:3000",
                "X-Title": "MediaResearch"
            }
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            raw_ans = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()
            m = re.search(r"\{.*\}", raw_ans, re.S)
            if m:
                res = json.loads(m.group(0))
                is_rel = bool(res.get("is_relevant", False)) # Дефект A8: строго bool
                cat = str(res.get("category", "нейтральное"))
                sp = str(res.get("speaker") or author or "Редакция")
                qt = str(res.get("key_quote", "")).strip()
                topics = res.get("topics", ["День народного единства"])
                if isinstance(topics, str): topics = [topics]
                sm = str(res.get("summary", ""))
                time.sleep(1.0)
                return is_rel, f"Класс: {cat}. Темы: {', '.join(topics)}. Спикер: {sp}. {sm}", qt, sp, topics, "openrouter_llm"
    except Exception as e:
        log(f"Сбой OpenRouter ({e}) -> переключение на резервный Gemini API+...", "WARN")
        # Резервный вызов Gemini API+ (по системной инструкции)
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
        gemini_payload = {
            "contents": [{"parts": [{"text": f"{system_inst}\n\n{user_payload}"}]}],
            "generationConfig": {"responseMimeType": "application/json"}
        }
        try:
            req_g = urllib.request.Request(
                gemini_url,
                data=json.dumps(gemini_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req_g, timeout=10) as rg:
                res_g = json.loads(json.loads(rg.read().decode("utf-8"))["candidates"][0]["content"]["parts"][0]["text"])
                is_rel = bool(res_g.get("is_relevant", False))
                cat = str(res_g.get("category", "нейтральное"))
                sp = str(res_g.get("speaker") or author or "Редакция")
                qt = str(res_g.get("key_quote", "")).strip()
                topics = res_g.get("topics", ["День народного единства"])
                if isinstance(topics, str): topics = [topics]
                sm = str(res_g.get("summary", ""))
                time.sleep(3.0) # Защита от 429
                return is_rel, f"Класс: {cat}. Темы: {', '.join(topics)}. Спикер: {sp}. {sm}", qt, sp, topics, "gemini_api_fallback"
        except Exception as eg:
            log(f"Сбой резервного Gemini ({eg}) -> эвристический семантический fallback", "WARN")

    norm_txt = normalize_text(text)
    found = [m for m in MARKERS if m in norm_txt]
    if found:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        quote = next((s.strip() for s in sentences if any(m in normalize_text(s) for m in found[:2])), "")
        sp = author if author and author != "UNKNOWN" else "Редакция"
        return True, f"Класс: одобрение/поддержка. Маркеры: {', '.join(found[:2])}. Спикер: {sp}.", quote, sp, found[:2], "context_fallback"

    return False, "Не соответствует теме", "", "", [], "rejected"

# --- ЭТАП 4: ИЗОЛИРОВАННАЯ ГЕНЕРАЦИЯ PDF (ДЕФЕКТЫ B7, B8, B10, A18 УСТРАНЕНЫ) ---
def make_pdf_isolated(browser_context, title, dt, author, sp, analysis, quote, markers, text, orig_url, src):
    out_dir = BASE_DIR / "pdf_archive" / str(TARGET_YEAR) / "БелТА"
    out_dir.mkdir(parents=True, exist_ok=True)

    url_hash = hashlib.sha1(orig_url.encode("utf-8")).hexdigest()[:12]
    pdf_p = out_dir / f"{dt}_{url_hash}.pdf"
    pdf_tmp = out_dir / f"{dt}_{url_hash}.tmp"

    p_html = "".join([f"<p>{html.escape(p)}</p>" for p in text.split("\n\n")])
    markers_badge = ", ".join([html.escape(m) for m in markers])

    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>body{{font-family:DejaVu Sans,Arial,sans-serif;margin:30px;font-size:13px;line-height:1.5;color:#111;}}
    .box{{border:1px solid #ccc;background:#f9f9f9;padding:15px;margin-bottom:20px;border-radius:4px;}}
    .q{{border-left:4px solid #2563eb;background:#eff6ff;padding:10px 14px;margin:15px 0;font-style:italic;}}
    .badge{{background:#e2e8f0;padding:2px 8px;font-weight:bold;display:inline-block;margin-bottom:8px;}}</style></head><body>
    <div class="box"><span class="badge">БелТА | {TARGET_YEAR}</span>
    <h1>{html.escape(title)}</h1>
    <div><b>Дата публикации:</b> {html.escape(dt)} | <b>Автор:</b> {html.escape(author)} | <b>Спикер:</b> {html.escape(sp)}</div>
    <div><b>Канонический URL БелТА:</b> <a href="{html.escape(orig_url)}">{html.escape(orig_url)}</a></div>
    <div><b>Источник восстановления:</b> {html.escape(src)}</div>
    <div><b>Подтвержденные маркеры:</b> {markers_badge}</div>
    <div><b>Анализ:</b> {html.escape(analysis)}</div></div>
    {f'<div class="q"><b>Цитата:</b> \"{html.escape(quote)}\"</div>' if quote else ''}
    <div><h3>Полный текст статьи:</h3>{p_html}</div></body></html>"""

    # Изолированная вкладка
    pdf_page = browser_context.new_page()
    try:
        pdf_page.set_content(doc, wait_until="load")
        # Атомарная запись через tmp-файл (Дефект B7)
        pdf_page.pdf(path=str(pdf_tmp), format="A4", margin={"top":"12mm","bottom":"12mm","left":"12mm","right":"12mm"})
        pdf_tmp.replace(pdf_p)
    finally:
        pdf_page.close()

    return str(pdf_p)

# --- ЭТАП 2: УНИВЕРСАЛЬНЫЙ КАСКАД СЪЕМА (UNIFIED CASCADE С ПОЧИНКОЙ B3, B4, B5) ---
def universal_load_cascade(page, orig_url, dt_str=""):
    date_hint = dt_str.replace("-", "") if dt_str and dt_str != "UNKNOWN" else f"{TARGET_YEAR}{TARGET_MONTH:02d}17"

    # Уровень 1: Прямой Playwright HTTP/HTTPS (timeout=7000)
    direct_variants = [
        (orig_url, "DIRECT"),
        (orig_url.replace("https://", "http://").replace("www.", ""), "DIRECT_HTTP")
    ]
    for target, src in direct_variants:
        try:
            resp = page.goto(target, timeout=7000, wait_until="domcontentloaded")
            code = resp.status if resp else 0
            if code == 200:
                html_c = page.content()
                if len(html_c) > 1000 and "Раздел не найден" not in html_c and "404 Not Found" not in html_c:
                    curr_path = urllib.parse.urlsplit(page.url).path.rstrip("/")
                    if curr_path not in ["", "/"]:
                        return html_c, src, target, 200, None
        except Exception: pass

    # Уровень 2: Wayback Machine CDX с суффиксом id_ (Дефект B3, B4)
    wb_url = f"https://web.archive.org/web/{date_hint}id_/{orig_url}"
    try:
        resp = page.goto(wb_url, timeout=10000, wait_until="domcontentloaded")
        if resp and resp.status == 200:
            html_c = page.content()
            if len(html_c) > 1000 and "Scheduled Maintenance" not in html_c and "not archived" not in html_c.lower():
                return html_c, "Wayback Machine", page.url, 200, None
    except Exception: pass

    # Уровень 3: Arquivo.pt Replay
    ar_url = f"https://arquivo.pt/noFrame/replay/{date_hint}000000/{orig_url}"
    try:
        resp = page.goto(ar_url, timeout=10000, wait_until="domcontentloaded")
        if resp and resp.status == 200:
            html_c = page.content()
            if len(html_c) > 1000 and "not found" not in html_c.lower():
                return html_c, "Arquivo.pt", ar_url, 200, None
    except Exception: pass

    return "", "", None, 503, "Геоблок / Нет снимков в архивах"

# --- ЭТАП 1: КАСКАДНОЕ ОБНАРУЖЕНИЕ (ШАГИ 1.1 -> 1.4) ---
def unwrap_link(raw_link):
    if not raw_link or not isinstance(raw_link, str): return ""
    if raw_link.startswith("/") and not raw_link.startswith("//"): return ""
    if raw_link.startswith("//"): raw_link = "https:" + raw_link
    if "copilotsearch" in raw_link: return ""
    try:
        p = urllib.parse.urlsplit(raw_link)
        qs = urllib.parse.parse_qs(p.query)
        if "uddg" in qs: return qs["uddg"][0]
        if "url" in qs: return qs["url"][0]
        if "q" in qs and qs["q"][0].startswith("http"): return qs["q"][0]
    except Exception: pass
    return raw_link

def run_cascade_discovery(page, context):
    collected = []
    seen = set()
    log(f"СТАРТ ЭТАПА 1: КАСКАДНОЕ ОБНАРУЖЕНИЕ ({TARGET_YEAR}-{TARGET_MONTH:02d})...", "OK")

    # ШАГ 1.1: Прямой технический аудит БелТА (robots.txt, sitemap.xml, RSS)
    log("ШАГ 1.1: Прямой технический аудит БелТА (sitemap.xml, RSS)...", "STEP")
    month_prefix = f"{TARGET_YEAR}-{TARGET_MONTH:02d}"
    
    # Sitemap с устойчивым DNS-повтором (до 3 попыток)
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request("http://belta.by/sitemap.xml", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=7) as r:
                xml_s = r.read().decode("utf-8", errors="replace")
                for m in re.finditer(r"<loc>(https?://[^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", xml_s):
                    u = m.group(1)
                    lm = m.group(2) or ""
                    if lm and not (month_prefix in lm): continue
                    if "belta.by" in u and "/view/" in u and u not in seen:
                        seen.add(u)
                        collected.append({"url": u, "title": "", "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}-17", "text": "", "src": "SITEMAP"})
                break
        except Exception as e:
            if attempt == 3: log(f"Sitemap: {e}", "WARN")
            time.sleep(1.5 * attempt)

    # Дамп архива сентября
    try:
        req = urllib.request.Request("https://belta.by/dump_sitemap/sitemap_news_805000.xml", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as r:
            xml_d = r.read().decode("utf-8", errors="replace")
            for m in re.finditer(r"<loc>(https?://[^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", xml_d):
                u = m.group(1)
                lm = m.group(2) or ""
                if lm and not (month_prefix in lm): continue
                if "belta.by" in u and "/view/" in u and u not in seen:
                    seen.add(u)
                    collected.append({"url": u, "title": "", "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}-17", "text": "", "src": "DUMP_SITEMAP"})
    except Exception: pass

    log(f"Шаг 1.1 (Сайт) дал кандидатов: {len(collected)} шт.", "OK")

    # ШАГ 1.2: Поисковые системы (Яндекс и Google стелс)
    log("ШАГ 1.2: Поисковые системы (Яндекс, Google)...", "STEP")
    for se_q in [f'site:belta.by "17 сентября"', f'site:belta.by "День народного единства"']:
        # Яндекс
        try:
            ya_url = "https://ya.ru/search/?text=" + urllib.parse.quote(se_q)
            resp = page.goto(ya_url, timeout=10000, wait_until="domcontentloaded")
            time.sleep(1.5)
            ya_links = page.evaluate("""() => {
                const res = [];
                document.querySelectorAll('a').forEach(a => {
                    const du = a.getAttribute('data-url');
                    if (du && du.includes('belta.by') && du.includes('/view/')) res.push(du);
                    if (a.href && a.href.includes('belta.by') && a.href.includes('/view/')) res.push(a.href);
                });
                return Array.from(new Set(res));
            }""")
            for y_l in ya_links:
                if y_l not in seen:
                    seen.add(y_l)
                    collected.append({"url": y_l, "title": "", "date": f"{TARGET_YEAR}-{TARGET_MONTH:02d}-17", "text": "", "src": "YANDEX"})
        except Exception: pass

    # ШАГ 1.3: Социальный шлюз (@belta_telegramm)
    log("ШАГ 1.3: Социальный шлюз (@belta_telegramm с глубокой пагинацией)...", "STEP")
    for q in ["17 сентября", "День народного единства", "воссоединение", "1939"]:
        max_id = None
        for step in range(5):
            url_tg = f"https://t.me/s/belta_telegramm?q={urllib.parse.quote(q)}"
            if max_id: url_tg += f"&before={max_id}"
            try:
                req = urllib.request.Request(url_tg, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=8) as r:
                    soup = BeautifulSoup(r.read(), "html.parser")
                    msgs = soup.find_all("div", class_="tgme_widget_message")
                    if not msgs: break
                    for msg in msgs:
                        msg_id = msg.get("data-post", "").split("/")[-1]
                        if msg_id and msg_id.isdigit():
                            max_id = min(int(msg_id), max_id) if max_id else int(msg_id)

                        t_tag = msg.find("time")
                        dt_val = t_tag.get("datetime")[:10] if t_tag and t_tag.get("datetime") else None
                        if not dt_val or not is_in_target_period(dt_val): continue

                        t_div = msg.find("div", class_="tgme_widget_message_text")
                        if not t_div: continue
                        full_txt = t_div.get_text(separator="\n\n", strip=True)
                        if len(full_txt) < 150: continue

                        canon = None
                        for a in t_div.find_all("a", href=True):
                            if "belta.by" in a["href"]: canon = a["href"]; break
                        # Дефект A4 устранен: никаких фиктивных URL, если ссылки нет - пишем шлюзовой ключ
                        if not canon:
                            canon = f"telegram:belta_telegramm/{msg_id}"

                        lines = [line.strip() for line in full_txt.splitlines() if len(line.strip()) > 15]
                        title = lines[0] if lines else "Без названия"
                        title = re.sub(r'^[🇧🇾\s\W]+', '', title)[:140]

                        if canon not in seen:
                            seen.add(canon)
                            collected.append({"url": canon, "title": title, "date": dt_val, "text": full_txt, "src": "TELEGRAM_GATEWAY"})
            except Exception: break

    log(f"ИТОГО КАСКАДНЫМ ОБНАРУЖЕНИЕМ НАЙДЕНО КАНДИДАТОВ: {len(collected)} шт.", "OK")
    return collected

# --- ГЛАВНЫЙ ЦИКЛ КОНВЕЙЕРА ---
def run():
    init_db()
    log(f"СТАРТ СИСТЕМНОГО КОНВЕЙЕРА БЕЛТА (СЕНТЯБРЬ {TARGET_YEAR})", "OK")
    
    # State Tracking: режим работы
    conn = sqlite3.connect(SQLITE_DB)
    already_done = set([r[0] for r in conn.execute("SELECT canonical_url FROM articles WHERE status IN ('COMPLETED', 'NO_MATCH_VERIFIED')").fetchall()])
    conn.close()
    
    mode = "INCREMENTAL" if already_done else "FULL RUN"
    log(f"РЕЖИМ STATE TRACKING: {mode} (уже обработано в БД: {len(already_done)} статей)", "INFO")

    total_completed = 0
    # Этап 2: Context Hijacking (постоянный профиль браузера для обхода капчи)
    profile_dir = BASE_DIR / "browser_profile"
    
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars"
            ]
        )
        page = context.new_page()

        # Этап 1: Каскадный поиск
        raw_candidates = run_cascade_discovery(page, context)
        
        # Фильтрация по State Tracking
        candidates = [c for c in raw_candidates if c["url"] not in already_done]
        skipped = len(raw_candidates) - len(candidates)
        log(f"Пропущено по State Tracking: {skipped} шт. На обработку: {len(candidates)} шт.", "INFO")

        for idx, art in enumerate(candidates, start=1):
            if QUOTA > 0 and total_completed >= QUOTA:
                log(f"Квота ({QUOTA} ст.) выполнена!", "OK")
                break

            title = art["title"]
            dt = art["date"]
            text = art.get("text", "")
            url = art["url"]
            disc_src = art.get("src", "UNKNOWN")

            log(f"[{idx}/{len(candidates)}] {title[:65] if title else url[:65]}...")

            # Этап 2: Механический парсинг (если текста еще нет)
            src_used = disc_src
            snap_url = None
            if not text or len(text) < 150:
                html_raw, src_used, snap_url, http_code, load_err = universal_load_cascade(page, url, dt)
                if not html_raw:
                    save_article({
                        "canonical_url": url, "recovery_snapshot_url": snap_url,
                        "domain": BELTA_NAME, "year": TARGET_YEAR, "date_published": None,
                        "title": title or "UNKNOWN", "author": "UNKNOWN", "full_text": None,
                        "found_markers": "[]", "context_analysis": None,
                        "analysis_source": None, "quote": None, "speaker": None,
                        "pdf_path": None, "recovery_source": src_used or "FAILED",
                        "status": "UNAVAILABLE", "http_code": http_code,
                        "fetch_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "error_message": load_err or "Недоступен"
                    })
                    log(f"Пропуск: недоступен (HTTP {http_code})", "FAIL")
                    continue

                text, clean_t, dt_extracted, author = clean_belta_html(html_raw)
                if clean_t and clean_t != "Без названия": title = clean_t
                if dt_extracted and dt_extracted != "UNKNOWN": dt = dt_extracted
            else:
                author = "UNKNOWN"

            # Валидация даты (Дефект A5 ликвидирован: текст сохраняется с флагом)
            if dt == "UNKNOWN" or not is_in_target_period(dt):
                save_article({
                    "canonical_url": url, "recovery_snapshot_url": snap_url,
                    "domain": BELTA_NAME, "year": TARGET_YEAR, "date_published": dt,
                    "title": title, "author": author, "full_text": text,
                    "found_markers": "[]", "context_analysis": None,
                    "analysis_source": None, "quote": None, "speaker": None,
                    "pdf_path": None, "recovery_source": src_used,
                    "status": "DATE_UNVERIFIED", "http_code": 200,
                    "fetch_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "error_message": f"Дата '{dt}' вне сентября {TARGET_YEAR}"
                })
                log(f"Пропуск: дата '{dt}' вне сентября {TARGET_YEAR}", "WARN")
                continue

            # ЭТАП 3: РАЗДЕЛЕННАЯ ЛОГИКА ВЕРИФИКАЦИИ
            # Нулевое кольцо: проверка чистого заголовка
            title_markers = match_markers_in_title(title)
            
            if title_markers:
                log(f"Нулевое кольцо: маркер в заголовке -> ПРЯМАЯ ЗАПИСЬ: {title_markers[:2]}", "OK")
                quote = text.split("\n\n")[0]
                sp = "Редакция"
                analysis = f"Класс: одобрение/поддержка. Маркер в заголовке: '{title_markers[0]}'. Спикер: Редакция."
                topics = title_markers
                an_src = "title_marker_direct"
            else:
                # Первое кольцо: проверка наличия тематических корней в тексте статьи
                norm_text_low = normalize_text(text)
                thematic_roots = [
                    "единств", "17 сентября", "17 верасня", "1939", "воссоединен", "уз'яднан",
                    "западн", "заходн", "рижск", "рыжск", "оккупац", "акупац", "патриот", "патрыёт",
                    "историческ", "гістарычн", "памят", "памяц", "освободительн", "вызвольн", "береза-картузск"
                ]
                has_candidate_roots = any(r in norm_text_low for r in thematic_roots)

                if not has_candidate_roots:
                    # Мгновенный отсев бытового шума (спорт, ДТП, агро) без вызова LLM
                    save_article({
                        "canonical_url": url, "recovery_snapshot_url": snap_url,
                        "domain": BELTA_NAME, "year": TARGET_YEAR, "date_published": dt,
                        "title": title, "author": author, "full_text": text,
                        "found_markers": "[]", "context_analysis": None,
                        "analysis_source": "fast_filter_rejected", "quote": None, "speaker": None,
                        "pdf_path": None, "recovery_source": src_used,
                        "status": "NO_MATCH_VERIFIED", "http_code": 200,
                        "fetch_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "error_message": "Тематические корни темы отсутствуют в тексте"
                    })
                    log("Пропуск: вне темы (мгновенный отсев бытовых новостей)", "STEP")
                    continue

                # Если тематические зацепки есть -> передаем в ИИ для глубокого семантического анализа
                log("Первое кольцо: тематические маркеры есть в тексте -> семантический анализ ИИ...", "STEP")
                is_rel, analysis, quote, sp, topics, an_src = analyze_context_llm(title, text, author)
                if not is_rel:
                    save_article({
                        "canonical_url": url, "recovery_snapshot_url": snap_url,
                        "domain": BELTA_NAME, "year": TARGET_YEAR, "date_published": dt,
                        "title": title, "author": author, "full_text": text,
                        "found_markers": "[]", "context_analysis": None,
                        "analysis_source": None, "quote": None, "speaker": None,
                        "pdf_path": None, "recovery_source": src_used,
                        "status": "NO_MATCH_VERIFIED", "http_code": 200,
                        "fetch_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "error_message": "ИИ отклонил материал (REJECTED)"
                    })
                    log("Пропуск: отклонено ИИ (REJECTED)", "WARN")
                    continue
                log(f"ИИ подтвердил релевантность [{an_src}]: {topics}", "OK")

            # ЭТАП 4: АРХИВАЦИЯ И ЭКСПОРТ (Изолированная генерация PDF)
            pdf_p = make_pdf_isolated(context, title, dt, author, sp, analysis, quote, topics, text, url, src_used)

            save_article({
                "canonical_url": url, "recovery_snapshot_url": snap_url,
                "domain": BELTA_NAME, "year": TARGET_YEAR, "date_published": dt,
                "title": title, "author": author, "full_text": text,
                "found_markers": json.dumps(topics, ensure_ascii=False),
                "context_analysis": analysis, "analysis_source": an_src,
                "quote": quote, "speaker": sp, "pdf_path": pdf_p,
                "recovery_source": src_used, "status": "COMPLETED",
                "http_code": 200, "fetch_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "error_message": None
            })
            total_completed += 1
            log(f"COMPLETED #{total_completed}: {title[:40]}... -> {pdf_p}", "OK")

        context.close()

    # Финальный экспорт в CSV
    conn = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT domain, date_published, author, title, found_markers,
               context_analysis, quote, canonical_url, pdf_path
        FROM articles WHERE status='COMPLETED' AND year=?
        ORDER BY date_published ASC, canonical_url ASC
    """, (TARGET_YEAR,)).fetchall()
    conn.close()

    csv_file = BASE_DIR / f"corpus_{TARGET_YEAR}.csv"
    with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
        f.write("domain,date_published,author,title,found_markers,context_analysis,quote,canonical_url,pdf_path\n")
        for r in rows:
            f.write(",".join(['"' + str(x).replace('"', '""') + '"' for x in r]) + "\n")

    print("=" * 80)
    log(f"СБОР ЗАВЕРШЕН! Успешно сохранено COMPLETED: {total_completed} | Всего в базе: {len(rows)} | CSV: {csv_file}", "OK")
    print("=" * 80)

if __name__ == "__main__":
    run()
