/**
 * 画布问题节点闭环:agent_questions 的待回答问题 -> 左侧 React Flow 节点 ->
 * 在画布上回答 -> 状态同步;右侧对话区只留状态提示与定位。
 *
 * ## 这一条为什么需要脚本
 *
 * 隔离验收栈里没有模型 key,兜底规则永远返回"没有动作、没有问题"。所以这条链路在
 * 真实模型之外只能靠 `ZHITU_SCRIPTED_ACTIONS` 走一遍 —— 它是**测试脚手架**
 * (库里那一轮 `model_source = 'scripted'`,来源徽标写「脚本回放」)。
 *
 * 没有脚本时整组 `skip`(与 `interview-loop.spec.ts` 同一条纪律):一句"测试没跑"
 * 看得见,而"跑过了、绿了、其实什么都没验"看不见。
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
    '这一条要 --script=apps/web/tests/fixtures/question-script.json 才跑得起来',
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

const questionNode = (page: Page) => page.locator('.react-flow__node-question');
const questionCard = (page: Page) => page.locator('.canvas-question-node');

test('问题投影为画布节点,在画布上回答,刷新后消失', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'canvas-question');
  const workspaceId = await createWorkspace(page, token, '画布问题空间', '提升英语和数学');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 选一个节点,让问题带上 source —— 画布节点应锚定在它旁边。
  const plan = await getPlan(page, token, workspaceId);
  const root = plan.nodes.find(node => node.parentId === null)!;
  await page.locator(`.react-flow__node[data-id="${root.id}"]`).click();
  await expect(page.locator('.context-chip')).toContainText(root.title);
  await page.getByRole('button', { name: '关闭弹窗' }).click();

  await say(page, '我想提升一下,但不知道先抓哪边。');

  // ---- 左侧画布出现问题节点 + 仅 UI 的锚定虚线 ----
  await expect(questionNode(page)).toHaveCount(1, { timeout: 20000 });
  await expect(questionCard(page)).toContainText('这学期你希望把重心放在哪一边?');
  await expect(page.locator('.react-flow__edge.question-anchor-edge')).toHaveCount(1);

  // 锚定虚线**不是**业务关系:正式 plan 的 relations 仍为空。
  const rel = await getPlan(page, token, workspaceId);
  expect(rel.relations).toHaveLength(0);

  // ---- 右侧对话区降级:只有状态提示 + 定位,没有第二份可提交控件 ----
  const hint = page.locator('.question-hint');
  await expect(hint).toBeVisible();
  await expect(hint.getByRole('button', { name: '提交回答' })).toHaveCount(0);
  await expect(hint.locator('.cq-option')).toHaveCount(0);

  // ---- 定位:点右侧提示,画布节点被选中/聚焦 ----
  await hint.getByRole('button', { name: '定位到画布' }).click();
  await expect(page.locator('.canvas-question-node.is-focused')).toHaveCount(1);

  // ---- 在画布节点上选择并提交 ----
  await questionCard(page).getByRole('button', { name: '英语' }).click();
  await questionCard(page).getByRole('button', { name: '提交回答' }).click();

  // 提交后状态变化(处理中),并且后续提案出现。
  const proposal = page.locator('.proposal').filter({ hasText: '这学期重心:英语' }).first();
  await expect(proposal).toBeVisible({ timeout: 20000 });
  const beforeConfirm = await getPlan(page, token, workspaceId);
  expect(beforeConfirm.nodes.some(node => node.title === '这学期重心:英语')).toBe(false);

  // ---- 刷新:resolved 的问题不再出现在画布(状态由后端恢复) ----
  await page.reload();
  await waitForRealPlan(page);
  await expect(questionNode(page)).toHaveCount(0);
});

test('无 source 的问题仍稳定显示;稍后回答刷新后仍在', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'canvas-question-later');
  const workspaceId = await createWorkspace(page, token, '画布问题稍后空间', '提升英语');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 不选任何节点 -> 问题没有 source,应锚定到空间根,而不是丢失。
  await say(page, '我想提升一下。');
  await expect(questionNode(page)).toHaveCount(1, { timeout: 20000 });

  await questionCard(page).getByRole('button', { name: '稍后回答' }).click();
  await expect(questionNode(page)).toHaveCount(1);

  await page.reload();
  await waitForRealPlan(page);
  await expect(questionNode(page)).toHaveCount(1, { timeout: 20000 });
  await expect(questionCard(page)).toContainText('这学期你希望把重心放在哪一边?');
});
