# Стан реалізації

Оновлено 13 листопада 2026 року.

Підготовлено пакет вимог, інструкцій, контрактів, фікстур і тестів. Продукт не реалізований. Поточні перевірки пакета наведені у VALIDATION_REPORT.md.

| Milestone | Статус | Підстава |
|---|---|---|
| M0 | Виконано, live STT — NOT RUN (очікує власника) | Toolchain, backend/desktop shell, sidecar packaging smoke; див. запис 2026-11-13 нижче |
| M1 | Не розпочато | Backend та export engine ще не створені |
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
