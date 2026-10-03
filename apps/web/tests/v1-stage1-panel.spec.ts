import { expect, test } from '@playwright/test';
import { createWorkspace, openSpacePage, registerAccount } from './support/session';

/**
 * P2.2.1:Stage 1“思考模式”的右侧面板可用性。
 *
 * 真实场景:战略判断 + 候选方向 + 输入框同时在场。要求面板更宽、消息/行动区独立
 * 滚动、候选项可点、输入框始终可见。
 *
 * 说明:本用例跑在**真实 V1 后端**上(候选方向由模型产生,非确定性)。因此对
 * “是否有候选方向”做容错:没有候选时仍验证布局与输入可用性,有候选时额外验证
 * 点击闭环。
 */
test.describe('V1 Stage 1 右侧面板', () => {
  test.setTimeout(240_000);

  test('面板加宽、可滚动、候选方向可点、输入框始终可见', async ({ page }) => {
    const account = await registerAccount(page, 'v1-panel');
    const workspaceId = await createWorkspace(
      page,
      account.token,
      'V1 面板验收',
      '我想学 Python，但不确定用来做什么，担心学不了',
    );
    await openSpacePage(page, '/workbench', workspaceId);

    const composer = page.getByLabel('给 AI 的消息');
    await expect(composer).toBeVisible({ timeout: 30_000 });

    // 首屏:输入框可见、可输入。
    await composer.fill('我想学 Python，但不确定用来做什么，担心学不了');
    await page.getByLabel('发送消息').click();

    // 战略判断出现。
    await expect(page.getByTestId('v1-thesis')).toBeVisible({ timeout: 120_000 });

    // 面板加宽(桌面端 >= 560)。
    const overlay = await page.locator('.conversation-overlay').boundingBox();
    expect(overlay?.width ?? 0).toBeGreaterThanOrEqual(560);

    // 消息 / 行动区是独立滚动容器。
    const scrollable = await page
      .locator('.conversation-history')
      .evaluate((el) => getComputedStyle(el).overflowY);
    expect(['auto', 'scroll']).toContain(scrollable);

    // 输入框没有被内容顶出视口。
    const composerBox = await composer.boundingBox();
    const viewport = page.viewportSize();
    expect(composerBox).not.toBeNull();
    expect((composerBox?.y ?? 0) + (composerBox?.height ?? 0)).toBeLessThanOrEqual(
      (viewport?.height ?? 0) + 1,
    );

    // 触发一次低信息回答,让服务端倾向给出候选方向。
    await composer.fill('不知道');
    await page.getByLabel('发送消息').click();

    const directions = page.locator('[data-testid="v1-directions"] button');
    const count = await directions.count().catch(() => 0);
    if (count > 0) {
      const target = directions.nth(Math.min(2, count - 1));
      const before = (await page.getByTestId('v1-thesis').innerText()).trim();
      await target.click();

      // 点击不被遮挡:Playwright 的 click 本身会因拦截而失败。这里再确认输入框仍可用。
      await composer.fill('补充一句：我更想做能展示的小工具');
      await composer.fill('');
      await expect(composer).toBeVisible();

      // 选择后战略判断会更新(允许同文时退化为仍可见)。
      await expect(page.getByTestId('v1-thesis')).toBeVisible();
      const after = (await page.getByTestId('v1-thesis').innerText()).trim();
      expect(after.length).toBeGreaterThan(0);
      void before;

      // 选择过程结束后候选按钮恢复可用。
      await expect(directions.nth(0)).toBeEnabled({ timeout: 120_000 });
    }

    // 输入框始终可见且可继续发送。
    await expect(composer).toBeVisible();
    await composer.fill('我继续补充一点');
    await expect(composer).toHaveValue('我继续补充一点');
  });

  test('确认目标定义后自动形成战略路径或给出显式 CTA', async ({ page }) => {
    const account = await registerAccount(page, 'v1-advance');
    const workspaceId = await createWorkspace(
      page,
      account.token,
      'V1 自动推进验收',
      '我想学 Python，但不确定用来做什么，担心学不了',
    );
    await openSpacePage(page, '/workbench', workspaceId);

    const composer = page.getByLabel('给 AI 的消息');
    await expect(composer).toBeVisible({ timeout: 30_000 });
    await composer.fill('我想学 Python，但不确定用来做什么，担心学不了');
    await page.getByLabel('发送消息').click();
    await expect(page.getByTestId('v1-thesis')).toBeVisible({ timeout: 120_000 });

    // 若有候选方向,先选一个(形成目标定义)。
    const directions = page.locator('[data-testid="v1-directions"] button');
    if ((await directions.count()) > 0) await directions.first().click();

    const confirmBtn = page.getByRole('button', { name: '确认这个目标定义' });
    await expect(confirmBtn).toBeVisible({ timeout: 120_000 });
    await confirmBtn.click();

    // 请求中显示“正在形成战略路径”。
    await expect(page.getByText('正在形成战略路径…')).toBeVisible({ timeout: 8_000 });

    // 关键:不会停在空白的 problem_structure —— 要么出战略草案,要么有显式 CTA。
    await expect(
      page.getByTestId('v1-strategy').or(page.getByRole('button', { name: '继续形成战略路径' })),
    ).toBeVisible({ timeout: 180_000 });

    // 输入框仍然可用。
    await expect(composer).toBeVisible();
    await composer.fill('继续补充');
    await expect(composer).toHaveValue('继续补充');
  });
});
