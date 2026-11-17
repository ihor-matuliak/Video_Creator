# Архітектура та контракти

## 1 Компоненти і межі

Electron main керує вікнами, native dialogs, OS secrets, запуском Python sidecar та allowlisted IPC. React renderer керує UI, але не запускає FFmpeg, не читає довільні шляхи й не зберігає секрети. Python FastAPI sidecar реалізує domain services, SQLite, jobs, FFmpeg і OpenRouter adapters. Окремий background worker у тому самому Python application забезпечує серіалізацію jobs, leases і cancellation.

Sidecar слухає тільки 127.0.0.1 на випадковому доступному порту. Main генерує випадковий session bearer token і передає конфігурацію через stdin pipe; не argv. Main робить authenticated HTTP calls і повертає renderer лише типізовані результати. Renderer не отримує bearer token. Native picked path від main є входом import API. CORS не відкривати зовнішнім origin; Host header перевіряти. Не віддавати довільний файл за raw path у URL.

Playback: custom app-media protocol main process приймає registered source/proxy ID, перевіряє scope і проксіює range request у sidecar. Немає привілейованої filesystem API у renderer. Перевірити 206/416 і seeking на довгих файлах. Статуси jobs оновлюються polling 1с лише для активних view; API не повертає весь transcript на кожен poll.

## 2 Організація майбутнього коду

| Шлях | Вміст |
|---|---|
| apps/desktop/src/main | lifecycle, IPC, dialogs, secrets, sidecar, media protocol |
| apps/desktop/src/preload | вузький typed bridge |
| apps/desktop/src/renderer | React screens, components, state, accessible controls |
| apps/backend/clipforge/domain | time/interval, categories, selection validation, state transitions |
| apps/backend/clipforge/services | orchestration, source registration, export, transcript, analysis |
| apps/backend/clipforge/adapters | ffmpeg, OpenRouter, filesystem, clock |
| apps/backend/clipforge/storage | models, migrations, repositories |
| apps/backend/clipforge/jobs | durable queue, leases, retry/resume/cancel |
| apps/backend/clipforge/api | routes, request/response schemas, auth |
| tests | provided acceptance/E2E + додані unit/integration |

Domain не залежить від FastAPI/Electron. Provider і FFmpeg adapter мають injectable boundary для тестів, але production не може підключити fake непомітно.

## 3 Дані

Project(id,name,created_at,updated_at,archived_at).
Source(id,project_id,path,display_name,size,mtime,quick_fingerprint,sha256,hash_status,duration_ms,video_stream_index,audio_streams,canonical_origin,probe_json,availability,created_at).
TranscriptRun(id,source_id,revision,model,provider,options_hash,status,coverage,usage,created_at).
TranscriptChunk(id,run_id,core_start_ms,core_end_ms,upload_start_ms,upload_end_ms,state,attempts,request_id,response_path,error_code,reserved_micro_usd,actual_micro_usd_nullable); суми — цілі мікродолари (r0006, ADR 031).
TranscriptWord(id,run_id,chunk_id,ordinal,start_ms,end_ms,text,speaker_local,confidence_nullable).
TranscriptSegment(id,run_id,start_ms,end_ms,text,word_ids,timing_level).
AnalysisRun(id,source_id,transcript_revision,profile_json,model,prompt_version,schema_version,status,usage).
Clip(id,source_id,analysis_run_id_nullable,revision,title,start_ms,end_ms,recommended_start_ms,recommended_end_ms,evidence_start_ms_nullable,evidence_end_ms_nullable,categories,evidence_type,evidence_ids,reason,scores,rank,visual_review_required,review_status,created_at,updated_at).
ExportBatch(id,project_id,output_dir,profile_json,status,created_at).
ExportItem(id,batch_id,clip_id,clip_revision,snapshot_json,state,temp_path,final_path,observed_json,error_code).
Job(id,type,state,progress_json,lease_owner,lease_expires_at,checkpoint_json,created_at,updated_at).
SpendEntry(id,run_id,request_id,state,reserved_micro_usd,actual_micro_usd_nullable,model,price_snapshot); суми — цілі мікродолари (r0004, ADR 031).

