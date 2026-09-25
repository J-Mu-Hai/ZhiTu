import { expect, test } from '@playwright/test';

test('conversation cards reserve planning space and stay inside the viewport', async ({ page }) => {
  await page.goto('/workbench');
  await expect(page.locator('.react-flow__node').first()).toBeVisible();

  const navigation = await page.locator('.top-navigation').boundingBox();
  expect(navigation!.height).toBeLessThanOrEqual(56);
  await expect(page.locator('.sidebar')).toHaveCount(0);

  const canvas = page.locator('.react-flow');
  const panel = page.getByRole('region', { name: '浮动对话卡片群' });
  const openCanvas = await canvas.boundingBox();
  expect(openCanvas!.width).toBeGreaterThan(700);
  expect(openCanvas!.width).toBeLessThan(1440);
  await expect(panel).toBeVisible();

  await page.getByRole('button', { name: '让对话内容消失' }).click();
  await expect(panel).toBeHidden();
  const closedCanvas = await canvas.boundingBox();
  expect(closedCanvas!.width).toBeGreaterThan(openCanvas!.width);

  await page.getByRole('button', { name: '展开对话' }).click();
  await expect(panel).toBeVisible();
  await expect.poll(async () => (await canvas.boundingBox())!.width).toBeCloseTo(openCanvas!.width, 0);

  await page.setViewportSize({ width: 390, height: 700 });
  const mobile = await panel.boundingBox();
  expect(mobile!.width).toBeLessThanOrEqual(390);
  expect(mobile!.x).toBeGreaterThanOrEqual(0);
  expect(mobile!.y + mobile!.height).toBeLessThanOrEqual(700);
  await page.screenshot({ path: 'artifacts/conversation-responsive.png' });
});
