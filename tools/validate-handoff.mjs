import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {spawnSync} from 'node:child_process';
const out=path.resolve('test-results/handoff');fs.mkdirSync(out,{recursive:true});
const runs=[['syntax',['tools/check-syntax.mjs']],['pack',['--test','tests/pack/contracts.test.mjs','tests/pack/fake-provider.test.mjs']],['fixtures',['--test','tests/media-fixtures/fixtures.test.mjs']]];
const results=[];
for(const [name,args] of runs){const r=spawnSync(process.execPath,args,{encoding:'utf8',windowsHide:true,timeout:120000});const output=(r.stdout||'')+(r.stderr||'');fs.writeFileSync(path.join(out,name+'.log'),output);results.push({name,status:r.status===0?'PASS':'FAIL',exit_code:r.status,error:r.error?.message??null});console.log(name+': '+results.at(-1).status);}
const count=p=>(fs.readFileSync(p,'utf8').match(/^test\('/gm)||[]).length;
const ff=spawnSync(process.env.FFMPEG_PATH||'ffmpeg',['-version'],{encoding:'utf8',windowsHide:true});
const report={created_at:new Date().toISOString(),platform:os.platform(),release:os.release(),node:process.version,ffmpeg:(ff.stdout||'').split('\n')[0],results,acceptance_tests:count('tests/acceptance/api.test.mjs'),e2e_tests:count('tests/e2e/review.spec.mjs'),product_status:'NOT IMPLEMENTED / PRODUCT TESTS NOT RUN'};
fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));
const md=`# Звіт перевірки пакета ClipForge Local

Дата запуску: ${report.created_at}. Середовище: ${report.platform} ${report.release}; Node ${report.node}; ${report.ffmpeg}.

## Виконано

| Перевірка | Статус | Що означає |
|---|---|---|
| Синтаксис усіх JS інструментів і тестів | ${results[0].status} | Файли успішно розбираються Node; це не виконання UI/backend |
| Пакет і fake provider | ${results[1].status} | 14 тестів комплектності, узгодженості прикладів, часових векторів та тестового HTTP-сервера |
| Synthetic media | ${results[2].status} | 6 перевірок справжніх файлів через FFprobe: sparse keyframes, no audio, два tracks, offset, VFR, Unicode |

JSON і повні логи запуску: test-results/handoff. Короткі synthetic fixtures генеруються tools/generate-fixtures.mjs; вони не містять користувацьких записів.

## Написано для майбутньої реалізації, не виконано

- ${report.acceptance_tests} API acceptance-тестів: авторизація, bounds/revisions, approval, точне нарізання поза keyframe, незмінність оригіналу, Unicode, no-audio, вибір звукового вмісту, audio offset, VFR, transcript validation, категорії, empty result, evidence IDs.
- ${report.e2e_tests} Playwright Electron-тести: ручний кліп та експорт, invalid interval, введення тексту з гарячими клавішами, порожні категорії.
- Матриця 56 сценаріїв задає повний обсяг майбутніх перевірок. Надані файли покривають підмножину; решту Claude Code має додати під час milestones.

## Межі цієї перевірки

Продукт ще не реалізовано. Запуск acceptance runner без apps/backend/clipforge/server.py завершується ненульовим кодом і повідомленням NOT RUN. UI suite потребує зібраного Electron-застосунку та Playwright. Тут не виконувались живі платні AI-запити, оцінка якості на GTA-записах, performance-тест реального шестигодинного відео, тест чистої Windows або security-аудит готової програми. Шестигодинний синтетичний файл створено окремо; FFprobe підтвердив duration=21600 секунд. Це лише перевірка фікстури, не продуктивності продукту.

Перевірка схем у qa:pack контролює ключі прикладів і частину семантики; вона не заміняє повну JSON Schema validation у майбутньому backend. У цьому середовищі немає робочого Python runtime, тому Python-тести не запускалися.

## Виправлення під час підготовки

Виправлено екранування виразу setpts у генераторі VFR-фікстури; після виправлення реальні PTS перевірено. Узгоджено repair input із початковими transcript units, додано відсутні UI test IDs, уточнено nullable audio index для no-audio/all. Експортні acceptance assertions перевіряють також частоту вибраного аудіо та збереження початкової тиші.

Готовність цього пакета означає готовність до розробки за ТЗ, а не готовність застосунку до використання.
`;
fs.writeFileSync('VALIDATION_REPORT.md',md);
process.exitCode=results.every(x=>x.status==='PASS')?0:1;