Час у домені — integer milliseconds; UTC timestamps з ISO8601 для журналів. SQLite constraints: source duration>0; 0≤clip.start<clip.end; app validator також перевіряє end≤source duration. JSON arrays зберігати з schema version. Evidence IDs належать одному transcript revision. Large raw provider response — app-owned file, посилання в DB, без ключів.

## 4 API правила

Base /v1. JSON UTF-8. Authorization: Bearer session token на всіх routes, включно з health. Успішна mutation response — entity або job envelope, не голий bool. Error shape: {"error":{"code":"INVALID_INTERVAL","message":"Кінець має бути пізніше початку","details":{},"retryable":false}}.

HTTP 400 — некоректна доменна дія; 401 — token; 404 — ID; 409 — stale revision/state conflict; 422 — invalid schema/bounds; 429 — local rate/budget condition із domain code; 503 — unavailable dependency. Не повертати raw exception/секрети у response.

### Основні маршрути

| Метод і шлях | Тіло / параметри | Відповідь |
|---|---|---|
| GET /v1/health | немає | {status:"ok",mode:"test" або "production",version} |
| POST /v1/projects | {name} | 201 {id,name} |
| GET /v1/projects | limit,cursor,include_archived,q | {items,next_cursor} |
| PATCH /v1/projects/{id} | {name?,archived?} | 200 project (ADR 020) |
| POST /v1/sources | {project_id,path} | 201 source з id,duration_ms,audio_streams |
| GET /v1/sources/{id} | немає | source metadata |
| POST /v1/sources/{id}/relink | {path} | source або SOURCE_MISMATCH |
| GET /v1/sources/{id}/media | Range header | registered bytes, 200/206/416 |
| POST /v1/clips | {source_id,title,start_ms,end_ms,categories,evidence_type:"manual"} | 201 clip, revision=1,review_status="suggested" |
| GET /v1/clips | source_id,review_status,category,limit,cursor | {items,next_cursor} |
| PATCH /v1/clips/{id} | {expected_revision,...editable fields} | clip з increment revision |
| POST /v1/clips/{id}/review | {expected_revision,status:"approved" або "rejected" або "suggested"} | clip з increment revision |
| POST /v1/transcripts/import | {source_id,format:"normalized_json",document} або {source_id,format:"srt"/"vtt",text} | 201 {id,revision,timing_level} |
| POST /v1/transcription-runs | {source_id,audio_stream_index,model,budget_usd} | 202 {id,job_id,status} |
| GET /v1/transcripts/{id} | offset,limit | transcript metadata + paginated segments/words |
| POST /v1/analysis-runs | {source_id,transcript_id,profile,model,budget_usd} | 202 {id,job_id,status} |
| GET /v1/analysis-runs/{id} | немає | run + clip_ids |
| POST /v1/export-batches | {project_id,clip_ids,output_dir,profile} | 202 {id,job_id,status} |
| GET /v1/export-batches/{id} | немає | {id,status,items,manifest_path,catalog_path} |
| GET /v1/jobs/{id} | немає | {id,state,progress,error} |
| POST /v1/jobs/{id}/cancel | {} | 202 job |
| POST /v1/jobs/{id}/resume | {retry_unknown:false} | 202 job або UNKNOWN_OUTCOME_REQUIRES_DECISION |
| GET /v1/settings | немає | nonsecret settings + has_api_key |
| PATCH /v1/settings | nonsecret allowlisted options | settings |
| POST /v1/runtime/credentials | {openrouter_api_key} main-only | 204; in-memory sidecar secret |
| POST /v1/cache/preview-cleanup | {project_id} | {files,count,bytes} app-owned only |
| POST /v1/cache/cleanup | {preview_id} | job; перевірити preview freshness |

Додано в M2 (2026-11-14): GET /v1/sources (project_id,limit,cursor → {items,next_cursor}; ADR 023); POST /v1/sources/{id}/previews (202), GET /v1/previews/{id}, GET /v1/previews/{id}/media (Range, 200/206/416) — preview-proxy, ADR 021. GET /v1/projects (Т18): параметр `q` (пошук підрядка в назві без урахування регістру, Unicode) і поля кожного item `source_count`, `clip_count`, `last_activity_at` — адитивно, ADR 023; `q` довший за 200 символів → 422. POST/GET /v1/sources (Т12a): `probe.frame_rate` (дріб "num/den") і `probe.vfr` (true/false/null — невідомо), ADR 023. GET /v1/system/checks (Т17) → {ffmpeg:{ok,version}, ffprobe:{ok,version}, data_dir:{ok,free_bytes}} — без шляхів; ADR 023.

