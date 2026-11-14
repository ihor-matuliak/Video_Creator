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

Уточнення 2026-11-13 (вказівка власника, M1): Opus — лише для головного оркестратора. Субагенти (builder, reviewer, Explore тощо) ніколи не запускаються з model=opus: складні задачі (медіа, таймінг, FFmpeg, транскрипція, безпека, storage) — sonnet, прості — haiku. Пункт про model=opus вище скасовано.

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

## ADR 017 Каркас ядра M1 (2026-11-13)

- Шари backend за docs/04 §2: `clipforge/domain` (чисті функції: timecode, interval, naming, catalog), `clipforge/storage` (SQLAlchemy 2.1 + Alembic), `clipforge/adapters` (процеси, ffprobe, ffmpeg, fingerprint), `clipforge/services` (оркестрація, export, jobs). Domain не імпортує FastAPI/SQLAlchemy.
- SQLite: `<data_dir>/clipforge.sqlite3`, WAL, `foreign_keys=ON`, busy_timeout; міграції Alembic запускаються програмно під час старту; перед upgrade наявної БД — копія в `<data_dir>/backups/`; при невдалій міграції sidecar завершується з кодом 2, попередня схема лишається. Workspace lock `<data_dir>/workspace.lock` (msvcrt) на весь час процесу: другий sidecar на той самий data dir не стартує. ID — uuid4-рядки; час журналів — ISO 8601 UTC; JSON-поля мають `schema_version`.
- FFmpeg/FFprobe: шляхи визначаються один раз під час старту — у test mode з `CLIPFORGE_FFMPEG_PATH`/`CLIPFORGE_FFPROBE_PATH`, інакше `shutil.which` → абсолютний шлях; версії записуються в manifest. Немає інструментів → media-маршрути повертають 503 `MEDIA_TOOLS_UNAVAILABLE`, health працює. У M8 Electron передасть шляхи бандла через stdin-конфіг.
- Jobs у M1: рядки Job у БД + потоки-воркери в процесі sidecar (export — 1, SHA-256 — 1). На старті `queued`/`running`/`cancelling` → `interrupted` (уточнено 2026-11-14, див. ADR 019). Leases/resume — M6.
- Рецепт exact cut (порядок seek/trim/setpts) фіксується окремим ADR після spike і тестів T-003/T-008/T-009.

## ADR 018 Рецепт exact cut (2026-11-13, M1 spike)

Spike на копіях фікстур (FFmpeg 8.1). S/E — секунди від нуля контейнера: canonical origin + ms/1000; filtergraph будується лише з чисел, відформатованих кодом.

```
ffmpeg -ss <max(0,S-3)> -copyts -i <src>
  -filter_complex "[0:v:0]trim=start=S:end=E,setpts=PTS-S/TB[v];
                   [0:<aidx>]atrim=start=S:end=E,asetpts=PTS-S/TB,aresample=async=1:first_pts=0[a]"
  -map [v] -map [a] -c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p -bf 0
  -fps_mode passthrough -enc_time_base:v demux -c:a aac -b:a 192k <tmp>.mp4
```

- `-copyts` + trim/atrim за абсолютним часом дає межі за фактичними PTS (non-keyframe start: перший кадр зелений, PTS 0,013; VFR: інтервали 0,033 і 0,067 збережені).
- `aresample=async=1:first_pts=0` заповнює тишею пізній старт аудіо (audio_offset: тиша до 1,000 с, rms 0,0 / 0,088), а не лише зсуває контейнер.
- `-enc_time_base:v demux` — без нього x264 квантує PTS до 1/fps. `-bf 0` для всіх експортів: з B-кадрами MP4 edit list обрізає останній VFR-кадр і duration виходить 2,9 замість ~2,97 с; `+negative_cts_offsets`/`-use_editlist 0` не допомагають. Ціна — трохи більший файл за того ж CRF. Тривалість останнього VFR-кадру береться з default frame interval.
- Pre-seek (`-ss S-3` до `-i`) безпечний лише для форматів з індексом (mov/mp4, matroska). На MPEG-TS seek перескакує на наступний keyframe після цілі (GOP 10 с → вихід порожній). Тому: pre-seek лише для mov/mp4/matroska; для решти — декодування від початку; якщо перевірка показує порожній вихід або перший кадр пізніше за допуск після pre-seek — один повтор без pre-seek. `seek_mode` (`preseek`/`full_decode`) записується в manifest.
- Без аудіо або `audio=none` — лише `[v]`. Rotation/odd dimensions/HDR — окремими кроками експорту за docs/05 §2 (HDR → unsupported).
- Уточнення (2026-11-14, T4): значення input `-ss` FFmpeg відраховує від `start_time` контейнера (додає його сам), тому pre-seek — `-ss max(0, start_ms-3000)/1000` **без** origin; trim/atrim лишаються в абсолютному часі (origin + ms/1000) завдяки `-copyts`. Варіант `-ss S-3` з origin на джерелі з початком 10 с перескакує ціль. Регресія: `test_nonzero_origin_with_preseek[mkv|mp4]` (зсув 10 с, preseek, усі кадри зелені).

