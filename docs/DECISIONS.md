# Архітектурні рішення

## ADR 001 Локальний desktop

Windows 11 x64, Electron/React/TypeScript + Python FastAPI sidecar, SQLite, FFmpeg. Причина: native paths для великих файлів, зручний review UI, reuse Python media/AI ecosystem. Наслідок: два runtime і окремий packaging gate; перевірити sidecar bundle у M0. macOS/Linux не входять у перший release gate.

## ADR 002 Текстовий відбір

MVP використовує транскрипцію та ручну перевірку картинки. Причина: погоджений фокус — нарізання, STT і категорії. Наслідок: silent visual events можуть бути пропущені; UI і benchmark розділяють їх. Video understanding — окреме майбутнє розширення.

## ADR 003 Тести перед реалізацією

Пакет містить black-box HTTP/UI tests, які фіксують public behavior. Вони доповнюються unit/integration під час написання коду. Невиконані продуктні тести позначаються NOT RUN, а перевірки пакета — окремо.

## ADR 004 Точність часу

Integer milliseconds, source-relative canonical origin, [start,end). Word/segment IDs є evidence і не замінюються вигаданими LLM часами. FFmpeg policies приймаються після content-boundary tests, а не за синтаксично правдоподібною командою.

## ADR 005 Платні запити

Default tests offline. Live вмикається явно з API-key і ceiling. Unknown paid timeout не повторюється автоматично. Причина: локальний клієнт не може гарантувати exactly-once billing у стороннього сервісу.

## ADR 006 Toolchain Python (2026-11-13)

Python 3.12 (3.12.16), встановлений через uv у профіль користувача; системний Python 3.14 не використовується. Єдиний менеджер залежностей і lock tool для backend — uv (uv.lock). Node 24.12.0, FFmpeg 8.1 gyan.dev full build.

## ADR 007 STT за замовчуванням (2026-11-13)

Власник обрав microsoft/mai-transcribe-2 основною STT-моделлю. Адаптер google/gemini-3.5-transcribe лишається за FR-013 (P0) як перемикний, але не default. Модель аналізу ще не обрана: визначається в M0 п.6 / M4 після capability probe. Ключі OpenRouter власник надає на запит у момент live-перевірки; до того — fake provider, live gates NOT RUN.

## ADR 008 Пакування і ліцензія FFmpeg (2026-11-13)

Програма лише для особистого використання власником, без поширення; GPL-збірка FFmpeg (libx264) допустима, ліцензії все одно включаються в inventory. Packaged-перевірку M8 виконуємо на ПК власника (per-user installer без прав адміністратора). Цей ПК має dev Python/Node, тому T-056 «чиста Windows» на ньому не доводиться і звітується як NOT RUN, якщо не буде окремої VM.

## ADR 009 Моделі субагентів (2026-11-13)

builder/reviewer за замовчуванням Haiku. Задачі з медіа, таймінгом, FFmpeg, chunk/merge транскрипції (skills cf-media-timing, cf-transcription) оркестратор запускає з model=opus. CLAUDE_CODE_SUBAGENT_MODEL_FORCE прибрано з .claude/settings.json, щоб override працював.

## ADR 010 Виправлення тесту пакета (2026-11-13)

tests/pack «seven project skills» рахував усі записи .claude/skills і падав після додавання власного skill reflect. Тепер рахує лише cf-* (рівно 7). Acceptance-тести не змінювались.

## ADR 011 Frontend toolchain (2026-11-13, M0)

npm workspaces: один package-lock.json у корені, member apps/desktop. Точні pins (без ^/~): electron 45.3.2, electron-vite 6.0.0, vite 8.3.8, react/react-dom 19.3.1, @vitejs/plugin-react 6.3.0, vitest 5.1.0, eslint 10.14.0, typescript-eslint 8.72.2, @playwright/test 1.65.0 (у кореневому package.json, бо qa:e2e запускається з кореня). TypeScript 6.0.3, а не 7.x: typescript-eslint 8.72 має peer `typescript <6.1.0`. Переглянути, коли typescript-eslint підтримає TS 7.

## ADR 012 Python layout (2026-11-13, M0)

Кореневий pyproject.toml — віртуальний uv workspace (member apps/backend), тому uv.lock і .venv лежать у корені: саме `.venv/Scripts/python.exe` очікує tools/run-acceptance.mjs. Пакет `clipforge` у apps/backend; runtime і dev залежності закріплені `==` і в uv.lock. Dev group: pytest, pytest-asyncio, hypothesis, ruff, mypy, pyinstaller.

