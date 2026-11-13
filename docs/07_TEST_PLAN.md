# Тестова стратегія та критерії релізу

## 1 Рівні перевірки

Пакет містить реальні файли black-box acceptance-тестів та UI-тестів для майбутньої програми. Вони мають запрацювати після відповідних milestones. Окремий набір tests/pack перевіряє поточний handoff. Проходження tests/pack не є перевіркою реалізації продукту.

1. Unit: Python pytest для часу, interval validation, chunk planner, merge, scoring, state transitions, filename safety, cost ledger. Hypothesis для bounds/monotonicity/round trips.
2. Adapter contract: HTTPX fake transport / локальний OpenRouter stub; порожні/неповні fields, offsets, 429, 401, 402,413,500, timeouts, malformed JSON, unknown IDs.
3. Integration: реальні FFmpeg/FFprobe з короткими synthetic fixtures, SQLite migrations/transactions, cancellation/resume.
4. Acceptance: надані Node tests через реальний local HTTP API; оригінали і фактичні MP4 перевіряються незалежно від внутрішнього коду.
5. UI: Vitest/Testing Library для компонентів; Playwright Electron для user flows. Native dialogs stubbing виконується у Electron main; не покладатися на OS mouse automation.
6. Release: packaged build на чистій Windows, довгий реальний source, live-provider smoke, оцінка власника.

## 2 Написані тести і запуск

`npm run qa:pack` — комплектність пакета, перевірка ключів схем/прикладів, temporal vectors, skills frontmatter та поведінки fake provider. Це не повний JSON Schema validator і не тести бізнес-логіки майбутнього застосунку. Повну schema validation необхідно додати в M3/M4. Зовнішня мережа/API не потрібні; fake використовує loopback.

`npm run fixtures` — створює короткі відеофікстури реальним FFmpeg у test-results/fixtures. Оригінали користувача не потрібні. Для 6-hour tiny source виконати `node tools/generate-fixtures.mjs --long`.

`npm run qa:syntax` — перевіряє JS test/tool syntax.

`npm run qa:fixtures` — після генерації перевіряє справжні metadata/PTS у synthetic media: sparse keyframes, no audio, stream indices/languages, offset +1.5с, VFR, Unicode bytes. Це перевірка тестових вхідних файлів, а не готового експорту застосунку.

`npm run qa:acceptance` — після M1 і наступних milestones запускає fake OpenRouter, Python sidecar і HTTP suite; потрібен Python із встановленим backend. За відсутності реалізації завершується з помилкою, а не успішним skip. Частини suite можуть бути червоними до реалізації відповідних етапів.

`npm run qa:e2e` — після build Electron і встановлення pinned @playwright/test. Entry points та env описані в test config. Tests запускаються у тимчасовому user data directory. Не підключати production workspace.

## 3 Обов'язкова матриця сценаріїв

