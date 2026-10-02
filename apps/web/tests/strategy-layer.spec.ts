/**
 * 战略层与分层规划的前端闭环。
 *
 * 验的是产品行为,不是模型的判断(脚本是测试脚手架,后端会记 `model_source=scripted`):
 *
 * 1. 节点卡 / 提案卡显示 planningLevel 标签;战略提案展示优先项、暂缓项、依据与风险;
 * 2. 战略未确认时,界面提示"先确认战略方向",不允许假装已有可执行日程;
 * 3. 确认战略 -> 刷新页面后层级状态保持;
 * 4. 确认战略后才允许下一层(周)。
 *
 * 没有脚本时整组 `skip`(与 `interview-loop.spec.ts` 同一条纪律)。
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

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
  test.skip(
    !SCRIPTED,
    '这一条要 --script=apps/web/tests/fixtures/strategy-script.json 才跑得起来',
  );
});

async function say(page: Page, text: string): Promise<void> {
  const replies = page.locator('.message.assistant');
  const before = await replies.count();
  await page.getByLabel('给 AI 的消息').fill(text);
  await page.getByLabel('发送消息').click();
  await expect(replies, '这一轮没有回复 —— 后端可能没在 script 模式里').toHaveCount(before + 1, {
    timeout: 20000,
  });
}

test('战略先确认,再往下分层;层级标签可见且刷新保持', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'strategy');
  const workspaceId = await createWorkspace(page, token, '战略验收空间', '提升英语和数学');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 战略未确认:界面明确提示先确认方向。
  await expect(page.locator('.strategy-hint')).toBeVisible();

  // ---- 第一轮:战略提议 ----
  await say(page, '帮我定个长期方向。');
  const proposal = page.locator('.proposal').last();
  await expect(proposal).toBeVisible({ timeout: 20000 });
  await expect(proposal).toContainText('建立战略选择');
  // 战略 proposal 卡要能读到优先项 / 暂缓项 / 依据 / 风险(模型写在 description 里)。
  await expect(proposal).toContainText('优先');
  await expect(proposal).toContainText('暂缓');
  await expect(proposal).toContainText('依据');
  await expect(proposal).toContainText('风险');

  // 确认前:画布上没有战略节点。
  const before = await getPlan(page, token, workspaceId);
  expect(before.nodes.some(node => node.planningLevel === 'strategy')).toBe(false);

  await proposal.getByRole('button', { name: '确认，写入计划' }).click();
  await expect
    .poll(
      async () => (await getPlan(page, token, workspaceId)).nodes.some(n => n.planningLevel === 'strategy'),
      { message: '确认之后画布上还是没有战略节点', timeout: 15000 },
    )
    .toBe(true);

  // 节点卡上出现"战略层"标签。
  const strategyNode = page.locator('.react-flow__node').filter({ hasText: '战略:先英语' }).first();
  await expect(strategyNode.locator('.node-level-strategy')).toContainText('战略层');
  // 战略已确认,提示消失。
  await expect(page.locator('.strategy-hint')).toHaveCount(0);

  // ---- 刷新:层级状态从后端恢复 ----
  await page.reload();
  await waitForRealPlan(page);
  await expect(
    page.locator('.react-flow__node').filter({ hasText: '战略:先英语' }).first().locator('.node-level-strategy'),
  ).toContainText('战略层');

  // ---- 确认战略之后,才允许下一层(周) ----
  await say(page, '那这一周先做什么?');
  const weekProposal = page.locator('.proposal').last();
  await expect(weekProposal).toBeVisible({ timeout: 20000 });
  await expect(weekProposal).toContainText('本周重点');
  await weekProposal.getByRole('button', { name: '确认，写入计划' }).click();
  await expect
    .poll(
      async () => (await getPlan(page, token, workspaceId)).nodes.some(n => n.planningLevel === 'week'),
      { message: '确认之后还是没有周层级节点', timeout: 15000 },
    )
    .toBe(true);
  // 周节点是战略的子节点,要进入战略这一层才看得见它的卡片。
  await page
    .locator('.react-flow__node')
    .filter({ hasText: '战略:先英语' })
    .first()
    .getByLabel(/进入.*空间/)
    .click();
  await expect(
    page.locator('.react-flow__node').filter({ hasText: '本周重点:精读两篇' }).first().locator('.node-level-week'),
  ).toContainText('周重点');
});
