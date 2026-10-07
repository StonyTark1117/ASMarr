import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'.',testMatch:'app.spec.ts',workers:1,reporter:'line',timeout:30000,use:{baseURL:'https://127.0.0.1:8789',ignoreHTTPSErrors:true},webServer:{command:'python3 start_fixture.py',url:'https://127.0.0.1:8789/healthz',ignoreHTTPSErrors:true,timeout:60000,reuseExistingServer:false}});
