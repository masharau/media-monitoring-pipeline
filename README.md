Исследовательский программный комплекс для глубокого каскадного обнаружения, преодоления сетевых барьеров (WAF/GeoIP), двухшаговой фактологической верификации и детерминированной долгосрочной архивации публикаций государственных СМИ Республики Беларусь.

---

## 1. Архитектура системы

Конвейер построен по модульному принципу **Directed Resilient Pipeline** с раздельными кольцами верификации и универсальным каскадом съема контента (Unified Cascade):
[ ИНИЦИАЛИЗАЦИЯ И ПРОВЕРКА СОСТОЯНИЯ (STATE TRACKING) ]
├── Проверка наличия локальной базы SQLite (pipeline_state_<YEAR>.db).
├── БАЗЫ НЕТ ──► Режим "FULL RUN": поиск инициируется принудительно.
└── БАЗА ЕСТЬ ─► Режим "INCREMENTAL": загрузка множества обработанных URL -> сбор только дельты.
│
▼
[ ЭТАП 1: КАСКАДНОЕ ОБНАРУЖЕНИЕ (СТРОГО ПОСЛЕДОВАТЕЛЬНО) ]
├── ШАГ 1.1: Прямой технический аудит БелТА (robots.txt, sitemap.xml, дампы дат, RSS).
├── ШАГ 1.2: Поисковые системы:
│ ├── Яндекс (эксплуатация транзитных индексов, обход капчи)
│ └── Google (стелс-запросы)
├── ШАГ 1.3: Социальный шлюз (@belta_telegramm с глубокой пагинацией по ID через before=).
└── ШАГ 1.4: Исторические веб-архивы (Wayback Machine CDX API с суффиксом id_, Arquivo.pt).
│
▼
[ ЭТАП 2: БЕСПЛАТНЫЙ БАЙПАСС И МЕХАНИЧЕСКИЙ ПАРСИНГ ]
├── Обход Yandex SmartCaptcha: Context Hijacking (постоянный профиль браузера для траста).
├── Stealth-стек: Снятие флагов navigator.webdriver, подмена Client Hints (Sec-Ch-Ua).
├── Универсальный каскад съема (Unified Cascade): Direct -> Wayback -> Arquivo -> Gateway.
└── Синтаксическая декомпозиция (BeautifulSoup4/lxml): удаление служебного шума (реклама, шапки).
│
▼
[ ЭТАП 3: РАЗДЕЛЕННАЯ ЛОГИКА ВЕРИФИКАЦИИ (DUAL-GATE VERIFICATION) ]
├── ПРОВЕРКА ПО ЗАГОЛОВКУ (Нулевое кольцо):
│ └── Поиск нормализованного маркера из sources.json в h1?
│ ├── ДА ──► Прямая безусловная запись в БД (status = COMPLETED).
│ └── НЕТ ─┐
│ ▼
└── КОНТЕКСТНАЯ ВЕРИФИКАЦИЯ (Первое кольцо):
├── Быстрый семантический шлюз (отсев бытового шума без вызова LLM за 0.001 с).
└── Семантический анализ полного текста через LLM (OpenRouter / Gemini API+):
├── РЕЛЕВАНТНО ──► Запись в БД (COMPLETED, фиксация спикера, цитаты и тем).
└── НЕ РЕЛЕВАНТНО ─► Отклонение (NO_MATCH_VERIFIED).
│
▼
[ ЭТАП 4: АРХИВАЦИЯ И ЭКСПОРТ ]
├── Изолированная генерация PDF через Playwright (context.new_page() -> close()).
├── Детерминированные имена: {date_published}_{sha1(canonical_url)[:12]}.pdf.
├── Атомарная запись через .tmp -> replace (гарантия отсутствия поврежденных PDF).
└── Экспорт верифицированного корпуса в corpus_<YEAR>.csv (RFC 4180, кодировка UTF-8-SIG).
code
Code
---

## 2. Технический паспорт системы

### 2.1. Конфигурационные параметры (Environment Variables)

| Переменная | Тип | Назначение / Дефолтное значение |
|---|---|---|
| `TARGET_YEAR` | `int` | Год исследуемого периода (по умолчанию: `2026`). |
| `TARGET_MONTH` | `int` | Месяц исследуемого периода (по умолчанию: `9` — сентябрь). |
| `SQLITE_DB` | `str` | Путь к базе SQLite (`pipeline_state_2026.db`). Корпус 2021 года изолирован. |
| `TARGET_QUOTA_PER_DOMAIN` | `int` | Лимит отобранных статей `COMPLETED` (`0` — полный сбор всего массива). |
| `OR_KEY` | `str` | API-токен OpenRouter. |
| `OR_MODEL` | `str` | Модель семантической классификации (`recursive/flint-v1.0:free`). |
| `GEMINI_API_KEY` | `str` | Резервный ключ Google AI Studio для обхода лимитов. |
| `USER_AGENT` | `str` | Профиль Linux Desktop x86_64 с заголовками Client Hints (`Sec-Ch-Ua`). |

