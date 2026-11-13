# Покроковий план реалізації

Кожен milestone завершується робочим результатом і перевірками. Календарні строки визначаються після M0 та першого вертикального сценарію; кількість AI-сесій не є надійною одиницею оцінки. Не починати оформлення релізу до проходження основного циклу нарізання.

## M0 Перевірка середовища і контрактів

Залежності: немає. Власник надає ключ лише для live-перевірки; решта робіт виконується без нього.

1. Перевірити Windows/архітектуру, Git, Node, Python, FFmpeg/FFprobe, доступний диск. Reference target: Node 24, Python 3.12, актуальна сумісна стабільна Electron; точні версії вибрати, записати і зафіксувати lock-файлами.
2. Створити apps/desktop і apps/backend. Зберегти надані docs/contracts/tests. Додати .gitignore для ключів, .env, outputs, cache, recordings, .venv, node_modules та test-results.
3. Ініціалізувати Python environment; FastAPI, Pydantic 2, HTTPX, SQLAlchemy 2, Alembic, pytest, pytest-asyncio, Hypothesis, ruff, mypy. Використовувати один lock tool і не змішувати менеджери без потреби.
4. Створити frontend React/TypeScript/Vite та Electron main/preload. Додати lint, strict typecheck, Vitest/Testing Library, Playwright. Зафіксувати версії після перевірки сумісності.
5. Перевірити реальні STT endpoint/model IDs і output на 30–60 с дозволеного аудіо за наявності ключа. Записати timing_level, формат відповіді, language, помилки, usage. Додати redacted contract fixture. Якщо live недоступний — залишити позначку NOT RUN і продовжити на fake.
6. Для analysis model перевірити доступність, input context і JSON schema output. Модель окремо конфігурується; не використовувати STT endpoint для ранжування.
7. Вибрати варіант FFmpeg distribution, перевірити libx264/AAC, зібрати ліцензії й provenance. Перевірити sidecar packaging у найменшому прикладі відразу.

Вихід: app shell запускається, backend /health відповідає, toolchain документований, source contracts зафіксовані або live-блокер позначений. Команди dev/build/test працюють. Skill: cf-delivery-planner, cf-local-security.

## M1 Ядро джерел і точного нарізання

Залежності: M0. Вимоги FR-001–006, FR-030–032, FR-036–044, NFR-004.

1. Реалізувати SQLite schema/migration і репозиторії Project, Source, Clip, Export, Job.
2. Додати probe, timeline metadata, audio stream selection, fingerprint і SHA-256 background task.
3. Реалізувати pure timecode parser/formatter, interval validation, frame-boundary policy. Покрити їх unit/property tests.
4. Реалізувати FFmpeg argument builder та subprocess runner без shell. Додати progress parsing, timeout/cancel, захист шляхів.
5. Додати create/patch/approve clip API і export batch API. Snapshot clip revision перед render.
6. Писати output у temp, перевіряти duration/streams/decode, атомарно завершувати. Зберігати observed boundaries і manifest.
7. Запустити acceptance tests для ручного clipping, від'ємних меж, non-keyframe cut, Unicode, колізій, no-audio, multiple streams.

Вихід: із локального файлу за заданими інтервалами створюються перевірені кліпи; hash оригіналу незмінний. Наявність UI не є умовою завершення ядра. Skill: cf-media-timing.

## M2 Зручний ручний сценарій

Залежності: M1. Вимоги FR-029–035, NFR-001/002/006.

1. Реалізувати native file picker, project list, source inspector.
2. Підключити player через app-media protocol і backend stream IDs; перевірити range requests.
3. Реалізувати fallback proxy з source timeline mapping, якщо оригінал не відтворюється.
4. Додати In/Out, поля часу, ±1 с, preview loop, Save candidate, Approve, Export selected.
5. Реалізувати доступність, гарячі клавіші, збереження layout, стани loading/error/empty.
6. Додати Electron E2E: native dialog stub, ручний кліп, invalid interval, export, перезапуск і відновлення.

Вихід: людина без API-key створює й експортує кліп через UI. Skill: cf-desktop-ux.

## M3 Транскрипція та єдина часова шкала

Залежності: M1; UI може розвиватися після M2. Вимоги FR-013–020, FR-045–050.

1. Реалізувати TranscriptProvider protocol і два OpenRouter adapters.
2. Реалізувати audio extraction, chunk planner, payload-size check, core/context ownership, adaptive split.
3. Запитувати потрібні timing fields; робити capability validation, а не лише HTTP 200.
4. Нормалізувати provider response в contracts/transcript.schema.json. Відокремити unknown/non-speech/error від успішного тексту.
5. Merge chunks, time-aware dedupe, stable IDs, coverage map, transcript revision.
6. Додати SRT/VTT import із коректним hours parsing, multiline text, BOM і bounds validation.
7. Реалізувати retry policy, cache, spend ledger, checkpoint resume. Не повторювати unknown paid outcome автоматично.
8. Показати transcript search, click-to-seek, text correction і missing coverage.