Додано в M3 (2026-11-15, ADR 030): POST /v1/transcripts/import також приймає `offset_ms` (int32, за замовчуванням 0). Відповідь — надмножина {id,revision,timing_level}: source_id, origin (import/stt/correction), run_id, parent_id, language, duration_ms, word_count, segment_count, coverage, metadata, created_at. GET /v1/transcripts/{id}: `offset` (≥0) і `limit` (1–1000, за замовчуванням 200) застосовуються однаково до segments і words, плюс `next_offset` (null, коли більше немає). Новий маршрут GET /v1/transcripts?source_id&limit&cursor → {items,next_cursor}: список ревізій джерела. Помилковий cursor дає 422 INVALID_CURSOR.

Додано в M3 (2026-11-15, FR-020): POST /v1/transcripts/{id}/corrections {edits:[{word_id,text}]} для ревізії зі словами (timing_level word) або {edits:[{segment_id,text}]} для сегментної → 201 TranscriptOut нової ревізії: origin `correction`, parent_id = {id}, run_id = null, revision = максимум джерела + 1, а timing_level, language, duration_ms, coverage і metadata скопійовані з батьківської. Форма тіла: рівно 1–500 правок, лише ключі word_id/segment_id/text (час, ordinal, speaker змінити не можна), інакше 422 VALIDATION_ERROR зі списком `details.fields`. `text` — plain text: після trim 1–200 символів для слова, 1–2000 для репліки, без керівних символів (категорії Cc, Cs, а також U+2028/U+2029); розмітка не інтерпретується, зберігається обрізаний текст. Час (start_ms/end_ms), ordinal, id, speaker_local, confidence, chunk_id слів і сегментів та word_ids сегментів копіюються без змін, змінюється лише text. У ревізії зі словами сегмент, що містить хоч одне виправлене слово, отримує текст зі слів своїх word_ids через один пробіл (правило merge), решта сегментів зберігають текст; якщо отриманий текст довший за 2000 символів — 422. У сегментній ревізії слів немає: змінюється лише text зазначених сегментів. Помилки: невідома ревізія → 404 TRANSCRIPT_NOT_FOUND; невідомий id, обидва id або жодного в одній правці, id не того виду для ревізії (segment_id для ревізії зі словами й навпаки, тобто змішані види), дубль id, неприйнятний text → 422 VALIDATION_ERROR з details.field — шлях першої поганої правки (`edits[2]`, `edits[2].word_id`, `edits[2].segment_id` або `edits[2].text`); правка не останньої ревізії джерела → 409 TRANSCRIPT_REVISION_STALE з details {latest_id}, перевіряється двічі: перед підготовкою та під write lock перед записом. Батьківська ревізія не змінюється, помилка нічого не зберігає. Позначення пов'язаних AI-оцінок застарілими (FR-020) додасть M4, коли з'являться analysis runs.

Додано в M3 (2026-11-15, Т7c): GET /v1/settings → {stt_model, stt_language, analysis_model, run_budget_usd, has_api_key}. PATCH приймає JSON-об'єкт лише з цими чотирма ключами (інший → 422); `null` повертає default, `{}` → 200 без змін, запит без тіла → 422 VALIDATION_ERROR. stt_model ∈ {microsoft/mai-transcribe-2 (default), google/gemini-3.5-transcribe} і не може бути null; stt_language null (default, автовизначення) або `[a-z]{2}`; analysis_model null або `vendor/model` (ASCII, ≤100 символів, рівно один `/`; допускаються `~` на початку і `:`, `+`, `_`, `.`, `-` у назві моделі); run_budget_usd null або десятковий рядок 0.01–1000, до 6 знаків після крапки, зберігається як введено. Таблиця `app_settings(key,value_json)` (r0005); невалідне збережене значення ігнорується (береться default). POST /v1/runtime/credentials {openrouter_api_key: 20–200 друкованих ASCII без пробілу} → 204; неправильний ключ → 422 INVALID_API_KEY без відлуння значення, неправильна форма тіла → 422 VALIDATION_ERROR; ключ лише в пам'яті сайдкара і зникає при перезапуску. Додатковий DELETE /v1/runtime/credentials → 204 (FR-048). 429 видається для ErrorKind.LIMIT_EXCEEDED, зокрема BUDGET_EXCEEDED (details у мікродоларах).

