# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(line_buffering=True)
import os
import re
import time
import json
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, Route
from pipeline_config import *
from pipeline_storage import *
from pipeline_analysis import *
from pipeline_pdf import *
from pipeline_llm import analyze_context_with_gemini

def log_event(media: str, msg: str, level: str = "INFO"):
    t = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "[*]", "OK": "[✓]", "WARN": "[!]", "FAIL": "[✗]", "STEP": " ├──", "END": " └──", "CB": "[CB]"}.get(level, "[*]")
    print(f"[{t}] {prefix} [{media}] {msg}", flush=True)

def get_utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def extract_candidates_for_domain(page, domain: str, year: int) -> list:
    urls = []
    
    # 1. БелТА — целевой сентябрьский дамп 805000
    if "belta.by" in domain:
        log_event(domain, "Загрузка целевого дампа 805000 (09.2026)...", "INFO")
        try:
            req = urllib.request.Request("https://belta.by/dump_sitemap/sitemap_news_805000.xml", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as r:
                xml_text = r.read().decode("utf-8", errors="replace")
                for m in re.finditer(r"<loc>(https?://[^<]+)</loc>\s*<lastmod>(2026-09-[^<]+)</lastmod>", xml_text):
                    urls.append(m.group(1))
            log_event(domain, f"Извлечено из дампа: {len(urls)} статей за сентябрь 2026", "OK")
            return urls
        except Exception as e:
            log_event(domain, f"Сбой дампа: {e}", "WARN")

    # 2. Прямые государственные порталы
    if any(k in domain for k in ["president.gov.by", "pravo.by", "edu.gov.by"]):
        try:
            req = urllib.request.Request(f"https://{domain}/sitemap.xml", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=12) as r:
                xml_text = r.read().decode("utf-8", errors="replace")
                for u in re.findall(r"<loc>(https?://[^<]+)</loc>", xml_text):
                    if "news" in u or "novosti" in u:
                        urls.append(u)
            log_event(domain, f"Извлечено из sitemap: {len(urls)} ссылок", "OK")
            return urls
        except Exception as e:
            log_event(domain, f"Ошибка sitemap: {e}", "WARN")

    # 3. Геоблокированные СМИ (СТВ, ОНТ, Минск-Новости, ТВР) -> Telegram-поиск
    tg_channels = {
        "ont.by": "ontnews",
        "ctv.by": "ctvby",
        "minsknews.by": "minsknews_by",
        "tvr.by": "tvrby"
    }
    ch = tg_channels.get(domain)
    if ch:
        log_event(domain, f"Поиск в Telegram @{ch} по маркеру '17 сентября'...", "INFO")
        try:
            t_url = f"https://t.me/s/{ch}?q=" + urllib.parse.quote("17 сентября")
            resp = page.goto(t_url, timeout=15000, wait_until="domcontentloaded")
            time.sleep(1.5)
            # Извлекаем ссылки на сайт из постов Telegram
            links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            for l in links:
                norm = validate_and_normalize_url(l, domain)
                if norm and norm not in urls:
                    urls.append(norm)
            log_event(domain, f"Найдено через Telegram: {len(urls)} целевых URL", "OK")
        except Exception as e:
            log_event(domain, f"Telegram поиск: {e}", "WARN")

    return urls

def process_article(page, url: str, domain: str, media_name: str, idx: int, total: int, year: int) -> bool:
    now_utc = get_utc_now_str()
    log_event(media_name, f"[{idx}/{total}] {url[:75]}", "INFO")

    html_content = ""
    rec_src = "DIRECT_LIVE"

    # Шаг 1: Загрузка через Playwright (снятие заглушек)
    try:
        resp = page.goto(url, timeout=12000, wait_until="domcontentloaded")
        if resp and resp.status == 200:
            html_content = page.content()
    except Exception as e:
        log_event(media_name, f"Прямой доступ сбой: {e}", "STEP")

    # Шаг 2: Архивный fallback (если прямой доступ упал)
    if not html_content or len(html_content) < 500:
        wb_url = f"https://web.archive.org/web/{year}0917/{url}"
        try:
            resp = page.goto(wb_url, timeout=18000, wait_until="domcontentloaded")
            if resp and resp.status == 200:
                html_content = page.content()
                rec_src = "Wayback Machine"
        except Exception: pass

    # Шаг 3: Извлечение контента и очистка от мусора
    meta = extract_article_content(html_content, url, domain)
    if not meta or meta.get("is_stub") or not meta.get("full_text"):
        db_update_article_state({
            "canonical_url": url, "domain": media_name, "year": year,
            "status": Status.RECOVERY_FAILED, "force_status": True,
            "fetch_date": now_utc, "error_message": meta.get("stub_reason") or "Текст не извлечен"
        })
        log_event(media_name, "Пропуск: текст не извлечен", "FAIL")
        return False

    # Шаг 4: Проверка даты
    date_pub = meta.get("date_published")
    title = meta.get("title") or "Без названия"

    if not date_pub or not is_date_in_target_period(date_pub, year):
        db_update_article_state({
            "canonical_url": url, "domain": media_name, "year": year,
            "date_published": date_pub, "title": title,
            "status": Status.NO_MATCH_VERIFIED, "force_status": True,
            "fetch_date": now_utc, "error_message": f"Дата {date_pub} вне периода"
        })
        log_event(media_name, f"Пропуск: дата {date_pub} вне сентября {year}", "END")
        return False

    # Шаг 5: Проверка наличия маркеров
    full_text = meta.get("full_text", "")
    detected = detect_markers_in_article(full_text)
    if not detected:
        db_update_article_state({
            "canonical_url": url, "domain": media_name, "year": year,
            "date_published": date_pub, "title": title,
            "status": Status.NO_MATCH_VERIFIED, "force_status": True,
            "fetch_date": now_utc, "error_message": "Маркеры не найдены"
        })
        log_event(media_name, f"Пропуск: маркеры не найдены ({date_pub})", "END")
        return False

    log_event(media_name, f"Маркеры найдены: {detected[:3]}", "STEP")

    # Шаг 6: Анализ через Gemini 2.5 Flash
    author = meta.get("author") or "UNKNOWN"
    analysis, quote, speaker, cat = analyze_context_with_gemini(full_text, detected, author)
    log_event(media_name, f"ИИ анализ: [{cat}] Спикер: {speaker}", "STEP")

    # Шаг 7: Генерация верифицированного PDF
    pdf_payload = {
        "domain": media_name, "title": title, "date": date_pub, "author": author,
        "url": url, "found_markers": detected, "context_analysis": analysis,
        "quote": quote, "speaker": speaker, "full_text": full_text,
        "recovery_source": rec_src
    }

    pdf_ok, verified_pdf = generate_verified_pdf(page, pdf_payload, year, domain)
    if not pdf_ok:
        db_update_article_state({
            "canonical_url": url, "domain": media_name, "year": year,
            "date_published": date_pub, "title": title,
            "status": Status.PDF_FAILED, "force_status": True,
            "fetch_date": now_utc, "error_message": "Ошибка PDF"
        })
        log_event(media_name, "Ошибка генерации PDF", "FAIL")
        return False

    # Шаг 8: Запись статуса COMPLETED в изолированную базу 2026 года
    db_update_article_state({
        "canonical_url": url, "domain": media_name, "year": year, "date_published": date_pub,
        "title": title, "author": author, "full_text": full_text,
        "found_markers": detected, "context_analysis": analysis, "quote": quote, "speaker": speaker,
        "pdf_path": verified_pdf, "recovery_source": rec_src,
        "status": Status.COMPLETED, "force_status": True, "fetch_date": now_utc
    })
    log_event(media_name, f"COMPLETED ({date_pub}): {title[:40]}... -> {verified_pdf}", "OK")
    return True

def run():
    init_storage()
    domains_cfg = CONFIG.get("domains", {})
    total_completed = 0

    print("=" * 80)
    print(f"[*] СТАРТ КОНВЕЙЕРА: ГОД {TARGET_YEAR} | БАЗА: {SQLITE_DB} | КВОТА: {TARGET_QUOTA_PER_DOMAIN}")
    print("=" * 80)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        # Блокировка тяжелых видео/шрифтов для ускорения
        def route_filter(route: Route):
            if route.request.resource_type in ["media", "font"]: route.abort()
            else: route.continue_()
        page.route("**/*", route_filter)

        for d_idx, (domain, d_info) in enumerate(domains_cfg.items(), start=1):
            media_name = d_info.get("name", domain)
            print(f"\n{'-'*80}")
            print(f"[{d_idx}/{len(domains_cfg)}] ОБРАБОТКА СМИ: {media_name} ({domain})")
            print(f"{'-'*80}")

            candidates = extract_candidates_for_domain(page, domain, TARGET_YEAR)
            if not candidates:
                log_event(media_name, "Кандидатов за сентябрь 2026 не найдено", "WARN")
                continue

            dom_success = 0
            for u_idx, art_url in enumerate(candidates, start=1):
                if TARGET_QUOTA_PER_DOMAIN and dom_success >= TARGET_QUOTA_PER_DOMAIN:
                    log_event(media_name, f"Достигнута квота {TARGET_QUOTA_PER_DOMAIN} статей.", "INFO")
                    break

                try:
                    if process_article(page, art_url, domain, media_name, u_idx, len(candidates), TARGET_YEAR):
                        dom_success += 1
                        total_completed += 1
                except Exception as e:
                    log_event(media_name, f"Сбой статьи {art_url}: {e}", "WARN")

                time.sleep(0.3)

        browser.close()

    csv_file = export_db_to_csv()
    print("\n" + "=" * 80)
    print(f"[✓] КОНВЕЙЕР УСПЕШНО ЗАВЕРШИЛ РАБОТУ.")
    print(f"[✓] Обработано COMPLETED: {total_completed} статей.")
    print(f"[✓] Итоговый CSV экспортирован в: {csv_file}")
    print("=" * 80)

if __name__ == "__main__":
    run()
