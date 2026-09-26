import { expect, test } from '@playwright/test';
import { openDemoSpace, registerAccount } from './support/session';

test('conversation cards reserve planning space and stay inside the viewport', async ({ page }) => {
  // 工作台需要一个空间。这份布局验收看的是"对话卡片占位之后画布还剩多少",
  // 所以进的是那份节点最多的示例空间 —— 空空间和新空间都只有根目标一个节点,
  // 画不满画布,量出来的宽度说明不了问题。
  await registerAccount(page, 'layout2');
  await openDemoSpace(page, '/workbench');
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
  /*
   * **不能点完马上量。**
   *
   * `.workspace` 的右内边距带 200ms 过渡(见 demo3-repair.css 的 `transition:padding .2s`),
   * 而 `toBeHidden()` 在 `hidden` 属性写上那一刻就通过了 —— 那时过渡才刚开始,
   * 量到的宽度还是收起之前那一帧的值,于是"变宽了"这条断言永远不成立。这是量早了一步,
   * 不是画布真的没让出空间。等它走到稳态再量,量到的仍然是真实宽度。
   * (下面展开那一半本来就是用 `expect.poll` 写的,同一个理由。)
   */
  await expect
    .poll(async () => (await canvas.boundingBox())!.width, { message: '收起对话后画布没有变宽' })
    .toBeGreaterThan(openCanvas!.width);

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
