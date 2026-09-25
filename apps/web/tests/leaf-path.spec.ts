import { expect, test } from '@playwright/test';

test('four growth categories open their own leaf paths with a single click', async ({ page }) => {
  await page.goto('/workbench');

  for (const category of ['academic', 'research', 'experience', 'personal']) {
    await page.locator(`.react-flow__node[data-id="${category}"]`).click();
    await expect(page.locator('.space-breadcrumb')).toContainText(
      ({
        academic: '学业成绩',
        research: '科研能力',
        experience: '综合经历',
        personal: '个人成长',
      } as const)[category as 'academic' | 'research' | 'experience' | 'personal'],
    );
    await expect(page.locator(`.react-flow__node[data-id="${category}"] .goal`)).toBeVisible();
    await page.getByRole('button', { name: '返回上级空间' }).click();
  }
});

test('a category is the center of its path and new leaves can carry details', async ({ page }) => {
  await page.goto('/workbench');
  await page.locator('.react-flow__node[data-id="research"]').click();

  const center = page.locator('.react-flow__node[data-id="research"]');
  const existingLeaf = page.locator('.react-flow__node[data-id="explore"]');
  const centerBox = await center.boundingBox();
  const leafBox = await existingLeaf.boundingBox();
  expect(centerBox).not.toBeNull();
  expect(leafBox).not.toBeNull();
  expect(leafBox!.y).toBeGreaterThan(centerBox!.y + centerBox!.height);

  await page.getByRole('button', { name: '添加树叶', exact: true }).click();
  await expect(page.getByRole('heading', { name: '为「科研能力」添加树叶' })).toBeVisible();
  await page.getByLabel('树叶名称').fill('阅读导师最新论文');
  await page.getByLabel('树叶说明').fill('梳理研究问题、方法与可复现实验');
  await page.getByLabel('树叶类型').selectOption('capability');
  await page.getByRole('dialog').getByRole('button', { name: '添加树叶', exact: true }).click();

  const newLeaf = page.locator('.react-flow__node').filter({ hasText: '阅读导师最新论文' });
  await expect(newLeaf).toContainText('梳理研究问题、方法与可复现实验');
  await expect(page.getByRole('button', { name: '进入阅读导师最新论文空间' })).toBeVisible();
});
