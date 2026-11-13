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
TranscriptChunk(id,run_id,core_start_ms,core_end_ms,upload_start_ms,upload_end_ms,state,attempts,request_id,response_path,error_code,reserved_cost,actual_cost).
TranscriptWord(id,run_id,chunk_id,ordinal,start_ms,end_ms,text,speaker_local,confidence_nullable).
TranscriptSegment(id,run_id,start_ms,end_ms,text,word_ids,timing_level).
AnalysisRun(id,source_id,transcript_revision,profile_json,model,prompt_version,schema_version,status,usage).
Clip(id,source_id,analysis_run_id_nullable,revision,title,start_ms,end_ms,recommended_start_ms,recommended_end_ms,categories,evidence_type,evidence_ids,reason,scores,rank,visual_review_required,review_status,created_at,updated_at).
ExportBatch(id,project_id,output_dir,profile_json,status,created_at).
ExportItem(id,batch_id,clip_id,clip_revision,snapshot_json,state,temp_path,final_path,observed_json,error_code).
Job(id,type,state,progress_json,lease_owner,lease_expires_at,checkpoint_json,created_at,updated_at).
SpendEntry(id,run_id,request_id,state,reserved_usd,actual_usd_nullable,model,price_snapshot).

Час у домені — integer milliseconds; UTC timestamps з ISO8601 для журналів. SQLite constraints: source duration>0; 0≤clip.start<clip.end; app validator також перевіряє end≤source duration. JSON arrays зберігати з schema version. Evidence IDs належать одному transcript revision. Large raw provider response — app-owned file, посилання в DB, без ключів.

## 4 API правила

Base /v1. JSON UTF-8. Authorization: Bearer session token на всіх routes, включно з health. Успішна mutation response — entity або job envelope, не голий bool. Error shape: {"error":{"code":"INVALID_INTERVAL","message":"Кінець має бути пізніше початку","details":{},"retryable":false}}.

HTTP 400 — некоректна доменна дія; 401 — token; 404 — ID; 409 — stale revision/state conflict; 422 — invalid schema/bounds; 429 — local rate/budget condition із domain code; 503 — unavailable dependency. Не повертати raw exception/секрети у response.

### Основні маршрути

| Метод і шлях | Тіло / параметри | Відповідь |
|---|---|---|
| GET /v1/health | немає | {status:"ok",mode:"test" або "production",version} |
| POST /v1/projects | {name} | 201 {id,name} |
| GET /v1/projects | limit,cursor | {items,next_cursor} |
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
