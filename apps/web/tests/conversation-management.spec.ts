import { expect, test } from '@playwright/test';
import { openDemoSpace, registerAccount } from './support/session';

/*
 * 这一条验的是"自己建的第一条对话会顶掉那四条示例"。
 *
 * 那四条示例现在只在**示例空间**里 —— 新账户的空间列表是空的,示例内容要用户
 * 自己点进去看(`?workspace=primary`),不再是他一打开对话页就摆在那儿的默认值。
 * 所以这里先登录、再显式进示例空间。
 */
test('first personal conversation replaces examples and supports editable tags', async ({ page }) => {
  await registerAccount(page, 'conversations');
  await openDemoSpace(page, '/conversations');

  await expect(page.locator('.hub-item')).toHaveCount(4);
  await expect(page.locator('.hub-item .example-badge')).toHaveCount(4);

  await page.getByRole('button', { name: '新建对话' }).click();
  await page.getByLabel('对话名称').fill('期末学习安排');
  await page.getByLabel('对话标签').fill('学习计划，分数');
  await page.getByRole('dialog').getByRole('button', { name: '开始对话' }).click();

  await expect(page.locator('.hub-item')).toHaveCount(1);
  await expect(page.locator('.hub-item .example-badge')).toHaveCount(0);
  await expect(page.locator('.hub-thread h2')).toHaveText('期末学习安排');
  await expect(page.locator('.hub-thread .conversation-tag')).toHaveText(['学习计划', '分数']);

  await page.getByRole('button', { name: '编辑对话' }).click();
  await page.getByLabel('对话名称').fill('大三成绩提升');
  await page.getByLabel('对话标签').fill('课程、GPA');
  await page.getByRole('dialog').getByRole('button', { name: '保存修改' }).click();

  await expect(page.locator('.hub-thread h2')).toHaveText('大三成绩提升');
  await expect(page.locator('.hub-thread .conversation-tag')).toHaveText(['课程', 'GPA']);
  await page.reload();
  await expect(page.locator('.hub-item')).toHaveCount(1);
  await expect(page.locator('.hub-item')).toContainText('大三成绩提升');
  await expect(page.locator('.hub-item .conversation-tag')).toHaveText(['课程', 'GPA']);
});
