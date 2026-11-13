# Первинні джерела та рішення проєкту

Документацію нижче перевірено 12 листопада 2026 року під час підготовки пакета. У M0 перевірити її знову та виконати capability smoke на реальному акаунті. Наявність поля в документації не є результатом нашого live-тесту.

| Джерело | Що підтверджує |
|---|---|
| https://openrouter.ai/microsoft/mai-transcribe-2 | Model ID, STT, часові позначки та provider options |
| https://openrouter.ai/google/gemini-3.5-transcribe | Model ID, word timing, обмеження тривалості |
| https://openrouter.ai/docs/guides/overview/multimodal/stt | Endpoint, формат запиту, verbose output та transport limits |
| https://learn.microsoft.com/en-us/azure/ai-services/speech-service/mai-transcribe | Дослівний режим, timing, словникові підказки |
| https://openrouter.ai/docs/guides/features/structured-outputs | JSON schema output для сумісних моделей аналізу |
| https://ffmpeg.org/ffmpeg.html | Seek, transcoding, stream selection; stream copy не гарантує довільну точну межу |
| https://code.claude.com/docs/en/skills | Формат SKILL.md, проєктне розміщення, виклик skills |
| https://code.claude.com/docs/en/memory | CLAUDE.md і розподіл постійних/спеціалізованих інструкцій |
| https://www.electronjs.org/docs/latest/tutorial/security/ | Isolation, sandbox, IPC і navigation controls |
| https://www.electronjs.org/docs/latest/api/safe-storage | OS encryption і платформні обмеження |
| https://playwright.dev/docs/api/class-electron | Автоматизація Electron і stubbing native dialogs; підтримка позначена experimental |

Числові пороги якості, тривалості windows, UX-розміри, performance targets, бюджетна логіка, архітектура й tests — власні проєктні вимоги. Вони не є обіцянками вендорів. Поточний пакет не містить результатів live API або навчання моделі. Версії dependencies фіксуються під час M0; тарифи не зашиваються як вічні константи.
