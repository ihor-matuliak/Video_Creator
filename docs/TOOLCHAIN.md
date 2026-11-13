# Toolchain і середовище розробки

Зафіксовано в M0, 13 листопада 2026 року. Точні версії залежностей — у `uv.lock` і `package-lock.json`; тут лише опорні значення та їх походження.

## Машина розробки

| Параметр | Значення |
|---|---|
| ОС | Windows 11 Education 10.0.26200, x64 (AMD64) |
| CPU | AMD Ryzen 5 5600, 6 ядер / 12 потоків |
| RAM | 31,9 ГБ |
| GPU | NVIDIA GeForce RTX 4060 (не обов'язковий для MVP) |
| Диск C: | 209 ГБ вільно на момент M0 |

## Інструменти

| Інструмент | Версія | Джерело |
|---|---|---|
| Git | 2.53.0.windows.1 | системний |
| Node.js / npm | 24.12.0 / 11.6.2 | системний; reference target Node 24 |
| Python | 3.12.16 | `uv python install`, профіль користувача (ADR 006) |
| uv | 0.13.8 | winget `astral-sh.uv` |
| FFmpeg / FFprobe | 8.1 full_build | winget `Gyan.FFmpeg`, www.gyan.dev |

## FFmpeg: provenance і ліцензія

- Збірка: `ffmpeg version 8.1-full_build-www.gyan.dev`, gcc 15.2.0 (MSYS2), статична.
- Конфігурація містить `--enable-gpl --enable-version3 --enable-libx264`; отже бінарник розповсюджується під GPLv3 (файл `LICENSE` у теці збірки — GNU GPL v3).
- Енкодери, потрібні продукту, присутні: `libx264` (H.264), `aac` (нативний AAC).
- SHA-256 (локальна копія winget):
  - `ffmpeg.exe` — `d1e2a156261ecc675081943197a85f08f2868784a0af499171ede89353edad31`
  - `ffprobe.exe` — `70872c3ffbc43d0b2c570f9837f54d6e9a832f4ca25463e9735b6a3ec0621478`
- Рішення щодо ліцензії: ADR 008 (лише особисте використання власником; GPL-збірка допустима, ліцензії все одно потрапляють в inventory M8).
- Шлях до бінарників у M1 задається конфігурацією backend, а не пошуком у PATH під час експорту; у M8 бінарники пакуються разом із sidecar, і їх checksum перевіряється.

## Backend (Python)

ADR 012, 015. Віртуальний uv workspace у корені, пакет `clipforge` у `apps/backend`.

| Пакет | Версія |
|---|---|
| fastapi | 0.162.6 |
| starlette | 1.10.0 |
| uvicorn | 0.54.0 |
| pydantic | 2.14.3 |
| httpx2 | 2.16.0 |
| sqlalchemy | 2.1.8 |
| alembic | 1.20.1 |
| dev: pytest / pytest-asyncio / hypothesis | 9.2.0 / 1.5.0 / 6.170.15 |
| dev: ruff / mypy / pyinstaller | 0.17.5 / 2.4.1 / 6.22.3 |

## Desktop (Node)

ADR 011. npm workspaces, єдиний `package-lock.json` у корені.

| Пакет | Версія |
|---|---|
| electron | 45.3.2 |
| electron-vite / vite | 6.0.0 / 8.3.8 |
| react / react-dom | 19.3.1 |
| @vitejs/plugin-react | 6.3.0 |
| typescript | 6.0.3 (не 7.x — typescript-eslint) |
| eslint / typescript-eslint | 10.14.0 / 8.72.2 |
| vitest | 5.1.0 |
| @testing-library/react | 16.3.3 |
| jsdom | 29.1.1 (30+ вимагає Node ≥24.15) |
| @playwright/test (корінь) | 1.65.0 |
| Інші dev-pins | див. ADR 016 |

## OpenRouter STT: що каже документація (перечитано 13.11.2026)

Джерела: docs/09_SOURCES.md. Live-перевірка — окремо, див. PROGRESS.

- `POST https://openrouter.ai/api/v1/audio/transcriptions`; JSON `input_audio{data(base64 без data URI),format}` або multipart `file` (до 25 МБ).
- `response_format: "verbose_json"` + `timestamp_granularities: ["segment","word"]`; час `start`/`end` у **секундах** (float), не мс — нормалізатор M3 переводить у цілі мс.
- Upstream-провайдери обривають запит через ~60 с обробки; рекомендовано ділити довші записи. Це обмежує розмір chunk у M3 сильніше за 25 МБ.
- Опції MAI: документований шлях `provider.options.azure.*` (приклад — diarization); `enhancedMode`/`verbatim` у документації OpenRouter **не описані**. Невідомі ключі можуть бути мовчки відкинуті — перевіряти лише live.
- Ідентифікатор запиту — заголовок `X-Generation-Id`. `Retry-After` не задокументований.
- Ціна MAI-Transcribe 2: $0,10/год аудіо (Azure); Gemini 3.5 Transcribe — токенна ($2/M input, $12/M output). Фактичну вартість брати з `usage.cost`.

### Live-спроба 13.11.2026 18:18 UTC

`stt_probe.py` для microsoft/mai-transcribe-2 і google/gemini-3.5-transcribe: обидва HTTP 402 за 0,4–0,65 с, `generation_id` відсутній (нічого не списано). Ключ прийнято (не 401). Причина — мінімальний баланс $0,50 для аудіо-запитів; тіло помилки має `metadata.limit_source="openrouter_credits"` і `remedy_hint`. Формат verbose_json і timing ще не підтверджені live.
