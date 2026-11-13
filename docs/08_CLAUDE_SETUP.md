# Підготовка Claude Code

## Середовище розробки

Основний сценарій — Claude Code у локальній папці цього проєкту на Windows 11 x64. Потрібні Git, Node.js, Python, FFmpeg/FFprobe і доступ до package registries. Цільові базові версії Node 24 та Python 3.12 є рішенням проєкту; у M0 розробник перевіряє сумісність усіх залежностей і фіксує точні версії. GPU не є обов'язковим. Для довгих файлів потрібне місце для оригіналу, proxy, cache та exports; необхідний обсяг показується перед обробкою, а не визначається одним універсальним числом.

API-ключ OpenRouter потрібен лише для live AI. Витрати Claude Code і застосунку обліковуються окремо. До live smoke розробка та автоматизовані тести використовують fake provider. Не вставляти секрет у START_HERE, CLAUDE.md, тестові файли або Git.

## Як передати роботу

1. Розпакувати весь пакет, включно з `.claude`.
2. Відкрити його корінь у Claude Code. Перевірити, що прочитано CLAUDE.md.
3. Вставити START_HERE.md як стартове повідомлення.
4. Попросити виконувати M0–M8 з оновленням PROGRESS.md, не зупиняючись після одного UI-макета.
5. Для наступної сесії використати текст: «Прочитай CLAUDE.md та docs/PROGRESS.md. Перевір фактичний стан коду й тестів. Продовжуй із першого незавершеного milestone відповідно до docs/02_IMPLEMENTATION_PLAN.md».

## Проєктні skills

Це підготовлені для цього проєкту інструкції, а не сторонні плагіни чи встановлені ChatGPT skills. Вони розташовані в стандартній для локального Claude Code структурі `.claude/skills/name/SKILL.md`. Їх можна викликати як `/cf-media-timing` тощо. Якщо конкретне середовище Claude не підхоплює skills, явно доручити прочитати відповідний файл. Локальні, cloud і Cowork сесії можуть завантажувати різні джерела skills; пакет орієнтований на локальну сесію.

| Skill | Коли застосовувати | Обов'язковий результат |
|---|---|---|
| cf-delivery-planner | Початок milestone, відновлення роботи | Перелік задач, залежностей і критеріїв виходу |
| cf-media-timing | FFmpeg, audio/video timestamps, export | Реальні content-boundary та A/V тести |
| cf-transcription | STT, chunking, merge, API errors | Нормалізована транскрипція й recovery tests |
| cf-highlight-selection | Категорії, evidence, ranking | Валідація model output і доказовий відбір |
| cf-desktop-ux | Екрани, плеєр, review flow | Доступний UI і E2E без ручних монтажних знань |
| cf-local-security | IPC, paths, credentials, subprocess | Перевірені локальні межі доступу |
| cf-quality-gates | Тести, regressions, release | Точний звіт passed/failed/not-run |

Skills не замінюють залежності, доступ до API або перевірки. Додаткові сторонні skills/MCP не є необхідними для MVP. Доступ до browser automation корисний для UI, але Playwright уже передбачений у тестовому стеку. Не вмикати необмежені permission bypass або невідомі install scripts заради зручності.

## Початкові команди пакета

```powershell
npm run qa:pack
npm run qa:syntax
npm run fixtures
npm run qa:fixtures
```

Ці команди використовують Node.js та локальний FFmpeg і не потребують npm install. Після реалізації backend встановити його залежності за створеним lock-файлом і виконати `npm run qa:acceptance`. Для E2E у M0 встановити й зафіксувати `@playwright/test`; пакет не вигадує lock-файл для ще не створеного застосунку. Запуск `npm run qa:e2e` потребує зібраного Electron entry, який задається через `CLIPFORGE_ELECTRON_ENTRY`, та встановленого Electron executable через `CLIPFORGE_ELECTRON_EXECUTABLE`.

## Організація роботи

CLAUDE.md містить інваріанти, а спеціалізовані процедури — у skills. Не додавати повне ТЗ до постійного контексту через десятки imports. Читати потрібні документи за етапом. Для кожного milestone записувати змінені FR, команди тестів і докази. Після перерви перевіряти код, а не лише довіряти старому PROGRESS.

Паралельна робота агентів не потрібна для старту. Якщо власник окремо її дозволить, розподіляти за незалежними модулями й не доручати двом агентам змінювати той самий API-контракт без спільного рішення.