| ID | Сценарій | Очікування | Рівень / milestone |
|---|---|---|---|
| T-001 | Timecode з годинами/мілісекундами | 05:59:59.125 →21599125; roundtrip | Unit M1 |
| T-002 | Negative, NaN, end≤start, end>D | Reject без subprocess | Unit/API M1 |
| T-003 | Non-keyframe start | Перший output frame належить потрібній сцені | FFmpeg M1 |
| T-004 | Кінець source | end=D дозволено, end>D відхилено | API M1 |
| T-005 | Unicode/spaces/reserved filename | Без shell execution, читабельний output | Integration M1 |
| T-006 | No-audio video | Manual export успішний, STT disabled | API/UI M1/M2 |
| T-007 | Multiple audio tracks | Вибрана доріжка, правильний count/language | Integration M1 |
| T-008 | Audio offset +1.5с | A/V offset збережено, drift≤50мс | Integration M1 |
| T-009 | Variable FPS | Межі за PTS, не average FPS | Integration M1 |
| T-010 | Nonzero start PTS | Canonical zero однаковий у UI/STT/export | Integration M1/M3 |
| T-011 | Source changed/moved | Block stale export; relink hash check | API M1/M6 |
| T-012 | Existing export filename | Немає silent overwrite | API M1 |
| T-013 | Unapproved clip export | 409, жодного render | API M1 |
| T-014 | Clip changed during render | Snapshot незмінний, output stale якщо треба | Integration M6 |
| T-015 | Stale expected_revision | 409, нові дані збережені | API M1 |
| T-016 | Chunk core/context | Exact coverage без gaps/double counting | Unit M3 |
| T-017 | Word timestamps chunk→source | Додається upload offset рівно один раз | Unit M3 |
| T-018 | Duplicate overlap words | Один occurrence на стику | Unit M3 |
| T-019 | Repeated words elsewhere | Справжній повтор не видалений | Unit M3 |
| T-020 | Speaker labels per chunks | Немає хибного cross-chunk identity | Unit M3 |
| T-021 | Silence / empty success | Coverage success, no hallucinated candidates | Adapter M3/M4 |
| T-022 | Missing word timestamps | Segment fallback позначений, не фальшива точність | Adapter M3 |
| T-023 | SRT/VTT BOM/multiline/hours | Correct import and seek | Unit/API M3 |
| T-024 | Imported transcript outside duration | 422 або explicit correction workflow | API M3 |
| T-025 | Provider 429 / Retry-After | Bounded wait і retry, без busy loop | Adapter M3 |
| T-026 | 401/402 | Stop, actionable settings/funds error | Adapter/UI M3 |
| T-027 | 413 | Adaptive core split, stable total coverage | Adapter M3 |
| T-028 | Paid timeout після send | unknown_outcome, no silent duplicate spend | Integration M3/M6 |
| T-029 | Partial transcript | Coverage видимий, full analysis blocked default | API/UI M3 |
| T-030 | Retry failed chunks | Completed cache не надсилається повторно | Integration M3 |
| T-031 | Categories OR/all/empty | OR filtering; empty rejected | Unit/API/UI M4 |
| T-032 | Custom instruction injection | Data only, unchanged schema/path rules | Adapter M4 |
| T-033 | Unknown/reversed evidence IDs | Reject, no invented clip time | API M4 |
| T-034 | Malformed/oversize model JSON | Bounded repair then fail | Adapter M4 |
| T-035 | No qualifying scenes | Empty state, no quota filling | API/UI M4 |
| T-036 | Multi-category same event | Один clip, кілька labels | Unit M4 |
| T-037 | Dedupe different times | Не зливати однакові слова у різні моменти | Unit M4 |
| T-038 | Reanalysis reviewed clips | Approval/rejection/manual bounds збережені | API M4/M5 |
| T-039 | Global/local subtitle times | SRT starts≥0, ends≤clip duration | Integration M1 |
| T-040 | API key in export/logs | Key відсутній у scan | Security M6 |
| T-041 | Unauthorized local API | 401 до читання даних | Security M0/M6 |
| T-042 | Arbitrary URL/path traversal | Reject; registered IDs only for media | Security M6 |
| T-043 | XSS transcript/title | Plain text, no script execution | UI/Security M5/M6 |
| T-044 | Cleanup reparse/symlink escape | Не торкається external files | Integration M6 |
| T-045 | Disk full/permission denied | No verified output, useful retry | Integration M6 |
| T-046 | Cancel owned FFmpeg | Тільки own process tree завершується | Integration M6 |
| T-047 | Crash/restart | Lease recovery, validated checkpoints | Integration M6 |
| T-048 | Budget concurrency | Atomic reservations, no knowingly overscheduled requests | Unit M3/M6 |
| T-049 | CSV formula injection | Text cells cannot execute formulas | Unit M1 |
| T-050 | Keyboard in input | I/O/A/R не перехоплюють набір | UI M2/M5 |
| T-051 | Minimum window/scaling | Без inaccessible actions/overflows | Visual M5 |
| T-052 | 6-hour tiny fixture | Correct near-end seek/time, bounded memory | Integration M7 |
| T-053 | Real long 1080p | Performance measured with hardware manifest | Performance M7 |
| T-054 | Live STT models | Реальний output і timing validation | Live M0/M7 |
| T-055 | Held-out highlight quality | Реальні reviewer metrics | Pilot M7 |
| T-056 | Clean Windows package | No dev Python/Node required | Release M8 |

Матриця є мінімумом. Acceptance-файли покривають підмножину, яку зазначено у VALIDATION_REPORT; Claude додає решту tests під час реалізації. Заборонено вважати рядок матриці виконаним лише через наявність його опису.

## 4 Coverage та quality gates

Для domain time/merge/selection/state modules: ≥90% branch coverage, для backend загалом ≥80%; frontend domain/state ≥80%. Ці пороги не заміняють content-boundary, A/V і release checks. Exclude generated schemas/migrations дозволено лише явно в config. Немає blanket skip/xfail для P0 або placeholder tests, які завжди pass.

Кожен milestone: lint, typecheck, релевантні unit/integration, regression tests; M5+: E2E main flow. Release: повна suite, Windows smoke, original hash preservation, credential scan, tool versions, license inventory. Зберігати JUnit/coverage, Playwright report і redacted FFmpeg stderr artifacts.

## 5 CI

Windows runner є обов'язковим для integration/Electron/package. Linux можна додати для pure domain tests, але він не заміняє Windows. Pull request checks: formatting/lint/types, unit, contracts, short real-media tests, E2E fake AI. Long 6h + packaging — окремий job/manual trigger перед release. Live AI — explicit manual job зі scoped secret і ceiling, не автоматичний PR check.

Pin checkout/setup actions за перевіреними refs/SHAs під час реалізації, встановлювати deps із lock files. FFmpeg binary має відомий provenance/checksum. CI не завантажує приватні source videos. Тестові фікстури генеруються локально скриптом і не потребують copyrighted material.

## 6 Як звітувати

Окремі поля: test suite, command, environment/tool versions, passed/failed/skipped, report path, missing gates. «Tests pass» допустиме лише з точним переліком виконаних suites. Fake API pass не є live model verification; syntax check не є execution; screenshot не є функціональним тестом; tiny6h fixture не є real1080p performance.
