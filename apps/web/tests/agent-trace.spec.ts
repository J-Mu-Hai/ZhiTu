/**
 * 本地运行记录(阶段 9)。
 *
 * ## 验什么
 *
 * 1. 诊断入口是**服务端开关**决定的:隔离栈是 `APP_ENV=development`,所以默认可见;
 * 2. 打开后看到的是**服务端真实执行边界**产生的 turn(触发来源、步骤、耗时),
 *    而不是前端自己编的一段进度;
 * 3. 它**不显示用户原文** —— 这是这一组存在的最重要一条。
 *
 * 这一条不需要脚本:rule 兜底也会真的走一轮 `agent_loop_service`,落一个 completed
 * 的轨迹。它不依赖任何模型 key。
 */

import { expect, test } from '@playwright/test';
import {
  API_BASE,
  assertBackendRunning,
  createWorkspace,
  registerAccount,
  waitForRealPlan,
} from './support/session';

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('运行记录:入口可见、状态来自服务端、不泄漏用户原文', async ({ page }) => {
  const { token } = await registerAccount(page, 'agent-trace');
  const workspaceId = await createWorkspace(page, token, '运行记录空间');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 没有问题时,标题下不应该有那条状态条。
  await expect(page.locator('.question-status-bar')).toHaveCount(0);

  // 发一句 —— 产生一个真实 turn。
  const utterance = '这是一句只属于用户的私密原文 ZEBRA-9911';
  await page.getByRole('textbox', { name: '给 AI 的消息' }).fill(utterance);
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(
    page.locator('.floating-conversation .message').filter({ hasText: 'ZEBRA-9911' }),
  ).toBeVisible({ timeout: 20000 });

  // 入口在服务端开关探测完成后出现。
  const entry = page.getByTestId('trace-entry');
  await expect(entry).toBeVisible();

  await entry.click();
  const inspector = page.getByTestId('trace-inspector');
  await expect(inspector).toBeVisible();
  await expect(inspector).toContainText('运行记录');

  // 至少有一个 turn,而且触发来源是真实的服务端记录。
  const turn = inspector.getByTestId('trace-turn').first();
  await expect(turn).toBeVisible();
  await expect(turn).toHaveAttribute('data-trigger', 'user_message');
  // 状态是服务端终态,不是"永远转圈"。
  await expect(turn).toHaveAttribute('data-status', /completed|failed|timed_out/);
  await expect(turn).toContainText('秒', { timeout: 20000 });

  // **脱敏红线**:诊断抽屉里不能出现用户的原文。
  await expect(inspector).not.toContainText('ZEBRA-9911');
  await expect(inspector).not.toContainText('私密原文');

  // Escape 关闭,不挤压画布。
  await page.keyboard.press('Escape');
  await expect(inspector).toHaveCount(0);
  // 关掉之后对话正文还在(抽屉没有把状态带走)。
  await expect(
    page.locator('.floating-conversation .message').filter({ hasText: 'ZEBRA-9911' }),
  ).toBeVisible();
});

test('运行记录展示地图轮触发来源(进入空间)', async ({ page }) => {
  const { token } = await registerAccount(page, 'agent-trace-map');
  const workspaceId = await createWorkspace(page, token, '地图轮轨迹空间');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  /*
   * 进入空间时服务端会自动跑一轮地图探索(`space_entered`,幂等)。它**不经过**
   * 普通对话循环,但必须出现在同一个抽屉里。
   *
   * 先直接轮询接口等它落库,再开抽屉 —— 否则"抽屉在轨迹创建前打开"会让它读不到,
   * 而那不是产品问题,是测试的时序。
   */
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `${API_BASE}/api/workspaces/${workspaceId}/agent/trace`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!response.ok()) return 0;
        const body = (await response.json()) as { turns: { trigger: string }[] };
        return body.turns.filter(turn => turn.trigger === 'space_entered').length;
      },
      { timeout: 20000 },
    )
    .toBeGreaterThan(0);

  const entry = page.getByTestId('trace-entry');
  await expect(entry).toBeVisible();
  await entry.click();
  const inspector = page.getByTestId('trace-inspector');
  await expect(inspector).toBeVisible();

  const mapTurn = inspector.locator('[data-testid="trace-turn"][data-trigger="space_entered"]');
  await expect(mapTurn.first()).toBeVisible();
  // 触发来源是给人看的中文标签,不是枚举值。
  await expect(mapTurn.first()).toContainText('进入空间');
  // 终态来自服务端(rule 兜底下通常是 failed/ROADMAP_INVALID,同样可读)。
  await expect(mapTurn.first()).toHaveAttribute('data-status', /completed|failed|timed_out/);
  await expect(mapTurn.first().locator('.trace-summary')).not.toHaveCount(0);
});