Додано в M3 (2026-11-15, Т9c, FR-048): POST /v1/runtime/credentials/check (без тіла; тіло, якщо є, ігнорується) надсилає один `GET {base}/key` до OpenRouter із ключем із пам'яті сайдкара (`Authorization: Bearer`); викликається лише за явною дією користувача. Відповіді: 200 `{state:"valid", is_free_tier: bool, limit_remaining_usd: десятковий рядок|null}` (рядок без експоненти й кінцевих нулів; `null` — ліміту немає або поле відсутнє; від'ємний залишок лишається від'ємним) або 200 `{state:"invalid"}`, якщо OpenRouter відповів 401 (ключ у пам'яті не змінюється). Інших полів (label тощо) немає. Без ключа — 409 API_KEY_REQUIRED, нічого не надсилається. Мережа, таймаут 15 с (на кожну мережеву операцію й на весь виклик), 5xx, будь-який інший статус (зокрема 403, 429, редирект, що не виконується) і непридатне тіло (не JSON, немає `data.is_free_tier` типу bool, `limit_remaining` не число й не null, тіло понад 64 КіБ) дають 503 з `retryable:true` і фіксованим текстом: KEY_CHECK_NOT_SENT (немає з'єднання або в цьому режимі немає провайдера), KEY_CHECK_UPSTREAM (таймаут, 5xx, обрив відповіді), KEY_CHECK_INVALID_RESPONSE (інший статус чи непридатне тіло); у тексті може бути лише HTTP-статус. Одна спроба, без повторів і редиректів; ключ і тіло відповіді не потрапляють у логи, помилки чи повідомлення. Чи тарифікує OpenRouter цей запит, документація не каже. База й режим — як у STT-адаптері (`resolve_base_url`): production — лише https://openrouter.ai/api/v1, test — лише loopback `CLIPFORGE_TEST_PROVIDER_BASE_URL`; без нього запит не надсилається (503 KEY_CHECK_NOT_SENT).

Додано в M3 (2026-11-15, Т7b2, ADR 032): POST /v1/transcription-runs {source_id, audio_stream_index, model?, language?, budget_usd?} → 202 {id, job_id, status:"queued"}; завдання типу `transcription`, один робочий потік. model, language і budget_usd за відсутності беруться з налаштувань; budget_usd — десятковий рядок за правилом run_budget_usd (інакше 422 INVALID_RUN_BUDGET), model і language перевіряються як налаштування (422 INVALID_STT_MODEL / INVALID_STT_LANGUAGE, details.field — ім'я поля запиту); бюджету немає ні в запиті, ні в налаштуваннях → 422 BUDGET_REQUIRED; без FFmpeg або FFprobe → 503 MEDIA_TOOLS_UNAVAILABLE (після перевірок 422, до джерела; Т7f); невідоме джерело → 404 SOURCE_NOT_FOUND, відсутня доріжка → 422 INVALID_AUDIO_STREAM (джерело перевіряється раніше за ключ); у пам'яті сайдкара немає ключа → 409 API_KEY_REQUIRED; для джерела є завдання queued/running/cancelling → 409 TRANSCRIPTION_RUN_ACTIVE (details: run_id, job_id). Після початку зупинки sidecar create і resume → 503 SERVICE_STOPPING (retryable), нічого не записано (Т7f). Run, його частини і завдання записуються однією транзакцією: збій не лишає ні run, ні завдання. GET /v1/transcription-runs/{id} → {id, source_id, job_id, model, status (queued/running/completed/partial/failed/cancelled/interrupted), revision_id, error_code, committed_micro_usd (поточне значення з ledger), chunks:[{id, core_start_ms, core_end_ms, state, attempts, error_code}]}; розділений батьківський chunk не показується. Checkpoint завдання — {run_id, audio_stream_index, language, budget_usd}; ключа в ньому, в БД, файлах і логах немає: його читають із CredentialStore лише в момент надсилання запиту. Стан завдання: completed → succeeded; cancelled або скасування користувачем → cancelled; partial і failed → failed з error {code, message} (STT_AUTH, STT_FUNDS, BUDGET_EXCEEDED, STT_UNKNOWN_OUTCOME, код першої невдалої частини або TRANSCRIPTION_PARTIAL); progress {stage, total, succeeded, failed, unknown_outcome} пишеться по завершенні, поточний стан частин — у GET run. POST /v1/jobs/{id}/cancel: queued завершується одразу (run і його частини → cancelled), running → cancelling; зупиняється лише власне: пауза, FFmpeg-витяг аудіо, черга; запит, що вже в дорозі, відкликати не можна, його відповідь зберігається; завершене чи перерване завдання → 409 JOB_NOT_CANCELLABLE. POST /v1/jobs/{id}/resume {retry_unknown:false, budget_usd?} (тіло необов'язкове) → 202 job (queued) лише для transcription у стані interrupted, failed або cancelled (інакше 409 JOB_NOT_RESUMABLE, невідоме завдання 404); budget_usd — десятковий рядок за правилом створення run (інакше 422 INVALID_RUN_BUDGET, details.field = budget_usd) і замінює бюджет у checkpoint, тож run, зупинений BUDGET_EXCEEDED, можна продовжити з вищим лімітом (без budget_usd бюджет лишається попереднім); для того самого джерела є інше завдання queued/running/cancelling → 409 TRANSCRIPTION_RUN_ACTIVE (details: run_id, job_id цього іншого завдання); частини unknown_outcome є, а retry_unknown=false → 409 UNKNOWN_OUTCOME_REQUIRES_DECISION з details {count}; без ключа → 409 API_KEY_REQUIRED; у цих випадках нічого не змінюється й не надсилається. Після перезапуску незавершені завдання стають interrupted, а їхні run — interrupted: частина, що була running і мала резерв, стає unknown_outcome, без резерву — pending. До явного resume нічого не надсилається: платні виклики не повторюються автоматично. Зупинка sidecar (Runtime.close) під час роботи завдання: запит, що вже в дорозі, чекаємо (відповідь зберігається); незавершений run стає interrupted (не cancelled і не partial), а завдання — interrupted: частина, що не була надіслана, — pending, а та, що мала резерв, — unknown_outcome; скасування користувача, яке вже діє (cancelling), завершується як cancelled. Запит до провайдера перевіряється перед викликом (`build_request_body`): ValueError там дає STT_INVALID_REQUEST (резерв знімається, текст винятку не зберігається). ValueError із самого виклику — дефект: частина стає unknown_outcome, резерв лишається в обліку, завдання failed (TRANSCRIPTION_FAILED) (Т7f).

API шлях із {id} означає параметр маршруту, не текст для вставляння в команду. У тестах створені IDs беруться з response.

profile export: {"video_codec":"h264","quality":"high","audio_mode":"selected","audio_stream_index":1,"write_srt":false}. audio_stream_index — global source stream index. Для no audio audio_mode="none" та audio_stream_index=null. all додає всі доступні audio streams із коректними offsets, audio_stream_index=null. Непідтримувані values →422, не silent fallback.

analysis profile: categories (nonempty unique array), custom_instruction, min_duration_ms, max_duration_ms, max_candidates, allow_partial=false. Model є окремим field. Backend встановлює resolved model/provider/options у snapshot, не довіряє UI-валідації.

## 5 Стани та повторні операції

Job: queued → running → succeeded / failed / cancelled / interrupted. running → cancelling → cancelled. Recovery виявляє expired lease і переводить running у interrupted. resume створює новий attempt того самого job, зберігаючи tasks успіху. HTTP request ID і локальний task ID — різні поняття.

Clip review: suggested/approved/rejected. Export state незалежний: none/queued/running/verified/failed/cancelled/stale. Любий edit content збільшує revision; review mutation теж збільшує revision для concurrency. ExportItem immutable snapshot. Паралельна зміна clip не змінює render, але створений output може бути позначений stale.

Створення batch з будь-яким неapproved clip відхиляє запит 409 CLIP_NOT_APPROVED до запуску subprocess. Однакові IDs дедуплікуються. Cross-project clip →422. Batch завершення: succeeded, partial_failed, failed, cancelled. User cancel з кількома готовими items зберігає їх, batch=cancelled.

## 6 Test mode і запуск backend

Модуль apps/backend/clipforge/server.py має підтримати `python -m clipforge.server --port 18765`. Production main передає runtime config через stdin; для automated tests дозволені environment configuration CLIPFORGE_MODE=test, CLIPFORGE_DATA_DIR, CLIPFORGE_SESSION_TOKEN, CLIPFORGE_TEST_PROVIDER_BASE_URL. Test token — не OpenRouter key. Fake URL дозволений лише loopback і лише test mode. У production відхиляти ці test switches та non-OpenRouter provider base URL.

В E2E Electron отримує CLIPFORGE_E2E=1 і CLIPFORGE_DATA_DIR для ізольованих даних. Усі тестові режими показують помітний badge «Тестові дані» і не доступні випадково у release. Запуск через `npm run dev` і packaged app має використовувати одні й ті самі domain services.

## 7 Cache і відновлення

Artifact key = SHA256 канонічно серіалізованих input identity + parameters + versions. Cache valid лише коли payload schema, source hash і checksum artifact проходять перевірку. Temporary files мають власника task і lifecycle. Invalid/частково записаний checkpoint повторно генерується. Не використовувати mtime як єдину ознаку валідності.

Реальне filesystem inode lock/Windows file lock або named mutex не дозволяє двом worker instances одночасно змінювати один workspace. SQLite WAL не заміняє job locking. Перед DB migration створювати backup, у разі невдачі залишати попередню схему доступною для recovery, а не продовжувати в напівмігрованому стані.

Додано в M3 (2026-11-16, Т9a, ADR 037, FR-049/050): POST /v1/transcription-runs/estimate {source_id, audio_stream_index, model?, language?, budget_usd?} → 200 {source_id, audio_stream_index, model, language, duration_ms, chunk_count, billed_seconds, usd_per_second_upper, estimate_micro_usd, price_basis, budget_usd, within_budget, existing}; нічого не записує й не надсилає, ключ і FFmpeg не потрібні; 422 — ті самі коди, що в create (крім BUDGET_REQUIRED: без бюджету `budget_usd` і `within_budget` = null), 404 SOURCE_NOT_FOUND, 422 INVALID_AUDIO_STREAM. estimate_micro_usd — сума перших резервів частин (кожна округлена вгору); повтори і split можуть додати витрат. `existing` = {transcript_id, run_id} найновішого run `completed` з тією самою model, provider і options (доріжка, мова), чия ревізія існує, інакше null. POST /v1/transcription-runs приймає `force` (bool, за замовчуванням false): за такого збігу і без force → 409 TRANSCRIPT_ALREADY_EXISTS {transcript_id, run_id}, нічого не записано; порядок: 422 → 503 MEDIA_TOOLS_UNAVAILABLE → 404/422 джерела → 409 TRANSCRIPT_ALREADY_EXISTS → 409 API_KEY_REQUIRED → 503 SERVICE_STOPPING → 409 TRANSCRIPTION_RUN_ACTIVE (force не обходить відкрите завдання). Run partial, interrupted, failed, cancelled та імпортований транскрипт create не блокують. GET /v1/transcription-runs?source_id= → {items:[{id, source_id, job_id, model, status, revision_id, error_code, committed_micro_usd, created_at}]}, новіші перші (created_at, потім id), без пагінації; без source_id → 422, невідоме джерело → 404 SOURCE_NOT_FOUND.

Додано в M4 (2026-11-17, Т8, Т9, ADR 041 §10, §11, §20): POST /v1/analysis-runs {source_id, transcript_id, profile, model?, budget_usd?} (strict, зайві ключі → 422; budget_usd — рядок або JSON-число, не bool) → 202 {id, job_id, status}; POST /v1/analysis-runs/estimate (те саме тіло) → 200 оцінка §10, нічого не пише й не шле; GET /v1/analysis-runs/{id} → run + {job_id, committed_micro_usd, windows {total, succeeded, failed, unknown_outcome, pending}, clip_ids, stale, diagnostics_summary}; GET /v1/analysis-runs?source_id= → {items}, новіші перші. Порядок помилок create і resume — ADR 041 §20; resume і cancel — через /v1/jobs, як у STT. ClipOut отримує похідне `analysis_stale: bool` (кліп має analysis_run_id, а ревізія транскрипту run не найновіша ревізія джерела); межі кліпу від цього не змінюються.
