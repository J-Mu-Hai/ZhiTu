import { expect, test } from '@playwright/test';
import { createWorkspace, openSpacePage, registerAccount } from './support/session';

/**
 * V1 交互职责分离(真实后端)。
 *
 * 这一版把**结构化问答、候选方向与节点确认**全部收进画布节点;对话区只保留
 * 判断 / 解释、自由输入与「定位到节点」。所以这里验的不是“候选方向长什么样”,
 * 而是**同一个 interaction 不再出现在对话区**:
 *
 * 1. 对话区没有选项按钮、没有第二个输入框、没有固定交互卡;
 * 2. 至多一个画布 active node;
 * 3. 输入框始终可用。
 *
 * 候选方向由真实模型产生,非确定性,因此对“有没有候选项”不做硬断言。
 */
test.describe('V1 交互职责分离', () => {
  test.setTimeout(240_000);

  test('对话区只对话,结构化控件不出现在对话区', async ({ page }) => {
    const account = await registerAccount(page, 'v1-separation');
    const workspaceId = await createWorkspace(
      page,
      account.token,
      'V1 职责分离验收',
      '我想学 Python，但不确定用来做什么，担心学不了',
    );
    await openSpacePage(page, '/workbench', workspaceId);

    const composer = page.getByLabel('给 AI 的消息');
    await expect(composer).toBeVisible({ timeout: 30_000 });
    await composer.fill('我想学 Python，但不确定用来做什么，担心学不了');
    await expect(page.getByLabel('发送消息')).toBeEnabled();
    await page.getByLabel('发送消息').click();

    // 有判断 / 有节点 / 有位置提示 —— 任一先到即可,不硬等某一个。
    await expect(
      page.getByTestId('v1-thesis')
        .or(page.locator('.canvas-question-node'))
        .or(page.getByTestId('chat-action-notice')),
    ).toBeVisible({ timeout: 180_000 });

    // 对话区**不允许**出现结构化交互。
    await expect(page.locator('.floating-conversation [data-testid="current-interaction-card"]')).toHaveCount(0);
    await expect(page.locator('.floating-conversation .cq-direction')).toHaveCount(0);
    await expect(page.locator('.floating-conversation .v1-alignment-options')).toHaveCount(0);
    await expect(page.locator('.floating-conversation [data-testid="cq-interaction"]')).toHaveCount(0);

    // 位置提示只给文字 + 定位,没有选项 / 第二个输入框。
    const notice = page.getByTestId('chat-action-notice');
    if (await notice.count() > 0) {
      await expect(notice).toContainText('节点');
      await expect(notice.getByRole('button', { name: '定位到节点' })).toBeVisible();
    }

    // 任意时刻至多一个 active node。
    const activeNodes = page.locator('.canvas-question-node.is-active');
    expect(await activeNodes.count()).toBeLessThanOrEqual(1);

    // 输入框始终可见、可继续自由输入。
    await expect(composer).toBeVisible();
    await composer.fill('我补充一点：我更想做能展示的小工具');
    await expect(composer).toHaveValue('我补充一点：我更想做能展示的小工具');
    await expect(page.getByLabel('发送消息')).toBeEnabled();
  });

  test('active 节点的结构化动作只在该节点里出现', async ({ page }) => {
    const account = await registerAccount(page, 'v1-node-action');
    const workspaceId = await createWorkspace(
      page,
      account.token,
      'V1 节点动作验收',
      '我想学 Python，但不确定用来做什么，担心学不了',
    );
    await openSpacePage(page, '/workbench', workspaceId);

    const composer = page.getByLabel('给 AI 的消息');
    await expect(composer).toBeVisible({ timeout: 30_000 });
    await composer.fill('我想学 Python，但不确定用来做什么，担心学不了');
    await page.getByLabel('发送消息').click();

    const activeNode = page.locator('.canvas-question-node.is-active');
    await expect(activeNode).toHaveCount(1, { timeout: 180_000 });

    // 点开 active 节点后,结构化控件出现在节点里(而不是对话区)。
    await activeNode.click();
    const nodeControls = activeNode.locator('[data-testid="cq-interaction"], [data-testid="cq-flow-action"]');
    if (await nodeControls.count() > 0) {
      await expect(nodeControls.first()).toBeVisible();
      await expect(page.locator('.floating-conversation [data-testid="cq-interaction"]')).toHaveCount(0);
    }

    // 对话区仍然可自由输入。
    await expect(composer).toBeVisible();
    await composer.fill('继续补充');
    await expect(composer).toHaveValue('继续补充');
  });
});