## ADR 019 Дрібні рішення domain і storage M1 (2026-11-13)

- Ім'я експорту: `<stem>_<clip_id>_r<revision>_<HHhMMmSSsmmm>-<HHhMMmSSsmmm>.mp4` (двокрапка в Windows заборонена), повний clip_id, NFC, `<>:"/\|?*` і символи категорій C* → `_`, зарезервовані імена отримують префікс `_`, ліміт 150 UTF-16 одиниць (скорочується лише stem), колізії — суфікс ` (N)`. `first_free_name` лише пропонує ім'я: фіналізація створює/перейменовує без перезапису.
- CSV-каталог: категорії через `", "`, CRLF, текстові клітинки, що починаються з `= + - @ \t \r \n` (зокрема повноширинних або після пробілів), отримують префікс `'`; самотні сурогати → `?`.
- Міграція Alembic — одна транзакція (`transactional_ddl`), невдала ревізія лишає попередню схему й `alembic_version`; БД з таблицями без версії або з новішою невідомою версією не чіпається (`MigrationError`, без бекапу; уточнено 2026-11-14: і journal mode теж — `PRAGMA journal_mode=WAL` виконується лише після успішної міграції, а не в connect-hook). Бекап — `sqlite3.backup` + `integrity_check` у `backups/clipforge-<UTC>-from-<rev>.sqlite3`.
- `Database.session()` — deferred BEGIN, `write_session()` — `BEGIN IMMEDIATE`. JSON-поля зберігаються як `{"schema_version":1,"value":...}`.
- `ClipRepository.update` змінює лише title, start_ms, end_ms, categories, review_status, visual_review_required; review теж збільшує revision; `set_export_state` revision не збільшує, щоб завершення експорту не робило правку користувача застарілою. Позначка verified→stale при зміні меж (FR-032) — у сервісі.
- `Source.mtime` — `st_mtime_ns`; `canonical_origin` — JSON `{origin_ms,time_base}`; CHECK лише для перелічених у docs наборів (review_status, export_state, job.state); `UNIQUE(batch_id, clip_id)` у export_items — дедуплікація з docs/04 §5. `clips.analysis_run_id` поки без FK — додати разом із таблицею analysis_runs у M4.
- Рестарт (уточнено 2026-11-14 за рев'ю M1): `JobRepository.interrupt_unfinished()` переводить `queued`, `running` і `cancelling` → `interrupted`, бо в M1 job живе лише в пам'яті процесу й після рестарту воркера в неї немає. Стан власників відновлюється окремо: hash job створюється заново, export batch закриває `ExportService.recover()`. Leases і resume — M6.
- SRT кліпу (`domain/subtitles.py`, FR-043): cue зсуваються до нуля кліпу й обрізаються межами; сегмент, що перетинає межу, зберігає весь текст (без word timing розбити неможливо); теги/коментарі видаляються до стабілізації, переводи рядків і табуляції → один пробіл, нумерація з 1 за часом.

## ADR 020 API джерел, кліпів і export batches M1 (2026-11-14)

- Старт sidecar: `open_database` → помилка міграції/блокування workspace/OSError → stderr і код виходу 2 без ready-рядка. Немає FFmpeg/FFprobe → `media_tools=None`, `/v1/health` 200, медійні маршрути 503 MEDIA_TOOLS_UNAVAILABLE.
- `Source.hash_status`: pending/computed/failed; `availability`: available/missing/changed (GET /v1/sources/{id} звіряє з файлом). POST /v1/sources повертає `hash_job_id`. На рестарті незавершені jobs → interrupted, а джерела з pending-хешем отримують новий hash job (інакше relink заблокований назавжди). Уточнено 2026-11-14: failed-хеш теж повертається в pending і перераховується, якщо файл за збереженим шляхом знову regular file з тим самим size і mtime_ns (`services/source_files.current_availability`); missing/changed лишаються failed. Хеш, що впав не через файл (HASH_FAILED), перераховується на кожному старті — по одному job, обмежено одним воркером.
- Relink: лише після обчисленого SHA-256; інший вміст → 409 SOURCE_MISMATCH; поки хеш не готовий → 409 SOURCE_HASH_PENDING/SOURCE_HASH_UNAVAILABLE.
- PATCH /v1/clips/{id}: title, start_ms, end_ms, categories (null = не змінювати); stale revision → 409 STALE_REVISION з `current_revision`, перевіряється до валідації значень. verified → stale лише за фактичної зміни меж. Те саме правило діє, коли рендер завершується після правки: межі кліпу ≠ межам snapshot → stale; зміна title/review під час рендеру лишає verified (уточнено 2026-11-14).
- Export profile `audio_mode`: selected (один глобальний stream index), none, all (усі audio streams джерела в порядку індексів, кожен зі своїм offset; `ExportRequest.audio_stream_indexes`). Інші значення → 422.
- `write_srt: true` у M1 → 422 SRT_REQUIRES_TRANSCRIPT: транскриптів ще немає (M3 під'єднає `build_clip_srt`); жодного тихого пропуску.
- Списки сортуються за (created_at, id); `utc_now` на Windows тикає ~15 мс, тож записи з однаковим часом ідуть за id — курсор стабільний.
- Export batch (T5b): порядок перевірок — 503 без FFmpeg → профіль і output_dir (422) → в одній write-транзакції проєкт/кліпи 404, чужий проєкт 422 CLIP_PROJECT_MISMATCH, аудіодоріжка 422 → 409 CLIP_NOT_APPROVED (details.clip_ids); частковий batch не створюється. Один worker на всі export jobs; порядок items — `snapshot_json.position`. Batch→job через `JobRepository.find_by_checkpoint`; шляхи manifest/catalog — у `checkpoint_json` job.
- Файли batch: `manifest_<batch_id>.json` і `catalog_<batch_id>.csv` в output_dir через temp + `os.replace`; пишуться і для failed/cancelled/partial_failed; batch, скасований у черзі, їх не має (paths = null).
- Перед рендером size і mtime_ns джерела звіряються з зареєстрованими: розбіжність → item SOURCE_CHANGED, файлу немає → SOURCE_UNAVAILABLE, FFmpeg не запускається; quick fingerprint звіряє движок.
- Статус batch: є cancelled item → cancelled, інакше succeeded/failed/partial_failed. Job: cancelled; failed, якщо жоден кліп не експортовано або не записано manifest/CSV; partial_failed → job succeeded. Progress export job: `{stage,total,completed,failed}`, де `completed` — усі завершені items (verified + failed + cancelled), тож значення не спадає; `failed` — лише failed (уточнено 2026-11-14).
- Cancel: queued → одразу cancelled без manifest; running → cancelling, FFmpeg зупиняється, незавершені items cancelled, готові зберігаються; повтор ідемпотентний (202); завершена або не-export job → 409 JOB_NOT_CANCELLABLE.
- Старт: `ExportService.recover()` переводить відкриті batch у failed/partial_failed, items — INTERRUPTED, кліпи — failed, видаляє лише temp із точним ім'ям движка (`.clipforge-<task_uuid>.part`). `Runtime.close()` зупиняє рендер без запису в БД.
- FR-001 (2026-11-14, рев'ю M1): PATCH /v1/projects/{id} `{name?, archived?}` → 200 (форма POST + `archived`); GET /v1/projects приховує архівні, якщо не `include_archived=true`. Порядок: 404 PROJECT_NOT_FOUND → 422 INVALID_PROJECT_NAME → 422 NO_CHANGES (порожнє тіло, та сама назва після trim або той самий стан архіву). docs/04 §4 таблиця маршруту не мала — це розширення контракту, не конфлікт.
- Перевірка виходу (2026-11-14): decode-check FFmpeg запускається з `-map 0`, тож повністю декодуються всі потоки, зокрема кожна доріжка audio_mode=all. Інтервал без жодного відеокадру → item `NO_VIDEO_FRAMES` (окремий прохід `-f null` з тим самим ланцюжком і `-progress frame=0`, не текст stderr; preseek → повтор full_decode), а не загальний FFMPEG_FAILED. Мінімальної тривалості кліпу API не вводить.
- Прогрес і діагностика export (2026-11-14, рев'ю M1): `on_progress` движка → progress_json `{stage: render|verify, total, completed, failed, item_percent}`; `item_percent` = out_time / тривалість кліпу в межах стадії (0–100, verify починає з 0), запис у БД не частіше ніж раз на 0,5 с, перший звіт кожної стадії — одразу. Відмова item → один WARNING `batch/item/code/detail`: хвіст stderr ≤4 КБ, шлях джерела, output dir і temp замінені на `<source>`/`<output>`/`<temp>`; detail ніколи не йде в API, БД чи manifest. `ExportService.recover()` після закриття batch пише manifest/CSV тим самим writer, що й звичайне завершення (best effort: немає output dir → paths = null, старт не падає).
