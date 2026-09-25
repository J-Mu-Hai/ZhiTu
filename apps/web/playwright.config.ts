import { existsSync } from 'node:fs';
import { defineConfig } from '@playwright/test';
const port = process.env.TEST_PORT || '3104';
const testAccount = {
  id: 'playwright-user',
  name: '同路人',
  email: 'demo@zhitu.local',
  passwordDigest: 'test-only',
  school: '知途大学',
  major: '人工智能',
  year: '大二',
  rank: 30,
  targetYear: 2027,
  targetGoal: '保研',
  bio: '把遥远的目标，变成今天可以迈出的一小步。',
  createdAt: '2026-09-16T00:00:00.000Z',
};
const localBrowser = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
].find((path): path is string => Boolean(path && existsSync(path)));
export default defineConfig({
  testDir: './tests', fullyParallel: false,
  outputDir: './artifacts/test-results',
  use: { baseURL: `http://127.0.0.1:${port}`, viewport: { width: 1440, height: 960 },
    storageState: { cookies: [], origins: [{ origin: `http://127.0.0.1:${port}`, localStorage: [
      { name: 'zhitu.auth.accounts.v1', value: JSON.stringify([testAccount]) },
      { name: 'zhitu.auth.session.v1', value: testAccount.id },
    ] }] },
    launchOptions: localBrowser ? { executablePath: localBrowser } : undefined, screenshot: 'only-on-failure' },
  webServer: { command: `node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port ${port}`, url: `http://127.0.0.1:${port}`, reuseExistingServer: !process.env.CI, timeout: 60000 },
});
