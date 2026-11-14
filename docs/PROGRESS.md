# Стан реалізації

Оновлено 14 листопада 2026 року.

Реалізовано M0 (оболонка) і M1 (ядро джерел і точного нарізання, backend API без UI). Продукт ще не готовий до користувача: UI, транскрипція й відбір — M2+. Перевірки пакета — VALIDATION_REPORT.md.

| Milestone | Статус | Підстава |
|---|---|---|
| M0 | Виконано, live STT — NOT RUN (очікує власника) | Toolchain, backend/desktop shell, sidecar packaging smoke; див. запис 2026-11-13 нижче |
| M1 | Виконано 2026-11-14; live STT, long-source, clean-machine — NOT RUN | Див. запис 2026-11-14 «M1 завершено» нижче |
| M2 | Не розпочато | Desktop UI ще не створено |
| M3 | Не розпочато | Реальні provider adapters ще не створені |
| M4 | Не розпочато | Аналізатор ще не реалізований |
| M5 | Не розпочато | Повний user flow ще не інтегрований |
| M6 | Не розпочато | Fault/security gates очікують реалізації |
| M7 | Не розпочато | Потрібні матеріали власника та live API |
| M8 | Не розпочато | Release build очікує попередніх етапів |

Після кожного milestone додати дату, FR, змінені файли, команди й результати перевірок, known issues та наступну задачу. Ніколи не змінювати статус лише через існування цього опису.

2026-11-13 — Підготовка до M0: git init; Python 3.12.16 + uv 0.13.8 встановлено; .claude/settings.json очищено (test:promo, SUBAGENT_MODEL_FORCE); тест skills у tests/pack виправлено. `npm run qa:pack` — 14/14 PASS. Рішення: ADR 006–010. Далі: M0.

2026-11-13 — M0 Перевірка середовища і контрактів. FR: — (інфраструктура); T-041 закрито на рівні API.
- Середовище й версії: docs/TOOLCHAIN.md (Ryzen 5 5600, 32 ГБ, RTX 4060; Node 24.12.0, Python 3.12.16/uv 0.13.8, FFmpeg 8.1 gyan full з libx264/AAC, SHA-256 записані). Рішення: ADR 011–016.
- Створено: кореневий uv workspace (pyproject.toml, uv.lock, .venv), apps/backend (clipforge: config, server, api/security, errors, health; tests; packaging; scripts), npm workspace apps/desktop (Electron main+sidecar, preload, React renderer, Vitest, e2e smoke), package-lock.json, docs/TOOLCHAIN.md.
- Команди й результати (усі запущені оркестратором):
  - `uv sync --locked && uv run ruff check apps/backend && uv run mypy --strict apps/backend && uv run pytest apps/backend -q` — PASS, 146 passed.
  - `npm ci && npm run typecheck && npm run lint && npm test && npm run build` — PASS, Vitest 89 passed.
  - `npm run test:e2e:smoke` — PASS 1/1 (sidecar стартує, app-ready, «Тестові дані», sidecar завершується після закриття).
  - `npm run dev` — запускає electron-vite і Electron (ручний smoke 25 с).
  - `uv run python apps/backend/packaging/build_sidecar.py && … smoke_sidecar.py` — PASS (PyInstaller onedir 35,6 МБ, ready ≈0,9 с, data dir із кирилицею, 401/200, exit 0).
  - `npm run qa:pack` — 14/14 PASS; `npm run qa:syntax` — OK.
  - `npm run qa:acceptance` — FAIL очікувано: 1/17 (T-041 ok), 16 падають 404 на маршрутах M1+.
  - `npm run qa:e2e` (review.spec) — NOT RUN: потребує M2.
- Live: M0 п.5 STT — NOT RUN, інструмент готовий (apps/backend/scripts/stt_probe.py + синтетичний зразок 47 с, голос Zira en-US); 13.11 18:18 UTC власник запустив probe: обидві моделі HTTP 402 (баланс < $0,50 для аудіо), generation_id відсутній, нічого не списано; ключ прийнято. Формат verbose_json/timing не підтверджено live — повторити перед M3 після поповнення. M0 п.6 analysis model — відкладено на M4 рішенням власника.
- Рев'ю: reviewer знайшов 9 пунктів; виправлено логування винятків (ADR 013 уточнення, error_id) і порядок збереження в probe; решта задокументована (ADR 016) або прийнята (умовний skip без LAN-адреси).
- Обмеження: підказка під неактивними кнопками в ready-стані застаріла — кнопки стануть робочими в M2. Пастки — docs/known-issues.md.
- Далі: M1 (власник погодив продовжувати без live STT; повтор probe — перед M3): SQLite schema, probe/fingerprint, timecode, FFmpeg runner, clips/export API.

