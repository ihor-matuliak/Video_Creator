# Известные ловушки

- Git на цій машині має core.autocrlf=true. Без .gitattributes (eol=lf) SKILL.md отримують CRLF і tests/pack падає на `startsWith('---\n')`. Не прибирати .gitattributes.
- uv встановлено через winget, Python 3.12 — через `uv python install`; у нових терміналах uv є в PATH, у старій сесії треба перезапустити shell.
