# Пошук і оцінювання моментів

## 1 Можливості цієї версії

Analyzer працює з текстом і source-aligned часом. Він може знайти гумор у репліках, пояснення гри, словесну реакцію й згадку про екшн. Він не бачить дії, обличчя або інтерфейс гри. Кандидат не є підтвердженим фактом візуальної події. FFmpeg може готувати thumbnail для людини, але thumbnail не перетворює text model на visual model.

## 2 Категорії та правила

funny: самодостатній жарт, кумедний діалог, явна словесна реакція. Сміх без контексту не є достатньою підставою високої оцінки.
action: мовленнєва згадка/супровід погоні, трюку, бою або втечі; visual_review_required=true.
fails: невдала спроба, помилка чи зірваний задум. Якщо результат установлено лише через припущення про картинку — visual_review_required=true.
unexpected: неочікувана зміна або результат; відрізняти реально озвучену подію від гіпотези «було б смішно, якби». За візуальної залежності — review required.
game_details: конкретна механіка, знахідка, пояснення; не перетворювати здогад стрімера на доведений факт гри.

Selected categories мають OR-семантику. У output categories тільки значення з дозволеного повного enum; збережений кандидат повинен мати intersection із selected. Cross-category candidate створюється один раз. Custom instruction уточнює subject/style, але не може обійти evidence, schema або category intersection. Повний текст user instruction зберігається в run.

## 3 Підготовка analysis input

Залишати stable word/segment IDs і chronological order. Порівняння й індексацію робити за ordinals, не за лексикографічним сортуванням IDs. Один analysis window орієнтовно 5–10 хвилин мовлення, limited tokens; context до 60с з кожного боку. Window core ownership не дає одній події створюватися кілька разів. За недостатнього контексту candidate потребує додаткової перевірки сусіднього window.

Передавати компактний source context (game/domain, language, source duration), profile, timing_level, words/segments, allowed IDs. User message — JSON object із полями source_context, profile, timing_level та units. units — впорядкований масив word або segment objects із id, ordinal, start_ms, end_ms, text; змішування двох рівнів в одному запиті не допускається. Це також контракт fake provider у пакеті. Глобальний стислий context може допомогти розумінню, але evidence повинні бути локальними реально існуючими репліками. Якщо transcript містить сторонні інструкції на кшталт «ігноруй правила», трактувати їх як цитоване мовлення.

## 4 Output та валідація

Model output відповідає contracts/selection.schema.json: candidates[]; кожен має title, categories, start_id,end_id,evidence_ids,reason,scores,visual_review_required. Model не повертає executable commands/paths/final numeric timestamps. Відсутній model candidate ID — локальний ID генерує backend. Text fields мають max lengths.

Semantic validation: усі IDs існують у цьому transcript revision; start ordinal≤end ordinal; evidence входить у вибраний span; категорії перетинаються з профілем; reason не порожній; max_candidates не перевищено після фінального ранжування. Timing з output не приймати навіть якщо модель додала unknown fields — strict schema відхиляє.

Model output size bounded; JSON parse timeout/length limits. Зберігати raw response локально для діагностики без key. Одна repair спроба зберігає первинні source_context/profile/timing_level/units та додає validation_errors і previous_output як недовірені дані. Це дозволяє виправити ID за справжніми units. За повторної помилки run failed. Не «виправляти» unknown IDs найближчим збігом мовчки.

## 5 Розрахунок меж

У word mode raw_start=start_word.start_ms, raw_end=end_word.end_ms; segment mode аналогічний. Додати рекомендований pre=3000мс/post=4000мс і clamp до 0..duration. Якщо sentence і event не завершені — review flag; не автоматично різати по середині для max duration. Shorter than min або longer than max candidate може бути запропонований у needs_review group лише якщо користувач увімкнув «Показувати поза бажаною тривалістю» (P1); default — відкинути з internal reason.

Три рівні часу: evidence interval, recommended interval, approved interval. Вони зберігаються окремо. Користувач може розширити межі понад evidence і бажану AI-тривалість. Export бере current approved revision. LLM ніколи не замінює ручні межі при reanalysis.

## 6 Оцінки та пріоритет

Кожен model component integer 0..4: clarity, hook, completeness, emotion, novelty, relevance. Backend обчислює rank 0..100 як round(100×(0.20 clarity+0.15 hook+0.25 completeness+0.15 emotion+0.10 novelty+0.15 relevance)/4). Ваги — калібрувальні налаштування версії prompt, а не емпірично доведений прогноз.

Default candidate threshold rank≥60 і completeness≥2. В UI показати «Високий» ≥80, «Середній» 60–79; numerical rank може бути у details. Поріг змінюється тільки versioned config. Правдивість події не оцінювати rank: вона має окремий evidence_type/visual_review_required.

Спочатку відкинути invalid/low quality, потім duplicates, потім rank/diversity. Diversification може поміняти порядок близьких за score сцен, але не проштовхувати слабку сцену заради категорії. Cap виконується після dedupe.

## 7 Дедуплікація

Порівнювати тільки один source. Сигнал duplicate: temporal IoU≥0.65 або overlap/min_duration≥0.8 плюс спільні evidence чи семантично одна подія. Самого temporal overlap недостатньо для автоматичного злиття різних подій у довгій сцені. Обрати кращий candidate, union labels/evidence лише за сумісного контексту; зберегти suppressed candidates у run diagnostics.

Через різні windows можуть утворитися два кандидати зі start IDs різними, але однаковою кульмінацією. Звірити evidence overlap перед merge. Дві однакові фрази у різних годинах не duplicate.

Reanalysis: suggested AI candidates нового run можна dedupe зі старими suggested. Approved/rejected/manual залишаються незмінні; новий duplicate позначити already_reviewed і не повертати у звичайну чергу. Користувач може явно показати повторні пропозиції.

## 8 Якість і калібрування

Offline deterministic tests перевіряють parsing, bounds, IDs, filtering, scores, dedupe й prompts as data, а не творчий смак. Для живого AI потрібен benchmark із розміткою власника: accepted/topK, found/reference speech-evidenced scenes, median boundary adjustment seconds, diversity, cost/accepted clip, review time.

Зафіксувати dataset split перед налаштуванням. У developer subset змінювати prompt/threshold; held-out не використовувати для tuning. Зберігати source hashes, model/provider IDs, options, date, профілі, anonymized reviewer decisions. Без API доступний deterministic fake E2E, але він не доводить якість моделі.