## ADR 013 Контракт запуску sidecar (2026-11-13, M0)

`python -m clipforge.server --port N [--config-stdin]`, cwd apps/backend.
- `--port 0` — ефемерний порт; після bind sidecar друкує один рядок stdout `{"event":"ready","port":N}`. Main не вгадує вільний порт (немає race).
- `--config-stdin` — перший рядок stdin: JSON `{"mode":"production"|"test","data_dir":"...","session_token":"..."}`. Після цього sidecar стежить за stdin і завершується на EOF (смерть батька не лишає сироти).
- Без `--config-stdin` конфігурація з env (§6 у 04_ARCHITECTURE_API) дозволена лише при CLIPFORGE_MODE=test. Production відхиляє CLIPFORGE_MODE=test, будь-які CLIPFORGE_TEST_* і не-loopback provider URL; test provider URL лише http://127.0.0.1|localhost.
- Bind лише 127.0.0.1. Bearer на всіх маршрутах (constant-time порівняння); 401 і чужий Host — до читання даних; помилки у форматі §4 з українським message; 500 без тексту винятку; токен ніколи не логуються (access log вимкнено).

## ADR 014 Test mode у desktop (2026-11-13, M0)

Electron main вмикає test mode лише якщо env CLIPFORGE_E2E=1 або CLIPFORGE_MODE=test; тоді передає sidecar `mode:"test"` і data dir із CLIPFORGE_DATA_DIR, а renderer показує бейдж «Тестові дані». Інакше production і data dir у app.getPath('userData'). Пакування (M8) має прибрати цей шлях або підтвердити, що він не активується випадково.

Уточнення до ADR 013 після реалізації (M0): чужий Host → 400 `INVALID_HOST`; перевірка Host іде до токена (дозволено лише `127.0.0.1:<port>` і `localhost:<port>`). Auth і Host — чистий ASGI middleware до роутингу, тому й невідомий маршрут без токена дає 401. 404/405/422/500 теж у форматі §4; 422 містить лише `loc`/`type` без значень запиту; docs/redoc/openapi вимкнені. Stdin-конфіг має строгий набір ключів, `data_dir` абсолютний, токен ≥32 друкованих ASCII. Ready-рядок друкується після старту uvicorn на заздалегідь прив'язаному сокеті 127.0.0.1.

## ADR 015 HTTP-клієнт backend — httpx2 (2026-11-13, M0)

План M0 називає HTTPX. Пакет `httpx` не оновлювався з 0.28.1 (грудень 2024); Starlette 1.x TestClient вимагає `httpx2` (github.com/pydantic/httpx2, 2.16.0 від 21.10.2026). Використовуємо один клієнт `httpx2` і в production (OpenRouter adapters), і в тестах; `httpx` не додаємо. Runtime також: SQLAlchemy 2.1.8, Alembic 1.20.1.

## ADR 016 Дрібні рішення desktop M0 (2026-11-13)

- Preload відкриває `window.clipforge = {bridgeVersion: 1, getStatus}`. `bridgeVersion` — заморожена константа для сумісності мосту, не можливість; токен, fetch і Node API renderer не отримує (перевіряє E2E smoke).
- CSP у `electron-vite dev` (лише serve) послаблено до `'unsafe-inline'` для скрипта React Refresh і `ws:` для HMR. Зібраний/пакований renderer має строгий `default-src 'self'` (перевіряє Vitest-тест index.html). Packaged build не використовує dev-сервер.
- Додаткові dev-pins поза ADR 011 (точні, без ^/~): @eslint/js 10.0.1, eslint-plugin-react-hooks 7.1.1, globals 17.14.0, @testing-library/dom 10.4.2, @testing-library/jest-dom 7.0.1, @types/node 24.19.2, @types/react 19.3.0, @types/react-dom 19.3.0, jsdom 29.1.1.

Уточнення до ADR 013 (M0, після рев'ю): необроблений виняток перехоплює зовнішній ASGI `UnexpectedErrorMiddleware`; у лог іде лише `Unhandled <ClassName> (error_id=<16 hex>)` без тексту винятку і traceback, відповідь 500 у форматі §4 має `details.error_id` для зіставлення з логом. Регресійний тест — apps/backend/tests/test_server_unexpected_error.py (реальний uvicorn у підпроцесі).
