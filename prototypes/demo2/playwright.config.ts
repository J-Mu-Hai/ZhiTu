import { defineConfig } from '@playwright/test';
const port = process.env.TEST_PORT || '3102';
export default defineConfig({
  testDir: './tests', fullyParallel: false,
  outputDir: './artifacts/test-results',
  use: { baseURL: `http://127.0.0.1:${port}`, viewport: { width: 1440, height: 960 },
    launchOptions: { executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe' }, screenshot: 'only-on-failure' },
  webServer: { command: `node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port ${port}`, url: `http://127.0.0.1:${port}`, reuseExistingServer: !process.env.CI, timeout: 60000 },
});