Вихід: transcript правильно синхронізований на початку, chunk boundaries та наприкінці довгого source; 429/timeout/partial не гублять стан. Skills: cf-transcription, cf-media-timing.

## M4 Категорії і відбір

Залежності: M3. Вимоги FR-007–012, FR-021–028.

1. Реалізувати search profile із п'ятьма категоріями, all checkbox, custom instruction, duration bounds, candidate cap.
2. Розділити transcript на semantic windows із контекстом і стабільними IDs. Визначити token budget, не покладатися на довільну довжину стріму в одному запиті.
3. Підключити prompts/select.system.md та strict output schema.
4. Перевіряти evidence, типи, категорії, bounds, timing_level. Обмежувати максимум candidate_count незалежно від AI.
5. Розрахувати rank детерміновано з model component scores. Виконати rerank та дедуплікацію.
6. Зберігати analysis runs і snapshot параметрів. Reanalysis не перезаписує reviewed/manual clips.
7. Написати tests для unknown evidence, prompt injection, category OR, empty selection, нульового результату, повторів і custom text.

Вихід: модельні пропозиції доказово прив'язані до джерела; візуальні висновки не маскуються як перевірені. Skill: cf-highlight-selection.

## M5 Повний сценарій перегляду

Залежності: M2 + M4. Вимоги FR-029–035, FR-042–044.

1. Інтегрувати candidate cards, filters/sort, transcript evidence, reason/rank, visual review badge.
2. Вивести missing transcript coverage. Показати «кандидат», доки користувач не затвердив.
3. Реалізувати selection checkboxes окремо від approval; bulk export approved only.
4. Додати edit revision, undo, stale export indicator, persist review decisions.
5. Зв'язати export batch із папкою, progress, частковими помилками та історією.
6. Перевірити 1100×720, 1440×900, 1920×1080 і Windows scaling 100/125/150%.

Вихід: end-to-end flow на fake provider через UI; жодних «успішних» demo-результатів у production. Skills: cf-desktop-ux, cf-quality-gates.

## M6 Відмови, дані та секрети

Залежності: M5. Вимоги FR-045–052 і NFR-003–005.

1. Перевірити interrupt/crash/resume, partial writes, disk full, permission denied, moved source.
2. Реалізувати lease recovery, single active local worker, bounded queues та backpressure.
3. Перевірити sidecar auth, IPC sender validation, local media routes, CSP, HTML escaping.
4. Перевірити secrets at rest, redaction, no secrets in argv/logs/exports, session-only fallback.
5. Перевірити bounded retries, Retry-After, reservation ledger, cancellation of child processes.
6. Реалізувати cache cleanup dry preview, resolved path guards, safe atomic export і DB backup/restore instruction.

Вихід: fault matrix проходить; немає втрати оригіналів або витоку ключів. Skill: cf-local-security.

## M7 Якість AI і продуктивність

Залежності: M6; потрібні матеріали/оцінка власника і live API для live gate.

1. Власник позначає сильні/слабкі моменти на 3–5 дозволених записах. Один залишити held-out.
2. Порівняти два STT на однакових уривках: власні назви, сленг, змішані мови, перекриття мовлення, шум. Виміряти word errors та timing drift; не робити висновок за одним уривком.
3. Калібрувати відбір на development subset, а не на held-out. Фіксувати precision@10, recall розмічених speech-evidenced сцен, border adjustment, cost/accepted clip.
4. Окремо зафіксувати silent visual misses. Нуль мовлення не повинен створювати вигадані репліки/сцени.
5. Виконати 6-hour tiny fixture correctness test та реальний 1080p довгий запис для performance; це різні перевірки.
6. Виміряти memory, rendering time, API time, user review time та витрати. Зберегти hardware/tool versions.

Вихід: звіт із результатами і межами. Якщо quality gate не досягнуто, виправити налаштування або чесно залишити його непройденим.

## M8 Пакування і приймання

Залежності: M7, усі P0 checks.

1. Зібрати PyInstaller sidecar та Electron installer/portable. Забезпечити запуск без встановлених Python/Node.
2. Запустити packaged smoke на чистій Windows 11 VM або іншій машині. Перевірити кирилицю в username/path.
3. Перевірити install/uninstall, restart, workspace location і збереження користувацьких даних.
4. Додати ліцензії, dependency inventory, checksums, версію build, user guide з 5–7 ілюстрованими кроками.
5. Оновити матрицю FR→test, test reports, known issues, live provider status і security report.
6. Передати build, source, locks і release report. Демонстрація: один manual clip та один AI-assisted export.

Вихід: придатна до встановлення версія та відтворювані інструкції. Непройдені release gates не приховувати.

## Правила завершення етапу

До коду додані необхідні тести; тестові очікування походять із вимог, не копіюють реалізацію. Перевірки запущено, а не лише написано. Немає unresolved blockers P0. Логи й результати не містять секретів. Зміни записані в Git і відображені у PROGRESS. За відсутності ключа продуктова розробка продовжується, а live gate лишається NOT RUN.
