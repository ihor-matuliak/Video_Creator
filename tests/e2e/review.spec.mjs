import {test,expect,_electron as electron} from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
const entry=process.env.CLIPFORGE_ELECTRON_ENTRY;
if(!entry||!fs.existsSync(entry))throw new Error('CLIPFORGE_ELECTRON_ENTRY must point to the built Electron main entry. These tests require the implemented app.');
let app,page,output;
test.beforeEach(async()=>{
 const data=fs.mkdtempSync(path.join(os.tmpdir(),'clipforge-e2e-'));
 output=path.join(data,'exports');fs.mkdirSync(output);
 app=await electron.launch({...(process.env.CLIPFORGE_ELECTRON_EXECUTABLE?{executablePath:process.env.CLIPFORGE_ELECTRON_EXECUTABLE}:{}),args:[path.resolve(entry)],env:{...process.env,CLIPFORGE_MODE:'test',CLIPFORGE_E2E:'1',CLIPFORGE_DATA_DIR:data,CLIPFORGE_ALLOW_LIVE_AI:'false'}});
 await app.evaluate(({dialog},{source,folder})=>{dialog.showOpenDialog=async(...args)=>({canceled:false,filePaths:[args.at(-1)?.properties?.includes('openDirectory')?folder:source]});},{source:path.resolve('test-results/fixtures/color_gop.mp4'),folder:output});
 page=await app.firstWindow();await expect(page.getByTestId('app-ready')).toBeVisible();
 await page.getByTestId('new-project').click();await page.getByTestId('project-name').fill('Перевірка інтерфейсу');await page.getByTestId('create-project').click();
 await page.getByTestId('import-video').click();await expect(page.getByTestId('source-ready')).toBeVisible();
});
test.afterEach(async()=>{if(app)await app.close();});
async function manual(){await page.getByTestId('manual-clip').click();await page.getByTestId('clip-title').fill('Мій фрагмент');await page.getByTestId('clip-start').fill('00:00:04.120');await page.getByTestId('clip-end').fill('00:00:07.880');await page.getByTestId('save-clip').click();}
test('E2E-01 manual clipping, approval and actual export',async()=>{await manual();await page.getByTestId('approve-clip').click();await page.getByTestId('select-clip').first().check();await page.getByTestId('export-selected').click();await page.getByTestId('confirm-export').click();await expect(page.getByTestId('export-complete')).toBeVisible({timeout:60000});expect(fs.readdirSync(output).some(f=>f.endsWith('.mp4'))).toBe(true);});
test('E2E-02 reversed interval displays actionable validation',async()=>{await page.getByTestId('manual-clip').click();await page.getByTestId('clip-start').fill('00:00:08.000');await page.getByTestId('clip-end').fill('00:00:04.000');await page.getByTestId('clip-end').blur();await expect(page.getByTestId('interval-error')).toBeVisible();await expect(page.getByTestId('save-clip')).toBeDisabled();});
test('E2E-03 keyboard shortcuts do not overwrite text editing',async()=>{await page.getByTestId('manual-clip').click();const title=page.getByTestId('clip-title');await title.fill('');await title.pressSequentially('air o');await expect(title).toHaveValue('air o');await expect(page.getByTestId('clip-title')).toBeFocused();});
test('E2E-04 empty categories disable analysis',async()=>{await page.getByTestId('search-settings').click();await expect(page.getByTestId('category-funny')).toBeChecked();await page.getByTestId('categories-all').click();for(const cat of ['funny','action','fails','unexpected','game_details'])await expect(page.getByTestId('category-'+cat)).not.toBeChecked();await expect(page.getByTestId('find-highlights')).toBeDisabled();await expect(page.getByTestId('category-error')).toBeVisible();});
