import { test, expect } from '@playwright/test';

test('shared growth state, canvas context, timeline drag and accepted proposal', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  await expect(page).toHaveURL(/workbench/);
  await page.locator('.react-flow__node[data-id="research"]').click();
  await expect(page.getByRole('textbox', { name: '给 AI 的消息' })).toHaveAttribute('placeholder', '关于「科研能力」，告诉 AI 你的想法……');
  await page.screenshot({ path: 'artifacts/path.png' });
  await page.getByRole('tab', { name: '时间线' }).click();
  await expect(page.getByTestId('timeline-canvas')).toHaveAttribute('data-ready', 'true');
  if (!await page.locator('[data-timeline-item="project"]').count()) {
    await page.getByRole('button', { name: /另有.*展开/ }).click();
    await page.locator('[data-cluster-panel]').getByRole('button', { name: /科研项目/ }).click();
  }
  const bar = page.locator('[data-timeline-item="project"] [data-timeline-card]');
  const box = await bar.boundingBox();
  if (!box) throw new Error('Missing project card');
  const density = Number(await page.getByTestId('timeline-canvas').getAttribute('data-density'));
  // Move the same shared task 45 days, from October 18 to December 2.
  await page.mouse.move(box.x + 12, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 12 + 45 * density, box.y + box.height / 2, { steps: 12 });
  await page.mouse.up();
  await expect(page.getByRole('button', { name: '接受调整' })).toBeVisible();
  await page.getByRole('button', { name: '查看影响' }).click();
  await expect(page.getByTestId('plan-ghost')).toHaveCount(1);
  await expect(page.getByTestId('date-inspector')).toContainText('2026-12-02');
  await expect(page.locator('[data-timeline-item="project"]')).toHaveAttribute('data-start-date', '2027-01-18');
  await page.getByRole('button', { name: '接受调整' }).click();
  await expect(page.getByTestId('date-inspector')).toContainText('2027-01-18');
  await expect(page.getByTestId('plan-ghost')).toHaveCount(0);
  await page.screenshot({ path: 'artifacts/timeline.png' });
  await page.getByRole('tab', { name: '任务', exact: true }).click();
  await page.getByRole('button', { name: '完成科研项目', exact: true }).click();
  await page.getByRole('button', { name: '今天', exact: true }).click();
  await expect(page.locator('.task-detail')).toHaveCount(4);
  await page.getByRole('button', { name: '联系导师', exact: false }).last().click();
  await expect(page.getByRole('textbox', { name: '给 AI 的消息' })).toHaveAttribute('placeholder', '关于「联系导师」，告诉 AI 你的想法……');
  await page.getByRole('textbox', { name: '给 AI 的消息' }).fill('我想先整理导师资料');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.locator('.message').last()).toContainText('联系导师');
  await page.screenshot({ path: 'artifacts/tasks.png' });
  await page.getByRole('tab', { name: '路径', exact: true }).click();
  await expect(page.locator('.react-flow__node[data-id="project"] .growth-node')).toHaveClass(/is-complete/);
  for (const name of ['首页','随笔','对话','我的']) {
    await page.getByRole('navigation').getByRole('link', { name, exact: true }).click();
    await expect(page.locator('.editorial-page, .conversation-hub')).toBeVisible();
  }
  await page.getByRole('navigation').getByRole('link', { name: /工作台/ }).click();
  await expect(page.locator('.react-flow__node[data-id="project"] .growth-node')).toHaveClass(/is-complete/);
  expect(errors).toEqual([]);
});
