/**
 * 阶段 7:目标推理智能体的隔离 E2E。
 *
 * 用 `--script=apps/web/tests/fixtures/goal-reasoning-script.json` 起隔离栈:
 * 没有真实模型、没有真实联网,脚本化 reasoner 按轮次给出问题地图操作。
 *
 * 覆盖:进入目标自动生成问题地图(幂等)、焦点与战略取舍问题、回答后地图状态推进、
 * 战略确认走既有 proposal、未确认不写业务计划、确认后才写战略节点并出现"细化第一阶段"。
 */

import { expect, test, type Page } from '@playwright/test';
import {
  assertBackendRunning,
  createWorkspace,
  getPlan,
  registerAccount,
  waitForRealPlan,
} from './support/session';

const SCRIPTED = process.env.ZHITU_SCRIPTED_ACTIONS ?? '';
const IS_GOAL_REASONING = SCRIPTED.includes('goal-reasoning-script');

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
  test.skip(
    !IS_GOAL_REASONING,
    '这一条要 --script=apps/web/tests/fixtures/goal-reasoning-script.json 才跑得起来',
  );
});

const reasoningNode = (page: Page) => page.locator('.react-flow__node-reasoning');
const reasoningCard = (page: Page) => page.locator('.reasoning-node');
const questionCard = (page: Page) => page.locator('.canvas-question-node');

async function waitForMap(page: Page): Promise<void> {
  await expect(reasoningNode(page)).toHaveCount(4, { timeout: 25000 });
  await expect(reasoningCard(page).filter({ hasText: '目标用途' })).toBeVisible();
}

test('进入目标自动生成问题地图,重复进入不重复', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'goal-reasoning-enter');
  const workspaceId = await createWorkspace(page, token, '目标推理空间', '我想系统学习 Python');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  await waitForMap(page);
  // 恰好一个焦点,而且焦点有"为什么先处理它"。
  await expect(page.locator('.reasoning-node.is-focus')).toHaveCount(1, { timeout: 20000 });
  await expect(page.locator('.reasoning-node.is-focus')).toContainText('用途');

  // 战略阶段先问取舍 —— 不能先问每周投入。
  await expect(questionCard(page)).toHaveCount(1, { timeout: 20000 });
  const questionText = await questionCard(page).innerText();
  expect(questionText).toContain('哪条路线');
  expect(questionText, '战略层不该先问每周投入').not.toContain('每周');

  // 重复进入(刷新)不重复一级节点。
  await page.reload();
  await waitForRealPlan(page);
  await waitForMap(page);
  await expect(reasoningNode(page)).toHaveCount(4);
});

test('回答推进地图,战略确认走提案,确认后才写计划', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'goal-reasoning-confirm');
  const workspaceId = await createWorkspace(page, token, '目标推理确认空间', '我想系统学习英语');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  await waitForMap(page);

  // 在画布上回答问题 -> 地图节点应推进为已澄清,并收敛出路线。
  await questionCard(page).getByRole('button', { name: '英语' }).click();
  await questionCard(page).getByRole('button', { name: '提交回答' }).click();
  await expect(reasoningCard(page).filter({ hasText: '战略:先英语' })).toBeVisible({ timeout: 25000 });
  await expect(page.locator('.reasoning-node.reasoning-resolved')).toHaveCount(1, { timeout: 20000 });

  // 点开路线节点 -> 确认这条战略 -> 生成待确认提案。
  await reasoningCard(page).filter({ hasText: '战略:先英语' }).click();
  await expect(page.locator('.reasoning-detail')).toBeVisible();
  await page.locator('.reasoning-detail').getByRole('button', { name: '确认这条战略' }).click();

  const proposal = page.locator('.proposal').filter({ hasText: '战略:先英语' }).first();
  await expect(proposal).toBeVisible({ timeout: 20000 });

  // **未确认时业务计划里没有战略节点。**
  const beforeConfirm = await getPlan(page, token, workspaceId);
  expect(beforeConfirm.nodes.some(node => node.title === '战略:先英语')).toBe(false);

  // 确认提案 -> 战略节点真的写入计划。
  await proposal.getByRole('button', { name: '确认，写入计划' }).click();
  await expect
    .poll(async () => (await getPlan(page, token, workspaceId)).nodes.some(node => node.title === '战略:先英语'), {
      timeout: 20000,
    })
    .toBe(true);

  // 已确认战略之后才出现"细化第一阶段"入口。
  await expect(page.getByRole('button', { name: '细化第一阶段' })).toBeVisible({ timeout: 20000 });
});