### 2.2. Схема базы данных SQLite

```sql
CREATE TABLE IF NOT EXISTS articles (
    canonical_url TEXT PRIMARY KEY,          -- Оригинальный канонический URL на belta.by
    recovery_snapshot_url TEXT,              -- Прямой URL архивного снимка (если восстанавливалось)
    domain TEXT NOT NULL,                     -- "БелТА"
    year INTEGER NOT NULL,                    -- 2026
    date_published TEXT,                     -- Подтвержденная дата публикации (строго YYYY-MM-DD)
    date_source TEXT,                        -- DUMP_SITEMAP, JSON-LD, META, TIME_TAG
    title TEXT,                              -- Заголовок из тега <h1>
    author TEXT,                             -- Извлеченный автор или "UNKNOWN"
    full_text TEXT,                          -- Авторский текст статьи без шума
    found_markers TEXT,                      -- JSON-массив найденных маркеров темы
    context_analysis TEXT,                   -- Аналитическое заключение
    analysis_source TEXT,                    -- title_marker_direct, openrouter_llm, context_fallback
    quote TEXT,                              -- Дословная цитата спикера по теме
    speaker TEXT,                            -- Атрибутированный спикер
    pdf_path TEXT,                           -- Абсолютный путь к файлу PDF
    recovery_source TEXT,                    -- DIRECT, WAYBACK, ARQUIVO, TELEGRAM_GATEWAY
    status TEXT NOT NULL,                    -- COMPLETED, NO_MATCH_VERIFIED, DATE_UNVERIFIED, UNAVAILABLE, LLM_FAILED
    http_code INTEGER,                       -- Фактический код ответа сервера (200, 403, 404, 503)
    fetch_date TEXT NOT NULL,                -- ISO-8601 таймстемп обработки
    error_message TEXT                       -- Лог ошибки при сбое
);

CREATE INDEX IF NOT EXISTS idx_art_status ON articles(status);
CREATE INDEX IF NOT EXISTS idx_art_date ON articles(date_published);
3. Реестр устраненных инженерных инцидентов (Troubleshooting Log)
Геоблокировка входящего трафика из ЕС:
Проблема: Провайдер БелТА блокирует входящие HTTP-соединения из зарубежных дата-центров (Timeout / Drop).
Решение: Универсальный каскад съема (Unified Cascade) с автоматическим переключением на нейтральные глобальные узлы: Wayback Machine CDX (с суффиксом id_), Arquivo.pt Replay и распределенный сетевой шлюз.
Заглушка DDoS-Guard (HTTP 503 Service Unavailable):
Проблема: Сервер выдает проверку браузера с кодом 503 при запросах со стандартного HTTP-клиента.
Решение: Запуск Playwright с флагом --disable-blink-features=AutomationControlled и маскировкой Client Hints (Sec-Ch-Ua).
SSL-сбой боевого сервера БелТА (HTTPS -> 404 Not Found):
Проблема: Архивные пути БелТА отдают 404 по HTTPS из-за особенностей маршрутизации веб-сервера.
Решение: Принудительный перевод ссылок БелТА на канонический протокол http:// и очистка от мусорного сегмента /view/.
Обход Yandex SmartCaptcha и Google Sorry:
Проблема: Headless-браузер блокируется страницами проверки (checkbox.yandex.net).
Решение: Использование постоянного контекста (Context Hijacking) в каталоге ./browser_profile с сохранением сессионных кук и рандомизацией пауз.
Предотвращение зависания на бытовых новостях (Быстрый семантический шлюз):
Проблема: Отправка тысяч бытовых статей (ДТП, урожай, спорт) в LLM приводила к лимитам 429 и задержкам на несколько часов.
Решение: Мгновенный отсев статей без ключевых корней темы за 0.001 секунды перед обращением к ИИ.
Защита от затирания успешных данных в БД:
Проблема: При повторном прогоне с сетевым сбоем статус COMPLETED затирался статусом UNAVAILABLE.
Решение: Защищенный UPDATE с условием status = CASE WHEN articles.status = 'COMPLETED' THEN 'COMPLETED' ELSE excluded.status END и функциями COALESCE.
Утечки памяти и атомарность PDF:
Проблема: Рендеринг PDF в рабочей вкладке вызывал OOM-падение Chromium.
Решение: Печать в изолированной вкладке context.new_page() с блоком try/finally и атомарной записью через .tmp -> replace. Имена детерминированы через SHA1-хэш канонического URL.
