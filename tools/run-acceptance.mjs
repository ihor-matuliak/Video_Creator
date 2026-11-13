import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {randomBytes} from 'node:crypto';
import {spawn} from 'node:child_process';
import {startFake} from './fake-openrouter.mjs';
const backend=path.resolve('apps/backend');
if(!fs.existsSync(path.join(backend,'clipforge','server.py'))){console.error('NOT RUN: implement apps/backend/clipforge/server.py before product acceptance tests.');process.exit(2);}
if(!fs.existsSync('test-results/fixtures/color_gop.mp4')){console.error('Run npm run fixtures first.');process.exit(2);}
const root=process.cwd();const fake=await startFake();const data=fs.mkdtempSync(path.join(os.tmpdir(),'clipforge-qa-'));
const token=randomBytes(32).toString('hex');const base='http://127.0.0.1:18765';
const py=process.env.CLIPFORGE_PYTHON||(process.platform==='win32'?path.join(root,'.venv','Scripts','python.exe'):path.join(root,'.venv','bin','python'));
const env={...process.env,CLIPFORGE_MODE:'test',CLIPFORGE_DATA_DIR:data,CLIPFORGE_SESSION_TOKEN:token,CLIPFORGE_TEST_PROVIDER_BASE_URL:fake.base+'/api/v1',CLIPFORGE_ALLOW_LIVE_AI:'false'};
let child;let launched=false;let log='';
try{
 child=spawn(py,['-m','clipforge.server','--port','18765'],{cwd:backend,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
 child.on('error',e=>{log+=e.message;});child.stdout.on('data',b=>log+=b);child.stderr.on('data',b=>log+=b);
 for(let i=0;i<100;i++){try{const r=await fetch(base+'/v1/health',{headers:{Authorization:'Bearer '+token},signal:AbortSignal.timeout(300)});if(r.ok){const h=await r.json();if(h.mode!=='test')throw new Error('Backend must report test mode');launched=true;break;}}catch{}await new Promise(r=>setTimeout(r,100));}
 if(!launched)throw new Error('Backend did not become ready. '+log.replaceAll(token,'[REDACTED]'));
 const tests=spawn(process.execPath,['--test','--test-concurrency=1','tests/acceptance/api.test.mjs'],{cwd:root,env:{...env,CLIPFORGE_TEST_BASE_URL:base,CLIPFORGE_TEST_TOKEN:token,CLIPFORGE_FAKE_CONTROL:fake.base},stdio:'inherit',windowsHide:true});
 process.exitCode=await new Promise(resolve=>{tests.once('exit',code=>resolve(code??1));tests.once('error',()=>resolve(1));});
}catch(e){console.error(e.message);process.exitCode=1;}finally{if(child&&child.exitCode===null)child.kill();await fake.close();console.log('Isolated test artifacts retained at '+data);}
