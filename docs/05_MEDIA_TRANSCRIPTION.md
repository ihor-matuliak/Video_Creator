# Обробка медіа і транскрипція

## 1 Канонічний час

Доменний нуль — presentation time першого відображуваного кадру відеодоріжки. Джерело може мати ненульові container/stream start_time, edit lists і аудіо, яке починається пізніше. Зберігати observed origin і time_base у source metadata. Усі timeline offsets переводяться від одного origin; незалежний PTS reset відео й аудіо може зруйнувати синхронізацію.

Integer milliseconds — формат домену/API/DB. Для розрахунку кадрів використовувати rational timebase без накопичення float error. Інтервал кліпу напіввідкритий [start,end). Під час export first video frame — перший доступний PTS≥start, last PTS<end. Фактичні first/last timestamps записувати у manifest. Для VFR не прирівнювати frame index до time×average_fps.

Аудіо для транскрипції нормалізується до того самого origin. Якщо audio starts +1500мс, початок extraction до цього часу містить тишу. Якщо починається раніше — початок до canonical zero відкидається. Змішування доріжок у цій версії не потрібне.

## 2 Медіапрофілі

Original: read-only, без зміни байтів. Probe може бути cached за source fingerprint, але export перед завершенням перевіряє ідентичність. Preview: H.264/AAC proxy до 720p за потреби player, без зміни тривалості чи швидкості. Source-to-proxy mapping зберігає zero/PTS; не виконувати silent duration normalization.

STT: FLAC або PCM WAV, mono 16 kHz як стартовий профіль, який перевіряється на цільовому матеріалі. Downmix здійснювати стандартно, не phase-invert канал; за окремого голосового audio stream віддавати перевагу йому. Передавання stereo замість mono можливе в adapter profile, якщо це покращує результат і вкладається в limits. Початкова реалізація не використовує aggressive denoise, pitch shift, silence removal або tempo change.

Export: MP4/H.264 libx264, CRF18, preset medium, yuv420p, AAC 192k для вибраного audio; source geometry, rotation applied once, aspect preserved. Odd dimensions pad до парних без crop; HDR/color metadata потребує explicit strategy: у v1 HDR source має зрозумілий unsupported status, поки не реалізований і протестований tone mapping. Не перетворювати HDR мовчки на washed-out SDR. Частота кадрів/PTS зберігається, VFR залишається VFR; output frame count може змінитися лише через потрібне відсікання.

## 3 FFmpeg runner

Усі виклики — масив argument tokens через subprocess з shell=False. Filename завжди окремий аргумент, ніколи вставка у shell string. Media filtergraph генерується лише з валідованих чисел/enum, а не з model text. Unicode/spaces/quotes/reserved Windows names мають tests. ffmpeg і ffprobe versions записуються у run/manifest.

Не використовувати stream copy як default exact cut. Builder має обрати seek/decode/trim/setpts policy, яка проходить content-boundary та A/V-offset тести; конкретний порядок flags фіксується після M1 spike. Самі `-ss` і `-t` без обліку origin не є достатнім доказом точності. Прогрес отримувати machine-readable `-progress pipe:1`, stderr — bounded log. Не парсити локалізований довільний текст як єдине джерело стану.

Child process запускається без видимого console window у desktop. На Windows використовувати Job Object або інший scoped lifecycle, який завершує тільки процеси власного task. Не викликати глобальний kill ffmpeg.exe. При cancel спочатку graceful stop, потім bounded terminate, за потреби kill власної process tree. Temp files очищаються після закриття handles. Відсутність permission або disk full дає окремий код і не залишає verified export.

Тестувати тривалість, codec/streams і декодування. У test fixture перевіряти фактичний вміст першого/останнього кадру, а не тільки duration. Повний decode final clip required для v1; це додатковий час, який показується як «Перевіряємо файл». Цільова тривалість має допуск max(1 локальний frame interval, 50мс контейнерного округлення). A/V drift перевіряти signal correlation на спеціальних fixtures, не лише порівнянням stream duration.

## 4 Розбиття аудіо

Core windows — неперекривні інтервали [k×300000,min((k+1)×300000,D)). Upload window розширює core на 5000мс в обидва доступні боки. Зберігати чотири числа: core_start/core_end/upload_start/upload_end. Для source 620000мс це core 0–300000,300000–600000,600000–620000; upload 0–305000,295000–605000,595000–620000.

Нормалізований word time = upload_start_ms + round(provider_seconds×1000). Повторно не додавати audio stream offset, якщо extraction вже приведено до canonical timeline. Після conversion перевірити 0≤start<end≤D. Out-of-range понад допуск 100мс → invalid provider timing; дрібне rounding overflow clamp із diagnostic. Не clamp довільні хвилини помилки.

