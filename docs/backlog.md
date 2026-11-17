# Задачи

Стан на 2026-11-17: M3 закомічено (`3b068f4`). M4 (відбір моментів, ADR 041) завершено, ще не закомічено. Наступний — M5 (повний user flow, docs/02_IMPLEMENTATION_PLAN.md). Кандидати на M5 з M4 — у docs/known-issues.md (збереження першої відповіді для repair) і в розділі «Відкриті питання» нижче (блок «Додатково» в панелі «Що шукати»).
Перед стартом прочитати docs/PROGRESS.md і ADR 041. Desktop-задачі виконуються послідовно (спільні out/ і Vitest), backend можна паралельно.

## M4 — план (ADR 041)

- Т1 `domain/search_profile.py` — профіль пошуку (§10, T-031).
- Т2 `domain/analysis_windows.py` — одиниці й вікна (§3).
- Т3a `domain/selection_output.py` — розбір і перевірка відповіді моделі (§5, T-033, T-034).
- Т3b `domain/selection_scoring.py` — rank, поріг, recommended interval (§6).
- Т4 `domain/selection_dedupe.py` — дедуплікація, різноманітність, ліміт (§7, T-036, T-037).
- Т5 storage r0007 + репозиторії (§8). Т6 `adapters/openrouter_chat.py` — профілі моделей, тіло запиту, класифікація відповіді (§1, §4).
- Т7a сервіс create/plan/estimate; Т7b виконання job (repair, повтори, cancel, interrupt, resume); Т7c агрегація в кліпи + `analysis_stale` (§2, §9, §11; T-035, T-038).
- Т8 API `/v1/analysis-runs` (§10). Т10 `scripts/analysis_probe.py` — живий probe запускає власник.
- Д1 main allowlist `analysis.*` + shared types; Д2 панель «Що шукати», підсумок, прогрес, кандидати; Д3 Electron e2e + E2E-04 (§12).
- Т1, Т2, Т3a, Т3b, Т4 — паралельно; далі Т5/Т6, Т7/Т8, desktop послідовно.

## M2 — прийнято

Т15, Т12a, Т13, Т18a, Т17a, Т18b, Т24 (bridge за ADR 024), Т17b, Т19 (drop zone), Т14 (повтор фрагмента), Т16 (панель експорту без перекриття, ADR 025), мікроправка `supportFetchAPI`, Т25 (тексти «Джерел»), Т26 (перейменування проєкту), Т27 (аудит розкладки нових екранів). Друге рев'ю пройдено, high-зауважень немає. Закриття: backend pytest 1019 passed, ruff/mypy чисто; apps/desktop e2e 37 passed; `npm run qa:e2e` E2E-01..03 passed, E2E-04 червоний до M4 (ADR 023).

## Далі

**Відновлено 2026-11-15.** Рев'ю Т1–Т6 пройдено (high немає). Т2c, Т5b (виправлення за рев'ю) і Т7a (облік бюджету, ADR 031) прийнято. Т7c (settings і credentials API, 429 для бюджету) прийнято. Рев'ю Т7a+Т7c пройдено (high немає). Т7b1 (сервіс STT-run, ADR 032), Т7b1a, Т7d (правки за рев'ю) і Т7b2 (API, job, cancel, resume) прийнято. Рев'ю Т7b1–Т7b2+Т7d пройдено (high немає); виправлення Т7e1, Т7e2 (ADR 035) і Т8 (ключ у main, екран «Налаштування» AI, ADR 034) прийнято. Рев'ю Т7e1+Т7e2+Т8 пройдено (high немає), виправлення Т7f і Т8b прийнято. Т9b, Т9c прийнято (ADR 036), їх рев'ю пройдено (high немає). 2026-11-16: Т9a (ADR 037), Т9g1, Т9g2 прийнято; `httpcore2` оголошено в pyproject. Рев'ю Т9a+Т9g пройдено (high/medium немає); Т9d1 (op, ADR 038) прийнято. Т9g3, Т9d2 (UI розпізнавання), Т9d3 прийнято; рев'ю Т9d1+Т9d2 пройдено (high 0, medium 1). Т9e1 (op транскриптів, `budget_usd` у jobs.resume) прийнято. Т9d4a–Т9d4c прийнято (рев'ю Т9d2 закрито). Т9e2 (панель транскрипції), Т9e3 (виправлення) прийнято. Т9f1 (канал імпорту) прийнято. Т9f2 прийнято. Рев'ю Т9e2–Т9f2 пройдено (high 0, medium 2); Т9h1 (правки renderer) прийнято. Т9h2 (правки імпорту в main) прийнято. Т9h3 (гонка фокусу) прийнято. Рев'ю Т9h1–Т9h3 пройдено (high 0, medium 3); Т9h4 (гонка джерела в панелі, прокрутка пошуку), Т9h5 (main/shared: тести, формат ліміту) прийнято. Рев'ю Т9h4–Т9h5 пройдено (high 0, medium 1). Повний backend pytest дав 25 падінь на Matroska (ffprobe 8.1 інколи повертає 0 пакетів після пошуку до кінця); Т9h7 (повторне читання без `-read_intervals`) прийнято. Т9h6 прийнято, рев'ю Т9h6–Т9h7 пройдено (high немає). Фінальні гейти M3 (2026-11-17): backend pytest 2474 passed; qa:acceptance 13/17 (4 очікувані M4: T-031 ×2, T-033, T-035); desktop e2e 46 passed; qa:e2e 3/4 (E2E-04 — M4, ADR 023). Не запускалось: живе STT (Т5a), живий `GET /key`, чиста машина. M3 закомічено 2026-11-17 за згодою власника (з dev-CSP фіксом; `.agents/`, `.codex/`, `AGENTS.md` поза git). Далі: M4 (відбір моментів).

