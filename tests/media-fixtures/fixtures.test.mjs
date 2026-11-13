import {test} from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
const dir=path.resolve('test-results/fixtures');
function probe(file,extra=[]){const r=spawnSync(process.env.FFPROBE_PATH||'ffprobe',['-v','error',...extra,'-show_streams','-show_format','-of','json',path.join(dir,file)],{encoding:'utf8',windowsHide:true});assert.equal(r.status,0,r.stderr);return JSON.parse(r.stdout);}
test('color fixture has 12 seconds with deliberately sparse keyframes',()=>{const d=probe('color_gop.mp4',['-select_streams','v:0','-show_frames','-show_entries','frame=best_effort_timestamp_time,key_frame']);assert.ok(Math.abs(Number(d.format.duration)-12)<0.01);const keys=d.frames.filter(f=>f.key_frame===1).map(f=>Number(f.best_effort_timestamp_time));assert.ok(!keys.some(t=>Math.abs(t-4.12)<0.5));assert.ok(keys.length<=2);});
test('no-audio fixture really has no audio',()=>assert.equal(probe('no_audio.mp4').streams.filter(s=>s.codec_type==='audio').length,0));
test('dual-track fixture preserves distinct stream indices and languages',()=>{const a=probe('two_tracks.mkv').streams.filter(s=>s.codec_type==='audio');assert.deepEqual(a.map(s=>s.index),[1,2]);assert.deepEqual(a.map(s=>s.tags.language),['ukr','eng']);});
test('offset fixture contains audio beginning 1.5 seconds after video',()=>{const d=probe('audio_offset.mkv');const v=d.streams.find(s=>s.codec_type==='video'),a=d.streams.find(s=>s.codec_type==='audio');assert.ok(Math.abs(Number(a.start_time)-Number(v.start_time)-1.5)<0.01);});
test('VFR fixture has both short and long presentation intervals',()=>{const d=probe('vfr.mkv',['-select_streams','v:0','-show_frames','-show_entries','frame=best_effort_timestamp_time']);const t=d.frames.map(f=>Number(f.best_effort_timestamp_time));const deltas=t.slice(1).map((x,i)=>x-t[i]);assert.ok(deltas.some(x=>x>0.03&&x<0.04));assert.ok(deltas.some(x=>x>0.06&&x<0.07));});
test('Unicode fixture is byte-identical to source',()=>assert.deepEqual(fs.readFileSync(path.join(dir,'Київ запис.mp4')),fs.readFileSync(path.join(dir,'color_gop.mp4'))));
