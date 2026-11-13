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
