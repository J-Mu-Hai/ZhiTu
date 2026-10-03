/**
 * 6B-3:公开研究的决策链路(隔离 E2E)。
 *
 * ## 它验的是什么
 *
 * 一次完整链路:**脚本化 reasoner 发起公开可研究的问题 -> 受控 mock provider 只被
 * 调用一次 -> 对话里出现服务端验证的可点击来源 -> 画布投影出一张战略取舍问题卡 ->
 * 可自由拖动、刷新后位置/状态/来源都恢复 -> 回答后状态同步且不自动写计划**;
 * 以及缓存命中、隐私拦截、每日额度耗尽、画布静止不闪烁。
 *
 * ## 为什么是隔离栈
 *
 * 它必须用 `--research-mock` 起测试后端:`RESEARCH_PROVIDER=mock` +
 * `RESEARCH_MOCK_ENABLED=true` + 非生产(三道闸见 `research_service.provider_ready`),
 * 于是**不碰真实 Tavily、不碰公网**,结果确定可复现。生产只支持真实 Tavily。
 *
 * ## 顺序是有意的
 *
 * 每日额度被 `--research-mock` 压到 1(每次运行都是全新临时库,额度从 0 开始)。
 * 所以本文件里的用例**必须按声明顺序跑**(Playwright 同文件、`workers=1` 时就是如此):
 *
 *   1. 第一次真实研究(用掉唯一额度)
 *   2. 同一查询 -> 缓存命中(不耗额度)
 *   3. 含敏感信息 -> 隐私拦截(不耗额度)
 *   4. 另一个查询 -> 额度耗尽(不出网)
 *   5. 稳定性(命中缓存,不耗额度)
 */

import { expect, test, type Page } from '@playwright/test';
import {
  api,
  assertBackendRunning,
  createWorkspace,
  getPlan,
  registerAccount,
  waitForRealPlan,
} from './support/session';

const SCRIPTED = process.env.ZHITU_SCRIPTED_ACTIONS ?? '';
const RESEARCH_MOCK = process.env.ZHITU_RESEARCH_MOCK ?? '';

