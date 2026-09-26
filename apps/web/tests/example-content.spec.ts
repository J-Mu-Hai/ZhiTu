import { expect, test } from '@playwright/test';
import { openDemoSpace, registerAccount } from './support/session';
import { workspaceStorageKey } from '../src/features/growth/workspaces';

/**
 * "示例"这两个字必须落对地方:预置内容标着它,用户自己写的东西不标。
 *
 * 这些内容只在**示例空间**里 —— 它是那份本地演示数据,现在要显式进入
 * (`?workspace=primary`),不再是"没选空间时的默认值"。见 `support/session.ts`。
 */

test('marks preset conversations as examples only', async ({ page }) => {
  await registerAccount(page, 'examples');
  await openDemoSpace(page, '/conversations');

  await expect(page.locator('.hub-item .example-badge')).toHaveCount(4);
  await page.getByRole('button', { name: /Transformer 学习/ }).click();
  await expect(page.locator('.hub-thread > header .example-badge')).toHaveText('示例');
  await expect(page.locator('.hub-messages .message .example-badge')).toHaveCount(2);

  await page.getByLabel('继续历史对话').fill('这是我的真实消息');
  await page.getByRole('button', { name: '发送历史对话' }).click();
  const personalMessage = page.locator('.hub-messages .message').filter({ hasText: '这是我的真实消息' });
  await expect(personalMessage).toBeVisible();
  await expect(personalMessage.locator('.example-badge')).toHaveCount(0);

  // 换页要重新带上空间参数:示例空间不像真实空间那样记在
  // `zhitu.active.workspace.<用户>` 里,它只认地址栏上那个参数。
  await openDemoSpace(page, '/workbench');
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

  await registerAccount(page, 'journal');
  await openDemoSpace(page, '/journal');
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

  await registerAccount(page, 'legacy-journal');
  await openDemoSpace(page, '/journal');

  /*
   * 键名不能再写死。
   *
   * 上一版写的是 `zhitu.workspace.playwright-user.v1`,那是"账户存在 localStorage 里"
   * 的时代按用户名分键的产物 —— 现在账户在后端,示例空间不属于任何账户,它用的是
   * `workspaceStorageKey('guest', 'primary')`。直接问产品自己的函数要这个键,
   * 而不是在测试里再抄一份它的拼法:抄的那份会在键名改动时安静地失效,
   * 而失效的表现是"这条测试什么也没验"(读到 null、写了个没人看的对象)。
   */
  const key = workspaceStorageKey('guest', 'primary');
  await expect.poll(() => page.evaluate((k) => localStorage.getItem(k), key)).not.toBeNull();
  await page.evaluate((k) => {
    const workspace = JSON.parse(localStorage.getItem(k) ?? '{}') as Record<string, unknown>;
    workspace.journals = [{
      id: 'legacy-personal-journal',
      content: '旧版本发布的个人随笔',
      date: '2026-09-16',
      linkedNodeIds: [],
      tags: [],
    }];
    localStorage.setItem(k, JSON.stringify(workspace));
  }, key);

  await page.reload();
  const personalEntry = page.locator('.journal-entry').filter({ hasText: '旧版本发布的个人随笔' });
  await expect(personalEntry.locator('.journal-date > span')).toHaveText(`${Number(month)}月`);
  await expect(personalEntry.locator('.journal-date > strong')).toHaveText(day ?? '');
});
