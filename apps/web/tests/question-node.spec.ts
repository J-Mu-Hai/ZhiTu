/**
 * 问题节点的前端闭环:问题卡出现 -> 选择/自由输入 -> 提交 -> 刷新恢复 / 稍后仍可见。
 *
 * ## 这一条为什么需要脚本
 *
 * 隔离验收栈里没有模型 key,兜底规则永远返回"没有动作、没有问题"。所以问题卡这条
 * 链路在真实模型之外只能靠 `ZHITU_SCRIPTED_ACTIONS` 走一遍 —— 它是**测试脚手架**
 * (库里那一轮会记着 `model_source = 'scripted'`,来源徽标写「脚本回放」)。
 *
 * ## 它钉的是"问题卡"这件事,不是模型的判断
 *
 * 断言的是产品输出:卡片出现、与源节点有关联、选择后提交、后续提案仍然要用户确认、
 * 刷新之后状态从后端恢复、稍后回答的问题仍在。模型该不该问这个问题,是真实模型
 * 那一层的事。
 *
 * 没有脚本时整组 `skip` —— 一句"测试没跑"看得见,而一条"跑过了、绿了、其实什么都没验"
 * 看不见(与 `interview-loop.spec.ts` 同一条纪律)。
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

/** 发一句话并等这一轮真的结束(判据同 `interview-loop.spec.ts`:回复多了一条)。 */
async function say(page: Page, text: string): Promise<void> {
  const replies = page.locator('.message.assistant');
  const before = await replies.count();
  await page.getByLabel('给 AI 的消息').fill(text);
  await page.getByLabel('发送消息').click();
  await expect(replies, '这一轮没有回复 —— 后端可能没在 script 模式里').toHaveCount(before + 1, {
    timeout: 20000,
  });
}

test('问题卡出现、选择后提交、刷新后状态从后端恢复', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'question');
  const workspaceId = await createWorkspace(page, token, '问题验收空间', '提升英语和数学');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 先在画布上选一个节点,让这一轮带上下文 —— 问题卡要能显示它跟哪个节点有关。
  const plan = await getPlan(page, token, workspaceId);
  const root = plan.nodes.find(node => node.parentId === null)!;
  expect(root, '空空间里应该有根目标').toBeTruthy();
  await page.locator(`.react-flow__node[data-id="${root.id}"]`).click();
  await expect(page.locator('.context-chip')).toContainText(root.title);
  // 点节点会打开详情弹窗 —— 关掉它,否则弹窗会挡住输入区。
  await page.getByRole('button', { name: '关闭弹窗' }).click();
  await expect(page.locator('.context-chip')).toContainText(root.title);

  await say(page, '我想提升一下,但不知道先抓哪边。');

  // ---- 问题卡出现,并且看得出它跟哪个节点有关(源节点关联可见 + 可定位) ----
  const card = page.locator('.question-card');
  await expect(card).toBeVisible({ timeout: 20000 });
  await expect(card).toContainText('这学期你希望把重心放在哪一边?');
  await expect(card).toContainText('它决定我先拆英语还是先拆数学');
  await expect(card).toContainText(root.title);

  // 先清掉上下文,再点问题卡上的“定位到画布” —— 它必须把焦点拉回源节点。
  await page.locator('.context-chip button[aria-label="清除上下文"]').click();
  await expect(page.locator('.context-hint')).toBeVisible();
  await card.getByRole('button', { name: /定位到画布/ }).click();
  await expect(page.locator('.context-chip')).toContainText(root.title);

  // ---- 选择“英语”并提交 ----
  await card.getByRole('button', { name: '英语' }).click();
  await card.getByRole('button', { name: '提交回答' }).click();

  // ---- 回答之后的后续提案仍然要用户确认(不绕过确认直接写计划) ----
  const proposal = page.locator('.proposal').filter({ hasText: '这学期重心:英语' }).first();
  await expect(proposal).toBeVisible({ timeout: 20000 });
  const beforeConfirm = await getPlan(page, token, workspaceId);
  expect(
    beforeConfirm.nodes.some(node => node.title === '这学期重心:英语'),
    '还没确认,回答之后的提案就已经写进计划了',
  ).toBe(false);

  // ---- 刷新:问题已经 resolved,卡片不再挂着;状态是后端说的,不是内存里的 ----
  await page.reload();
  await waitForRealPlan(page);
  await expect(page.locator('.question-card')).toHaveCount(0);
});

test('稍后回答保持 pending,刷新之后问题卡还在', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'question-later');
  const workspaceId = await createWorkspace(page, token, '问题稍后空间', '提升英语');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  await say(page, '我想提升一下。');
  const card = page.locator('.question-card');
  await expect(card).toBeVisible({ timeout: 20000 });

  await card.getByRole('button', { name: '稍后回答' }).click();
  // 稍后回答不归档:卡片还在(只是这一次动作被记下来了)。
  await expect(page.locator('.question-card')).toBeVisible();

  await page.reload();
  await waitForRealPlan(page);
  await expect(page.locator('.question-card')).toBeVisible({ timeout: 20000 });
  await expect(page.locator('.question-card')).toContainText('这学期你希望把重心放在哪一边?');
});