2026-11-13 — M1 пауза (на прохання власника, ліміти; продовжити після «Продовжуємо»).
- Прийнято (Done-when запущено оркестратором): T1 domain timecode/interval/naming/catalog (242 passed); T2 storage SQLite+Alembic+репозиторії (58 passed); T3a adapters/process.py (Job Object, cancel); T3b adapters/ffprobe, media_tools, fingerprint (T3a+T3b: 63 passed; ruff і `mypy --strict apps/backend` чисті); T-039 domain/subtitles.py (47 passed). Рішення: ADR 018 (рецепт exact cut), ADR 019; ADR 009 уточнено — субагенти лише sonnet/haiku.
- Зупинено незавершеними: T4 export engine (sonnet) — на диску чернетки `services/export_engine.py`, `services/errors.py`, `services/text.py`, `tests/test_export_engine.py`, не перевірені; при відновленні дати builder-у (sonnet) завершити їх за тим самим Done-when. T5a API projects/sources/clips/jobs — змін не зробив, перезапустити з нуля.
- Далі: T4 і T5a паралельно → T5b export-batches/jobs/manifest/CSV → `npm run qa:acceptance` (M1-тести мають пройти) → PyInstaller із міграціями → повні гейти → reviewer (sonnet) → коміт за згодою власника.

2026-11-14 — M1 відновлено. Прийнято (Done-when запущено оркестратором): T4 export engine (ruff, `mypy --strict` чисті; `test_export_engine.py` 41 passed); T5a API projects/sources/clips/jobs + старт із БД (повний `pytest apps/backend` 744 passed); T4b audio_mode all (`test_export_engine.py` 51 passed); T6 PyInstaller із міграціями (build 28,9 с, smoke PASS: 43,2 МіБ, ready 2,6 с, POST /v1/projects 201 у бандлі). Рішення: ADR 018 уточнено (`-ss` без origin), ADR 020. У роботі: T5b export-batches/cancel/manifest/CSV.

2026-11-14 — M1 пауза 2 (на прохання власника; продовжити наступного дня).
- T5b export-batches/cancel/manifest/CSV прийнято: ruff і `mypy --strict apps/backend` чисті; `pytest apps/backend` 801 passed; `npm run qa:acceptance` 11/11 M1-тестів PASS (T-041, T-002, T-004, T-015, T-013, T-003, T-006, T-005, T-007, T-008, T-009), 6 падають 404 на маршрутах M3/M4 (/transcripts/import, /analysis-runs) — очікувано; npm typecheck/lint/test (Vitest 89)/build — 0; qa:pack 14/14; qa:syntax OK; sidecar build+smoke PASS (43,3 МіБ, ready 2,6 с).
- Рев'ю M1 (reviewer, sonnet): 15 пунктів, high немає. Виправлено builder-ами (sonnet/haiku), Done-when запущено оркестратором, якщо не сказано інше:
  - export_engine: decode-check `-map 0` (усі доріжки audio_mode=all), код NO_VIDEO_FRAMES для інтервалу без кадру — `test_export_engine.py` 56 passed.
  - exports ч.1: stale лише за зміни меж (як ClipService.edit), progress `completed` монотонний + `failed`, тест snapshot для queued item — `test_api_export_batches.py` 49 passed.
  - exports ч.2: on_progress → stage render/verify + item_percent (≤1 запис/0,5 с), WARNING із хвостом stderr ≤4 КБ без шляхів, recover() пише manifest/CSV — builder: 54 passed; оркестратор окремо НЕ запускав, покриває повний прогін нижче.
  - storage: WAL лише після успішної міграції (чужа/новіша БД не змінюється, перевірено SHA-256), видалено невикористаний `interrupt_running` — 38 passed.
  - sources/jobs: failed-хеш перераховується на старті, якщо файл знову доступний (новий `services/source_files.py`) — 36 passed.
  - projects (FR-001): PATCH /v1/projects/{id} {name?, archived?}, `include_archived`, `ProjectRepository.unarchive` — 85 passed.
  - pytest-cov 7.1.0 (coverage 7.17.3) + `[tool.coverage]` у pyproject.toml, uv.lock оновлено.
  - Документація: ADR 017/019/020 уточнено; known-issues — синхронний SHA-256 у relink (→ M6), failed-хеш, хелпер `add_source`, manifest після recover.
