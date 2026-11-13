# Известные ловушки

- Git на цій машині має core.autocrlf=true. Без .gitattributes (eol=lf) SKILL.md отримують CRLF і tests/pack падає на `startsWith('---\n')`. Не прибирати .gitattributes.
- uv встановлено через winget, Python 3.12 — через `uv python install`; у нових терміналах uv є в PATH, у старій сесії треба перезапустити shell.
- Git Bash (інструмент Bash агентів) не бачить uv у PATH. Викликати повним шляхом: `/c/Users/Ihor/AppData/Local/Microsoft/WinGet/Packages/astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe/uv.exe`. У PowerShell — оновити `$env:Path` з Machine+User.
- TypeScript 7.x несумісний з typescript-eslint 8.72 (peer <6.1.0) — не оновлювати TS без перевірки (ADR 011).
- jsdom закріплено на 29.1.1: jsdom 30+/31 вимагає Node ^24.15, а на машині Node 24.12.0. Оновлювати разом із Node.
- Starlette 1.x TestClient вимагає `httpx2`; зі звичайним `httpx` падає StarletteDeprecationWarning (у pytest filterwarnings=error). Див. ADR 015.
- `python` у Git Bash — це аліас WindowsApps (Python 3.14), який зависає в очікуванні stdin. Використовувати `uv run …` або `.venv/Scripts/python.exe`.
- У тестах із `from __future__ import annotations` модель тіла запиту FastAPI оголошувати на рівні модуля: локальний клас FastAPI мовчки сприймає як query-параметр.
- Конфіг ruff/mypy/pytest лежить у кореневому pyproject.toml; гейти запускати з кореня репозиторію. Ruff RUF001–003 вимкнені свідомо (кирилиця в повідомленнях).
- PyInstaller-бандл sidecar (apps/backend/packaging) поки не містить SQLAlchemy/Alembic, бо код їх ще не імпортує. Після появи БД і міграцій (M1) повторити `build_sidecar.py` + `smoke_sidecar.py`: hidden imports і файли міграцій. Попередження `Hidden import "tzdata" not found` — якщо з'явиться zoneinfo, додати tzdata як залежність.
- Mypy-гейт запускати на весь backend: `uv run mypy --strict apps/backend` (не лише clipforge), інакше помилки в tests/ пропускаються.
- Після `npm ci` бінарник Electron може не завантажитися (немає `node_modules/electron/dist`). Виконати `node node_modules/electron/install.js` (бере zip із кешу `%LOCALAPPDATA%\electron\Cache` або з мережі).
- `.venv\Scripts\python.exe` на Windows — launcher: з'являються два процеси python (launcher і інтерпретатор). `child.kill()` на launcher завершує й дочірній (перевірено) — `taskkill /T` не потрібен.
- Electron main шукає Python відносно `out/main` (лише нерозпакована dev-розкладка). У M8 замінити на PyInstaller-бандл sidecar.
- У test mode `CLIPFORGE_*` env передаються sidecar як є (крім CLIPFORGE_SESSION_TOKEN); у production усі `CLIPFORGE_*` вирізаються з env дочірнього процесу.
- OpenRouter відхиляє будь-який аудіо-запит STT кодом 402, якщо баланс < $0,50 (перевірено live 13.11.2026 для обох STT-моделей; generation_id = null, нічого не списано). Тіло: `{"error":{"code":402,"message":"This request requires at least $0.50 in balance for audio","metadata":{"limit_source":"openrouter_credits","remedy_hint":"…"}}}`. M3: 402 → зупинити run, показати українською потребу поповнити баланс щонайменше до $0,50; не повторювати.
