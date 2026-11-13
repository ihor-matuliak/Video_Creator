import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
const roots=['tools','tests'];let count=0;
function walk(dir){for(const e of fs.readdirSync(dir,{withFileTypes:true})){const p=path.join(dir,e.name);if(e.isDirectory())walk(p);else if(p.endsWith('.mjs')){const r=spawnSync(process.execPath,['--check',p],{encoding:'utf8',windowsHide:true});if(r.status!==0)throw new Error(r.stderr||p);count++;}}}
roots.forEach(walk);console.log(`Syntax valid: ${count} JavaScript files. This is not product test execution.`);