Ownership визначати за midpoint слова в core, але прикордонні words звіряти з сусіднім chunk: та сама normalised phrase і близькі інтервали → один occurrence. Не втрачати слово, що перетинає boundary. Не дедуплікувати всі однакові тексти по всьому source. Детальний merge test: два однакових «ні» на 1с і 10с лишаються двома словами; duplicate на overlapping upload boundary стає одним.

При adaptive split розбивати тільки core, а upload context перераховувати. Parent chunk позначити superseded, дочірні неперекривні cores покривають його точно. У coverage не рахувати context вдруге. Коли chunk succeeded із zero speech, coverage усе одно успішне; empty response через error не трактувати як silence.

## 5 OpenRouter STT adapter

MAI-Transcribe 2, Gemini Transcribe та Whisper — різні моделі/сімейства. Назва Whisper тут не означає універсальний STT endpoint. У цій версії не потрібно встановлювати Whisper: реалізуються два погоджені OpenRouter adapters, а їхні відповіді приводяться до власної спільної схеми.

Контракт перевірений 12.11.2026: POST https://openrouter.ai/api/v1/audio/transcriptions; model IDs microsoft/mai-transcribe-2 і google/gemini-3.5-transcribe. Запит JSON input_audio із data base64/format або підтримуваний multipart. Потрібні response_format=verbose_json і timestamp_granularities=[segment,word]. Provider-specific options для MAI передаються через provider.options.azure; verbatim явно задається у відповідній підтримуваній enhancedMode.modelOptions гілці.

Adapter не припускає, що кожен provider поверне однакові fields. Нормалізувати duration, language, segments, words, usage і request ID. Відсутні speaker/confidence залишаються null. Capability smoke з реальним акаунтом має підтвердити фактичне forwarding options; документація не заміняє цього тесту. Diarization default off: вона не потрібна для нарізання й ускладнює limits/зв'язування speakers між chunks.

Початковий payload ceiling 20 MiB для binary audio з додатковим запасом для base64/JSON; фактичний transport ceiling задається adapter capability. Не вважати model duration limit універсальним upload limit. При 413 розділити chunk; при timeout не робити нескінченний retry. Gemini word timestamps обмежують документовану тривалість запиту до 30хв, але власні windows значно коротші. Актуальні limits перечитати в M0. Джерела в 09_SOURCES.

## 6 Retry, cache і бюджет

401/403 — stop run, запропонувати налаштування ключа. 402 — insufficient funds, stop. 429 — врахувати Retry-After, exponential backoff із jitter, максимум 3 спроби на конкретний task. 400/422 — не повторювати той самий invalid body. 413 — adaptive split. 5xx — bounded retry, якщо response означає відмову до завершення; connection timeout після відправлення request може мати unknown paid outcome. Зберігати request ID і стан, показати явне рішення повторити.

За замовчуванням concurrency STT=2, analysis=1, export=1. Налаштування не повинні перевищувати provider quota; rate limit загальний на adapter. Бюджет перевіряється під lock перед резервуванням кожного request, щоб паралельні chunks не перевищили ceiling. Облік overlap, repair і retries включений. Не обіцяти абсолютний hard cap на сторонньому рахунку за невідомих тарифів/unknown outcomes; UI пояснює межу локального контролю. Якщо верхню оцінку неможливо отримати, автоматичний довгий run блокується до pilot і заданого ceiling.

Cache не містить key. Audio artifacts і transcript — app-owned files; можна очистити raw audio після успішного STT, але зберегти checksum/options, щоб реконструювати. Changing categories не invalidates transcript. Changing STT model invalidates transcript cache для нового run, але не видаляє старий revision.

## 7 Import SRT/VTT і робота з текстом

Парсер підтримує HH:MM:SS,mmm / HH:MM:SS.mmm, cue identifiers, multiline cues, BOM. VTT styling/HTML не виконується; текст очищується до plain representation. Cue overlaps допустимі для одночасного мовлення, але end≤start і outside source — error. Немає words: timing_level=segment, selection mode=segment. Plain TXT зберігати як note, не автоматично вигадувати alignment.

Imported transcript не доводить тотожності версії source. Показати 3 контрольні точки для звірки, дозволити єдиний explicit offset з preview. Не застосовувати прихований affine time stretch. Користувацький offset записати в revision metadata; після нього знову перевірити межі.

## 8 Файлова безпека

App-owned data root обирається окремо від source folder. Export directory може бути зовнішнім лише через native picker і повний absolute path. Cleanup resolve/symlink/reparse guards забороняють вихід із cache root. Ніколи не видаляти unknown files. Temp name містить task UUID, final basename не містить prompt/LLM-controlled path. Same-source hash check перед render і immutable handle/mtime verification після render захищають від змін під час роботи; на зміну abort із SOURCE_CHANGED.
