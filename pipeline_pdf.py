# -*- coding: utf-8 -*-
import html
from pathlib import Path
from pipeline_config import TARGET_YEAR

def generate_verified_pdf(page, payload: dict, year: int = TARGET_YEAR, domain_raw: str = "") -> tuple:
    try:
        domain = payload.get("domain", "media")
        safe_domain = "".join([c if c.isalnum() or c in "-_" else "_" for c in domain_raw or domain])
        out_dir = Path("pdf_archive") / str(year) / safe_domain
        out_dir.mkdir(parents=True, exist_ok=True)

        url = payload.get("url", "")
        url_hash = abs(hash(url)) % 100000000
        date_str = payload.get("date", "no_date")
        target_path = out_dir / f"{date_str}_{url_hash}.pdf"

        title = html.escape(payload.get("title") or "Без названия")
        author = html.escape(payload.get("author") or "UNKNOWN")
        speaker = html.escape(payload.get("speaker") or "Редакция")
        context_analysis = html.escape(payload.get("context_analysis") or "")
        quote = html.escape(payload.get("quote") or "")
        rec_src = html.escape(payload.get("recovery_source") or "DIRECT_LIVE")
        
        full_text_paras = payload.get("full_text", "").split("\n\n")
        rendered_paras = "".join([f"<p>{html.escape(p)}</p>" for p in full_text_paras if p.strip()])
        markers_badge = ", ".join([html.escape(m) for m in payload.get("found_markers", [])])

        html_template = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{ font-family: DejaVu Sans, Arial, sans-serif; margin: 30px; color: #111; line-height: 1.5; font-size: 13px; }}
    .meta-box {{ border: 1px solid #ccc; background: #fafafa; padding: 14px; margin-bottom: 20px; border-radius: 4px; }}
    h1 {{ font-size: 19px; margin-top: 0; }}
    .badge {{ display: inline-block; background: #e2e8f0; padding: 2px 8px; border-radius: 3px; font-weight: bold; margin-bottom: 8px; }}
    .quote-box {{ border-left: 4px solid #2563eb; background: #eff6ff; padding: 10px 14px; margin: 15px 0; font-style: italic; }}
</style>
</head>
<body>
    <div class="meta-box">
        <span class="badge">{domain} | {year}</span>
        <h1>{title}</h1>
        <div><b>Дата:</b> {date_str} | <b>Автор:</b> {author} | <b>Спикер:</b> {speaker}</div>
        <div><b>URL:</b> <a href="{html.escape(url)}">{html.escape(url)}</a></div>
        <div><b>Источник:</b> {rec_src}</div>
        <div><b>Маркеры:</b> {markers_badge}</div>
        <div><b>Анализ ИИ:</b> {context_analysis}</div>
    </div>
    {f'<div class="quote-box"><b>Выделенная цитата:</b> "{quote}"</div>' if quote else ''}
    <div><h3>Полный текст статьи:</h3>{rendered_paras}</div>
</body>
</html>"""

        page.set_content(html_template, wait_until="load")
        page.pdf(path=str(target_path), format="A4", print_background=True,
                 margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"})
        return True, str(target_path)
    except Exception as e:
        print(f"[!] Ошибка сборки PDF: {e}")
        return False, None
