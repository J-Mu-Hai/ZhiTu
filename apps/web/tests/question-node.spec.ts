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

/*
 * **只在自己那份脚本被加载时才跑。** 隔离验收一次只能配一份 `--script`;
 * 如果这里只写 `!SCRIPTED`,那么用别的脚本(例如 goal-reasoning)起栈时,
 * 这一组会“跑起来”却拿不到它要的对话内容 —— 失败看起来像产品坏了。
 * 与 `strategy-layer.spec.ts` 同一条纪律。
 */
const IS_QUESTION_SCRIPT = SCRIPTED.includes('question-script');

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
  test.skip(
    !IS_QUESTION_SCRIPT,
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

  // 紧凑卡:默认宽度约 190–220px,而且“稍后回答/跳过/补充输入”收进“更多”。
  const cardBox = await questionCard(page).boundingBox();
  expect(cardBox, '问题卡没有尺寸').not.toBeNull();
  expect(cardBox!.width, `问题卡太宽了:${cardBox!.width}`).toBeLessThanOrEqual(220);
  expect(cardBox!.width).toBeGreaterThanOrEqual(150);
  await expect(questionCard(page).getByRole('button', { name: '稍后回答' })).toHaveCount(0);
  await questionCard(page).getByRole('button', { name: '更多' }).click();
  await expect(questionCard(page).getByRole('button', { name: '稍后回答' })).toBeVisible();

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

  await questionCard(page).getByRole('button', { name: '更多' }).click();
  await questionCard(page).getByRole('button', { name: '稍后回答' }).click();
  await expect(questionNode(page)).toHaveCount(1);

  await page.reload();
  await waitForRealPlan(page);
  await expect(questionNode(page)).toHaveCount(1, { timeout: 20000 });
  await expect(questionCard(page)).toContainText('这学期你希望把重心放在哪一边?');
});

/** 问题节点在当前流坐标系里的 transform(与视口平移缩放无关)。 */
async function questionTransform(page: Page): Promise<string> {
  return (await questionNode(page).getAttribute('style')) ?? '';
}

test('问题节点可自由拖动,锚定虚线跟随,刷新后位置恢复', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'canvas-question-drag');
  const workspaceId = await createWorkspace(page, token, '画布问题拖动空间', '提升英语');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  await say(page, '我想提升一下。');
  await expect(questionNode(page)).toHaveCount(1, { timeout: 20000 });
  await expect(page.locator('.react-flow__edge.question-anchor-edge')).toHaveCount(1);

  const draggedBefore = await questionTransform(page);
  const box = await questionNode(page).boundingBox();
  if (!box) throw new Error('问题节点没有边界框,拖不动');
  // 从卡片顶部空白处(徽标那一条,不是任何按钮)起手拖动。
  await page.mouse.move(box.x + 40, box.y + 14);
  await page.mouse.down();
  await page.mouse.move(box.x + 40 + 180, box.y + 14 + 120, { steps: 12 });
  await page.mouse.up();

  // 位置真的变了,而且锚定虚线仍然只有一条(拖动不产生业务关系)。
  await expect.poll(() => questionTransform(page)).not.toBe(draggedBefore);
  const draggedTo = await questionTransform(page);
  await expect(page.locator('.react-flow__edge.question-anchor-edge')).toHaveCount(1);
  const rel = await getPlan(page, token, workspaceId);
  expect(rel.relations).toHaveLength(0);

  // 刷新:位置由 UI-only 表恢复,状态与锚定边也回来。
  await page.reload();
  await waitForRealPlan(page);
  await expect(questionNode(page)).toHaveCount(1, { timeout: 20000 });
  await expect(page.locator('.react-flow__edge.question-anchor-edge')).toHaveCount(1);
  await expect.poll(() => questionTransform(page)).toBe(draggedTo);
});

test('画布静止时问题节点位置稳定、不被反复重挂载', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'canvas-question-stable');
  const workspaceId = await createWorkspace(page, token, '画布问题稳定空间', '提升英语');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  await say(page, '我想提升一下。');
  await expect(questionNode(page)).toHaveCount(1, { timeout: 20000 });
  // 等测量与初始定位都安静下来。
  await page.waitForTimeout(600);

  // 在 DOM 元素上留一个印记:元素被卸载重挂载时印记会消失(React Flow
  // 重建 wrapper 时不会保留它)。
  await questionNode(page).evaluate((element) => {
    (element as unknown as Record<string, unknown>).__zhituStableMark = true;
  });

  const samples: string[] = [];
  for (let i = 0; i < 12; i += 1) {
    await page.waitForTimeout(250);
    samples.push(await questionNode(page).evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return `${Math.round(rect.x)},${Math.round(rect.y)},${Math.round(rect.width)}x${Math.round(rect.height)}`;
    }));
  }

  const unique = [...new Set(samples)];
  expect(unique, `问题节点在静止时持续变化(闪烁):\n${unique.join('\n')}`).toHaveLength(1);
  expect(
    await questionNode(page).evaluate(
      (element) => (element as unknown as Record<string, unknown>).__zhituStableMark === true,
    ),
    '问题节点被卸载/重挂载了',
  ).toBe(true);
});
