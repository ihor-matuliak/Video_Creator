import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'.',testMatch:'review.spec.mjs',workers:1,fullyParallel:false,timeout:90000,expect:{timeout:15000},reporter:[['list'],['html',{outputFolder:'../../test-results/e2e-report',open:'never'}]],outputDir:'../../test-results/e2e-artifacts',use:{trace:'retain-on-failure'}});