/** 会被 mock 正常命中的查询 —— 场景 1、2、5 共用它,好让 2 与 5 命中 1 建立的缓存。 */
const QUERY_SUCCESS = '保研政策背景与截止时间';
/** 另一个查询 —— 额度已耗尽时用它触发“limited”,而不是命中缓存。 */
const QUERY_LIMIT = '公开课程报名时间与费用';

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
  test.skip(
    !SCRIPTED || !RESEARCH_MOCK,
    '这一条要 --script=apps/web/tests/fixtures/research-flow-script.json --research-mock',
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

type ResearchRecord = {
  status: string | null;
  consulted: boolean;
  citations: Array<{ sourceId: string; title: string; url: string; domain: string; provider: string }>;
};

/** 这个空间里最后一条助手消息上的 research 记录(服务端认可的真相)。 */
async function latestResearch(page: Page, token: string, workspaceId: string): Promise<ResearchRecord | null> {
  const view = await api<{ messages: Array<{ role: string; research: ResearchRecord | null }> }>(
    page,
    token,
    `/api/workspaces/${workspaceId}/messages`,
  );
  const assistant = [...view.messages].reverse().find(message => message.role === 'assistant');
  return assistant?.research ?? null;
}

const questionNode = (page: Page) => page.locator('.react-flow__node-question');
const questionCard = (page: Page) => page.locator('.canvas-question-node');

/** 问题节点在当前流坐标系里的 transform(与视口平移缩放无关)。 */
async function questionTransform(page: Page): Promise<string> {
  return (await questionNode(page).getAttribute('style')) ?? '';
}

/** 从卡片顶部空白处(不是按钮)起手拖动。 */
async function dragQuestion(page: Page, dx: number, dy: number): Promise<void> {
  const box = await questionNode(page).boundingBox();
  if (!box) throw new Error('问题节点没有边界框,拖不动');
  await page.mouse.move(box.x + 40, box.y + 14);
  await page.mouse.down();
  await page.mouse.move(box.x + 40 + dx, box.y + 14 + dy, { steps: 12 });
  await page.mouse.up();
}

test('公开研究:对话显示可信来源,画布提出问题,拖动/刷新恢复,未确认不写计划', async ({ page }) => {
  test.slow();
  const account = await registerAccount(page, 'research-flow');
  const workspaceId = await createWorkspace(page, account.token, '公开研究空间', '提升英语和数学');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  await say(page, QUERY_SUCCESS);

  // ---- 1/3. 对话里出现服务端验证的、可点击的来源(真实 <a>,外链属性齐全) ----
  const citations = page.locator('.research-citations');
  await expect(citations).toBeVisible({ timeout: 20000 });
  await expect(citations).toContainText('公开政策说明(测试来源)');
  const link = citations.getByRole('link', { name: /公开政策说明/ });
  await expect(link).toHaveAttribute('href', 'https://example.edu.cn/mock-policy');
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer');

  // API 上的记录:success + 服务端派生引用 + 不含搜索关键词。
  const research = await latestResearch(page, account.token, workspaceId);
  expect(research?.status).toBe('success');
  expect(research?.consulted).toBe(true);
  expect(research?.citations.length).toBeGreaterThan(0);
  expect(research?.citations[0]?.sourceId).toBeTruthy();
  expect(research?.citations[0]?.domain).toBe('example.edu.cn');
  expect(JSON.stringify(research)).not.toContain(QUERY_SUCCESS);

  // ---- 2/3. 画布投影出的是一张**战略取舍**问题卡,不是排期问题 ----
  await expect(questionNode(page)).toHaveCount(1, { timeout: 20000 });
  const cardText = await questionCard(page).innerText();
  expect(cardText).toContain('重心放在哪一边');
  expect(cardText, '战略阶段不该优先问每周投入时间').not.toContain('每周');
  await expect(page.locator('.react-flow__edge.question-anchor-edge')).toHaveCount(1);

  // ---- 3/3. 可自由拖动,锚定虚线跟随;刷新后位置/状态/来源恢复 ----
  const before = await questionTransform(page);
  await dragQuestion(page, 170, 110);
  await expect.poll(() => questionTransform(page)).not.toBe(before);
  const moved = await questionTransform(page);
  await expect(page.locator('.react-flow__edge.question-anchor-edge')).toHaveCount(1);

  await page.reload();
  await waitForRealPlan(page);
  await expect(questionNode(page)).toHaveCount(1, { timeout: 20000 });
  await expect(page.locator('.research-citations')).toContainText('公开政策说明(测试来源)');
  await expect.poll(() => questionTransform(page)).toBe(moved);

  // ---- 在画布上回答:状态同步,右侧没有第二份可提交控件 ----
  await questionCard(page).getByRole('button', { name: '英语' }).click();
  await questionCard(page).getByRole('button', { name: '提交回答' }).click();
  const proposal = page.locator('.proposal').filter({ hasText: '这学期重心:英语' }).first();
  await expect(proposal).toBeVisible({ timeout: 20000 });
  const hint = page.locator('.question-status-bar');
  await expect(hint.getByRole('button', { name: '提交回答' })).toHaveCount(0);
  await expect(hint.locator('.cq-option')).toHaveCount(0);

  // ---- 不确认就不写:研究结果与回答都没有自动进业务计划/关系 ----
  const plan = await getPlan(page, account.token, workspaceId);
  expect(plan.nodes.some(node => node.title === '这学期重心:英语')).toBe(false);
  expect(plan.relations).toHaveLength(0);
});

test('同一研究再次触发命中缓存:显示缓存来源,provider 不再被调用', async ({ page }) => {
  test.slow();
  const account = await registerAccount(page, 'research-cached');
  const workspaceId = await createWorkspace(page, account.token, '公开研究缓存空间', '提升英语');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 与场景 1 相同的查询 -> 命中上一条建立的持久化缓存(跨空间共享)。
  await say(page, QUERY_SUCCESS);
  await expect(page.locator('.research-citations')).toBeVisible({ timeout: 20000 });
  await expect(page.locator('.research-citations .eyebrow')).toContainText('缓存');

  const research = await latestResearch(page, account.token, workspaceId);
  expect(research?.status).toBe('cached');
  expect(research?.citations.length).toBeGreaterThan(0);
});

test('含敏感信息的查询被拦下:不出网,UI 诚实说明未检索', async ({ page }) => {
  test.slow();
  const account = await registerAccount(page, 'research-private');
  const workspaceId = await createWorkspace(page, account.token, '公开研究隐私空间', '提升英语');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  await say(page, '帮我查 xiaoming@example.com 的保研政策');

  const note = page.locator('.research-note');
  await expect(note).toBeVisible({ timeout: 20000 });
  await expect(note).toContainText('私密');
  // 没有伪造来源。
  await expect(page.locator('.research-citations')).toHaveCount(0);

  const research = await latestResearch(page, account.token, workspaceId);
  expect(research?.status).toBe('blocked');
  expect(research?.consulted).toBe(true);
  expect(research?.citations).toHaveLength(0);
});

test('每日额度耗尽后不出网:UI 显示额度限制', async ({ page }) => {
  test.slow();
  const account = await registerAccount(page, 'research-limited');
  const workspaceId = await createWorkspace(page, account.token, '公开研究额度空间', '提升英语');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 额度已在场景 1 用尽;这是另一个查询,不会命中缓存,于是走到额度检查。
  await say(page, QUERY_LIMIT);

  const note = page.locator('.research-note');
  await expect(note).toBeVisible({ timeout: 20000 });
  await expect(note).toContainText('额度');
  await expect(page.locator('.research-citations')).toHaveCount(0);

  const research = await latestResearch(page, account.token, workspaceId);
  expect(research?.status).toBe('limited');
  expect(research?.citations).toHaveLength(0);
});

test('画布静止时问题节点位置稳定、不被反复重挂载', async ({ page }) => {
  test.slow();
  const account = await registerAccount(page, 'research-stable');
  const workspaceId = await createWorkspace(page, account.token, '公开研究稳定空间', '提升英语');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  await say(page, QUERY_SUCCESS);
  await expect(questionNode(page)).toHaveCount(1, { timeout: 20000 });
  await page.waitForTimeout(600);

  await questionNode(page).evaluate(element => {
    (element as unknown as Record<string, unknown>).__zhituStableMark = true;
  });

  const samples: string[] = [];
  for (let i = 0; i < 12; i += 1) {
    await page.waitForTimeout(250);
    samples.push(
      await questionNode(page).evaluate(element => {
        const rect = element.getBoundingClientRect();
        return `${Math.round(rect.x)},${Math.round(rect.y)},${Math.round(rect.width)}x${Math.round(rect.height)}`;
      }),
    );
  }

  const unique = [...new Set(samples)];
  expect(unique, `问题节点在静止时持续变化(闪烁):\n${unique.join('\n')}`).toHaveLength(1);
  expect(
    await questionNode(page).evaluate(
      element => (element as unknown as Record<string, unknown>).__zhituStableMark === true,
    ),
    '问题节点被卸载/重挂载了',
  ).toBe(true);
});