1. **M3** — реальні STT adapters OpenRouter (docs/02, docs/05, skill cf-transcription). Live probe обох моделей виконано 2026-11-15 (docs/PROGRESS.md).
   План M3 (backend можна паралельно, desktop — послідовно):
   - Т1 `domain/chunking.py` — planner core/upload, adaptive split, payload estimate (T-016). Прийнято.
   - Т3 `domain/transcript_import.py` — SRT/VTT/normalized JSON, bounds (T-023, T-024). Прийнято.
   - Т2 `domain/transcript_merge.py` — chunk→source, dedupe, ownership, coverage (T-017..T-022). Прийнято (ADR 028).
   - Т2b: слово нульової тривалості (живий Gemini) → +1 мс із діагностикою; сегменти зі слів у word-режимі. Прийнято.
   - Т4 storage r0003 (transcripts, runs, chunks, words, segments, spend) + `POST /v1/transcripts/import`, `GET /v1/transcripts/{id}`. Прийнято.
   - Т5 OpenRouter STT adapter за ADR 029 (одна спроба, класифікація помилок, валідація 200). Прийнято.
   - Т5a capability probe (інструмент прийнято; живий запуск — за власником): чи передається verbatim у `provider.options.azure`, запит без мови (автовизначення) і `es`, фрагмент матеріалу власника англійською/іспанською (FR-013; українська не обов'язкова — вказівка власника). Після Т6, запускає власник.
   - Т6 витяг аудіо FFmpeg (mono 16 kHz PCM WAV за ADR 028, canonical zero, тиша на початку; T-010). Прийнято.
   - Т7a spend ledger (T-048). Прийнято (ADR 031). Т7c `POST/DELETE /v1/runtime/credentials`, `GET/PATCH /v1/settings`, 429 для BUDGET_EXCEEDED. Т7b run orchestration: job, concurrency 2, retry/Retry-After, 413 split, unknown_outcome, cache, resume (T-025..T-030).
   - Т8 desktop: ключ у main через safeStorage (session-only fallback), екран «Налаштування» AI.
   - Т9 розбито:
     - Т9a backend: оцінка перед запуском (FR-049), без повторної оплати готового транскрипту (FR-050: 409 TRANSCRIPT_ALREADY_EXISTS {transcript_id, run_id}, якщо не `force:true`), `GET /v1/transcription-runs?source_id`. Прийнято (ADR 037).
     - Т9b backend: `POST /v1/transcripts/{id}/corrections`, нова ревізія без зміни часу (FR-020). Прийнято (ADR 036).
     - Т9c backend: `POST /v1/runtime/credentials/check` через OpenRouter `GET /key` (FR-048). Прийнято (ADR 036).
   - Виправлення за рев'ю Т7e1/Т7e2/Т8 (medium 3, low 9): Т8b desktop і Т7f backend. Прийнято.
   - Т9g backend: виправлення за рев'ю Т9b+Т9c (docs/PROGRESS.md, запис «Рев'ю Т9b+Т9c»): загальний дедлайн 15 с і на фазу заголовків (тест із повільними заголовками); коментарі «free of charge» у `openrouter_key.py` узгодити з ADR 036; зафіксувати ConnectTimeout → KEY_CHECK_NOT_SENT тестом і в ADR; `trust_env=False` в обох httpx-клієнтах; `Accept-Encoding: identity`; тести: збереження ключа не шле запит, тіло check ігнорується, межа 64 КіБ, та сама правка створює ревізію, `details.fields`; флак паралельних тестів (warnings). Прийнято (Т9g1, Т9g2).
     - Т9d desktop: запуск транскрипції (підсумок, ліміт), прогрес частин, скасування, partial/unknown_outcome, resume.
     - Т9e desktop: панель транскрипції (пошук, click-to-seek, виправлення, пропуски покриття).
     - Т9f desktop: імпорт SRT/VTT, кнопка «Перевірити ключ».

## Відкриті питання (не блокують)

- T-010 (ненульовий start PTS) емпірично не перевірено.
- Наживо не перевірено: 409 при автозбереженні; частковий збій, повтор, скасування і «Відкрити папку» в експорті.
- Untracked `.agents/`, `.codex/`, `AGENTS.md` — рішення власника.
- Повторне використання STT-транскрипту для того самого файлу в іншому джерелі (FR-050: ключ із хешем файлу; ADR 037, доповнення).
- Блок «Додатково» в панелі «Що шукати» (docs/03 §6: мова, STT model, analysis model, бюджет на запуск) у M4 не зроблено. Модель аналізу й ліміт беруться з «Налаштувань», а для помилок є кнопка «Відкрити Налаштування» (ADR 041 §23 (4)). Рев'ю M4, low 8.
