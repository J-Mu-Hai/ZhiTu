import { expect, test } from '@playwright/test';

test('marks preset conversations as examples only', async ({ page }) => {
  await page.goto('/conversations');

  await expect(page.locator('.hub-item .example-badge')).toHaveCount(4);
  await page.getByRole('button', { name: /Transformer 学习/ }).click();
  await expect(page.locator('.hub-thread > header .example-badge')).toHaveText('示例');
  await expect(page.locator('.hub-messages .message .example-badge')).toHaveCount(2);

  await page.getByLabel('继续历史对话').fill('这是我的真实消息');
  await page.getByRole('button', { name: '发送历史对话' }).click();
  const personalMessage = page.locator('.hub-messages .message').filter({ hasText: '这是我的真实消息' });
  await expect(personalMessage).toBeVisible();
  await expect(personalMessage.locator('.example-badge')).toHaveCount(0);

  await page.goto('/workbench');
  await expect(page.locator('.floating-conversation .message .example-badge')).toHaveCount(4);
  await expect(page.locator('.floating-conversation .message .example-badge').first()).toHaveText('示例');
  await page.getByRole('textbox', { name: '给 AI 的消息' }).fill('这是工作台里的个人消息');
  await page.getByRole('button', { name: '发送消息' }).click();
  const workspaceMessage = page.locator('.floating-conversation .message').filter({ hasText: '这是工作台里的个人消息' });
  await expect(workspaceMessage.locator('.example-badge')).toHaveCount(0);
});

test('uses Beijing date and hides journal examples after personal publishing', async ({ page }) => {
  const dateParts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date());
  const month = dateParts.find(part => part.type === 'month')?.value;
  const day = dateParts.find(part => part.type === 'day')?.value;

  await page.goto('/journal');
  await expect(page.locator('.journal-entry')).toHaveCount(2);
  await expect(page.locator('.journal-entry .example-badge')).toHaveCount(2);
  await expect(page.locator('.journal-entry .example-badge').first()).toHaveText('示例');

  await page.getByLabel('此刻的想法').fill('这是我的个人记录');
  await page.getByRole('button', { name: '发布', exact: true }).click();

  const personalEntry = page.locator('.journal-entry').filter({ hasText: '这是我的个人记录' });
  await expect(page.locator('.journal-entry')).toHaveCount(1);
  await expect(personalEntry).toBeVisible();
  await expect(personalEntry.locator('.example-badge')).toHaveCount(0);
  await expect(personalEntry.locator('.journal-date > span')).toHaveText(`${Number(month)}月`);
  await expect(personalEntry.locator('.journal-date > strong')).toHaveText(day ?? '');

  await page.reload();
  await expect(page.locator('.journal-entry')).toHaveCount(1);
  await expect(page.locator('.journal-entry .example-badge')).toHaveCount(0);
});

test('migrates personal journals published with the old fixed demo date', async ({ page }) => {
  const dateParts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date());
  const month = dateParts.find(part => part.type === 'month')?.value;
  const day = dateParts.find(part => part.type === 'day')?.value;

  await page.goto('/journal');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('zhitu.workspace.playwright-user.v1'))).not.toBeNull();
  await page.evaluate(() => {
    const key = 'zhitu.workspace.playwright-user.v1';
    const workspace = JSON.parse(localStorage.getItem(key) ?? '{}');
    workspace.journals = [{
      id: 'legacy-personal-journal',
      content: '旧版本发布的个人随笔',
      date: '2026-09-16',
      linkedNodeIds: [],
      tags: [],
    }];
    localStorage.setItem(key, JSON.stringify(workspace));
  });

  await page.reload();
  const personalEntry = page.locator('.journal-entry').filter({ hasText: '旧版本发布的个人随笔' });
  await expect(personalEntry.locator('.journal-date > span')).toHaveText(`${Number(month)}月`);
  await expect(personalEntry.locator('.journal-date > strong')).toHaveText(day ?? '');
});
