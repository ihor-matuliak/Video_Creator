import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const out=path.join(root,'test-results','fixtures');fs.mkdirSync(out,{recursive:true});
const ffmpeg=process.env.FFMPEG_PATH||'ffmpeg';
function run(args){const r=spawnSync(ffmpeg,['-hide_banner','-loglevel','error','-y',...args],{encoding:'utf8',windowsHide:true,timeout:120000});if(r.error||r.status!==0)throw new Error(r.error?.message||r.stderr);}
const enc=['-c:v','libx264','-preset','ultrafast','-crf','18','-pix_fmt','yuv420p'];
run(['-f','lavfi','-i','color=c=red:s=320x180:r=30:d=4','-f','lavfi','-i','color=c=green:s=320x180:r=30:d=4','-f','lavfi','-i','color=c=blue:s=320x180:r=30:d=4','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=12','-filter_complex','[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]','-map','[v]','-map','3:a',...enc,'-g','300','-keyint_min','300','-sc_threshold','0','-c:a','aac',path.join(out,'color_gop.mp4')]);
run(['-i',path.join(out,'color_gop.mp4'),'-map','0:v:0','-c:v','copy','-an',path.join(out,'no_audio.mp4')]);
run(['-i',path.join(out,'no_audio.mp4'),'-f','lavfi','-i','sine=frequency=220:sample_rate=48000:duration=12','-f','lavfi','-i','sine=frequency=880:sample_rate=48000:duration=12','-map','0:v:0','-map','1:a:0','-map','2:a:0','-c:v','copy','-c:a','pcm_s16le','-metadata:s:a:0','language=ukr','-metadata:s:a:1','language=eng',path.join(out,'two_tracks.mkv')]);
run(['-i',path.join(out,'no_audio.mp4'),'-itsoffset','1.5','-f','lavfi','-i','sine=frequency=1000:sample_rate=48000:duration=10.5','-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','pcm_s16le',path.join(out,'audio_offset.mkv')]);
run(['-f','lavfi','-i','testsrc2=size=320x180:rate=30:duration=6','-vf',"setpts='if(lt(N,90),N/(30*TB),(3+(N-90)/15)/TB)'",'-fps_mode','vfr',...enc,path.join(out,'vfr.mkv')]);
fs.copyFileSync(path.join(out,'color_gop.mp4'),path.join(out,'Київ запис.mp4'));
if(process.argv.includes('--long'))run(['-f','lavfi','-i','color=c=purple:s=96x64:r=1:d=21600',...enc,'-g','60',path.join(out,'six_hours.mp4')]);
fs.writeFileSync(path.join(out,'fixture-info.json'),JSON.stringify({synthetic:true,source_duration_ms:12000,colors:[{start_ms:0,end_ms:4000,color:'red'},{start_ms:4000,end_ms:8000,color:'green'},{start_ms:8000,end_ms:12000,color:'blue'}],audio_frequencies_hz:[220,880],audio_offset_ms:1500,description:'Synthetic QA data, no real gameplay or speech. VFR and audio-offset have dedicated files.'},null,2));
console.log('Generated fixtures in '+out);
