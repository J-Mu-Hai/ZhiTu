import { expect, test } from '@playwright/test';

test('first personal conversation replaces examples and supports editable tags', async ({ page }) => {
  await page.goto('/conversations');
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