- Після всіх виправлень: `uv run ruff check apps/backend` — чисто; `ruff format --check` — чисто (1 файл переформатовано); `uv run mypy --strict apps/backend` — 82 файли, чисто.
- Запущено, результат НЕ перевірено: `uv run pytest apps/backend -q -p no:cacheprovider --cov=clipforge --cov-branch --cov-report=term --cov-report=xml:test-results/backend/coverage.xml --junitxml=test-results/backend/junit.xml` → лог `test-results/backend/pytest-full.log` (у кінці `EXIT=`). Якщо лог неповний — перезапустити.
- Далі (при відновленні): 1) перевірити лог: усі passed; покриття domain ≥90% branch, backend ≥80% (docs/07 §4), інакше дописати тести; 2) `npm run qa:acceptance`; 3) перезбірка sidecar + `smoke_sidecar.py` (поточний build старший за код); 4) за бажанням — короткий повторний reviewer виправлень; 5) фінальний запис M1 (live STT, long-source, clean-machine — NOT RUN) і статус M1 «Виконано»; 6) коміт M1 — лише за згодою власника. Усе M1 поки не закомічене (останній коміт 409e1bb, M0).

2026-11-14 — M1 фінальні гейти, крок 1 (pytest із покриттям). Перший прогін: 1 failed / 849 passed — flaky `test_concurrent_patches_with_the_same_revision_have_exactly_one_winner`. Причина: паралельне `create_app` у потоках, а FastAPI `warnings.catch_warnings()` не потокобезпечний. Виправлено тест (клієнти будуються до пулу; паралельні лише PATCH), 10/10 повторів PASS, пастка в known-issues. Повторний прогін `uv run pytest apps/backend -q -p no:cacheprovider --cov=clipforge --cov-branch …` — **850 passed, EXIT=0** (8 хв 22 с); покриття backend 89% (поріг 80%), domain 94–100% branch (поріг 90%); `server.py` 0% у звіті — він запускається підпроцесом у test_server_*, coverage підпроцеси не відстежує. Звіти: test-results/backend/{pytest-full.log, junit.xml, coverage.xml}. Ruff і `mypy --strict apps/backend` чисті. Далі: `npm run qa:acceptance` → перезбірка sidecar + smoke → фінальний запис M1 → коміт за згодою власника.

2026-11-14 — **M1 завершено** (Ядро джерел і точного нарізання). FR-001–006, FR-030–032, FR-036–044, NFR-004 на рівні backend API; UI — M2.
- Створено: apps/backend/clipforge/{domain, storage (SQLite + Alembic, репозиторії), adapters (process/Job Object, ffprobe, media_tools, fingerprint), services (clips, sources, projects, jobs, export_engine, exports, source_files, runtime), api (projects, sources, clips, jobs, exports, pagination)}; 20 нових test-файлів; PyInstaller-бандл із міграціями.
- Фінальні гейти (усі запущені оркестратором 14.11 після виправлень з рев'ю):
  - `uv run ruff check apps/backend` — чисто; `ruff format --check` — 82 файли OK; `uv run mypy --strict apps/backend` — 82 файли, чисто.
  - `uv run pytest apps/backend --cov=clipforge --cov-branch …` — 850 passed, 0 failed/skipped; покриття backend 89% (≥80%), domain 94–100% branch (≥90%). Звіти: test-results/backend/{junit.xml, coverage.xml, pytest-full.log}.
  - `npm run qa:acceptance` — 11/11 M1 PASS (T-041, T-002, T-004, T-015, T-013, T-003, T-006, T-005, T-007, T-008, T-009); 6 fail з 404 на маршрутах M3/M4 (T-024, T-031×2, T-022, T-035, T-033) — очікувано. Лог: test-results/acceptance/acceptance.log.
  - Решта M1-рядків матриці docs/07 покрита pytest: T-001, T-010, T-011 (relink/source changed), T-012 (колізія імені), T-039, T-049.
  - Sidecar: `build_sidecar.py` — 39,6 с (PyInstaller 6.22.3); `smoke_sidecar.py` — PASS, 43,3 МіБ, ready 2,5 с. Логи: test-results/sidecar/.
  - npm typecheck / lint / test (Vitest 89 passed) / build — 0; `npm run qa:pack` 14/14; `npm run qa:syntax` OK.
- NOT RUN: live STT (T-054; OpenRouter 402 до поповнення балансу), long-source 6h/1080p (T-052/T-053 → M7), clean-machine package (T-056 → M8), `npm run qa:e2e` (потребує M2).
- Обмеження й відкладене: синхронний SHA-256 у relink → M6; T-014 (зміна кліпу під час render) — snapshot реалізовано, повна перевірка → M6; hypothesis у бандлі → M8. Деталі — docs/known-issues.md.
- Далі: M2 — зручний ручний сценарій (file picker, player через app-media protocol, In/Out, експорт з UI, Electron E2E).
