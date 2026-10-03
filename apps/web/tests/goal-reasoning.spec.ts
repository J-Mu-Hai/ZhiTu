/**
 * 阶段 7–8:目标推理智能体的隔离 E2E(路线优先)。
 *
 * 用 `--script=apps/web/tests/fixtures/goal-reasoning-script.json` 起隔离栈:
 * 没有真实模型、没有真实联网,脚本化 reasoner 按轮次给出路线图操作。
 *
 * 覆盖:进入目标自动生成**路线图**(1 条 route + 4 个 stage,幂等)、焦点与战略取舍
 * 问题、回答后地图状态推进、战略确认走既有 proposal、未确认不写业务计划、确认后才写
 * 战略节点并出现“细化第一阶段”。
 */

import { expect, test, type Page, type Request } from '@playwright/test';
import {
  assertBackendRunning,
  createWorkspace,
  getPlan,
  openSpacePage,
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
const routeCard = (page: Page) => reasoningCard(page).filter({ hasText: '推荐路线' });

async function waitForMap(page: Page): Promise<void> {
  // 路线图 = 1 条 route + 4 个 stage。
  await expect(reasoningNode(page)).toHaveCount(5, { timeout: 25000 });
  await expect(routeCard(page)).toBeVisible();
}

/**
 * 回答对话式 intake 的关键问题。
 *
 * 选项 chip 就在最后一条助手消息下面 —— 点一下等价于把这句话发出去。
 */
async function answerIntake(page: Page, label: string): Promise<void> {
  const inline = page.getByTestId('intake-inline');
  await expect(inline).toBeVisible({ timeout: 25000 });
  await inline.getByRole('button', { name: new RegExp(label) }).click();
}

/**
 * 时间架构生成后工作台会自动切到时间线;这些用例验的是路径画布,所以切回去。
 */
async function backToPath(page: Page): Promise<void> {
  await expect(page).toHaveURL(/view=timeline/, { timeout: 25000 });
  await page.getByRole('tab', { name: '路径', exact: true }).click();
  await expect(page).toHaveURL(/view=path/);
}

/** 节点投影 memo 跑了几次(见 `PathView` 里 `__zhituNodeProjectionBuilds` 的说明)。 */
async function projectionBuilds(page: Page): Promise<number> {
  return page.evaluate(
    () => (window as unknown as { __zhituNodeProjectionBuilds?: number }).__zhituNodeProjectionBuilds ?? 0,
  );
}

/** 只算边的那份 memo 跑了几次 —— 用来证明 hover 真的到达了 React。 */
async function edgeFocusBuilds(page: Page): Promise<number> {
  return page.evaluate(
    () => (window as unknown as { __zhituEdgeFocusBuilds?: number }).__zhituEdgeFocusBuilds ?? 0,
  );
}

/** React Flow 此刻的平移与缩放。**从 DOM 里读**,与产品自己存了什么无关。 */
async function viewportTransform(page: Page): Promise<string> {
  return (await page.locator('.react-flow__viewport').getAttribute('style')) ?? '';
}

test('进入目标先问关键问题,回答后生成路线图,重复进入不重复', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'goal-reasoning-enter');
  const workspaceId = await createWorkspace(page, token, '目标推理空间', '我想系统学习 Python 做数据分析，每周 150 分钟。');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 阶段 11:首轮是对话式 intake —— 画布上没有路线、没有问题节点。
  await expect(page.getByTestId('intake-inline')).toBeVisible({ timeout: 25000 });
  await expect(page.getByTestId('intake-progress')).toContainText('正在梳理目标');
  await expect(reasoningNode(page)).toHaveCount(0);
  await expect(page.locator('.react-flow__node-question')).toHaveCount(0);

  // 回答关键问题 -> 一次性生成路线图,并自动切到时间线。
  await answerIntake(page, '英语');
  await backToPath(page);
  await waitForMap(page);

  // 有路线图、但还没有任何**业务计划子节点** —— 空态必须完全不渲染。
  await expect(page.locator('.empty-space-note'), '有推理地图时不该再显示空态').toHaveCount(0);
  // 恰好一个焦点,而且焦点是第一个阶段。
  await expect(page.locator('.reasoning-node.is-focus')).toHaveCount(1, { timeout: 20000 });
  await expect(page.locator('.reasoning-node.is-focus')).toContainText('用途');

  // 阶段 10:对话区首屏有**当前战略判断摘要**,内容来自已验证的路线/阶段。
  const summary = page.getByTestId('strategy-summary');
  await expect(summary).toBeVisible();
  await expect(summary).toContainText('当前战略判断');
  await expect(summary).toContainText('推荐路线：约 10 周');
  await expect(summary).toContainText('总时长');

  // 重复进入(刷新)不重复建节点;刷新后 architecture 会立刻把视图切到时间线,
  // 所以**先等自动切视图、再切回路径页**,最后才谈得上等计划画出来。
  await page.reload();
  await backToPath(page);
  await waitForRealPlan(page);
  await waitForMap(page);
  await expect(reasoningNode(page)).toHaveCount(5);
});

test('战略确认走提案,确认后才写计划', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'goal-reasoning-confirm');
  const workspaceId = await createWorkspace(page, token, '目标推理确认空间', '我想系统学习英语');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  await answerIntake(page, '英语');
  await backToPath(page);
  await waitForMap(page);

  // 点开路线节点 -> 确认这条战略 -> 生成待确认提案。
  await routeCard(page).click();
  await expect(page.locator('.reasoning-detail')).toBeVisible();
  await page.locator('.reasoning-detail').getByRole('button', { name: '确认这条战略' }).click();

  const proposal = page.locator('.proposal').filter({ hasText: '推荐路线' }).first();
  await expect(proposal).toBeVisible({ timeout: 20000 });

  // **未确认时业务计划里没有战略节点。**
  const beforeConfirm = await getPlan(page, token, workspaceId);
  expect(beforeConfirm.nodes.some(node => node.title.startsWith('推荐路线'))).toBe(false);

  // 确认提案 -> 战略节点真的写入计划。
  await proposal.getByRole('button', { name: '确认，写入计划' }).click();
  await expect
    .poll(async () => (await getPlan(page, token, workspaceId)).nodes.some(node => node.title.startsWith('推荐路线')), {
      timeout: 20000,
    })
    .toBe(true);

  // 已确认战略之后才出现"细化第一阶段"入口。
  await expect(page.getByRole('button', { name: '细化第一阶段' })).toBeVisible({ timeout: 20000 });
});

test('路线锚定到目标根节点,悬停不重建画布、点击只开一次详情', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'goal-reasoning-anchor');
  const workspaceId = await createWorkspace(page, token, '推理锚定空间', '我想系统学习 Python');

  // 锚定线绝不能写业务关系:任何 POST /relations 都记下来。
  const relationPosts: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().includes('/relations')) relationPosts.push(request.url());
  });

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  await answerIntake(page, '英语');
  await backToPath(page);
  await waitForMap(page);

  const plan = await getPlan(page, token, workspaceId);
  const root = plan.nodes.find((node) => node.parentId === null)!;
  await expect(page.locator(`.react-flow__node[data-id="${root.id}"]`)).toBeVisible();

  // 顶层只有那一条路线,它与根目标连一条 `reasoning-anchor:r1` 锚定线。
  const routeAnchor = page.locator('.react-flow__edge[data-id="reasoning-anchor:r1"]');
  await expect(routeAnchor, '推荐路线缺少到根目标的锚定线').toHaveCount(1);
  await expect(routeAnchor).toHaveAttribute('aria-label', `Edge from ${root.id} to reasoning:r1`);
  await expect(page.locator('.react-flow__edge.reasoning-anchor-edge')).toHaveCount(1);
  // 阶段链是真实的 reasoning links(r1→r2→r3→r4→r5)。
  await expect(page.locator('.react-flow__edge-reasoningLink')).toHaveCount(4);
  // 推理节点**不是业务节点**:它没有业务“删除/建立关系”入口,也没有更多操作菜单。
  await expect(page.locator('.react-flow__node-reasoning .node-more')).toHaveCount(0);
  await expect(page.locator('.react-flow__node-question .node-more')).toHaveCount(0);

  // 锚定线不是普通关系:正式 plan 里 relations 为空,也没发过 POST /relations。
  expect((await getPlan(page, token, workspaceId)).relations).toHaveLength(0);
  expect(relationPosts, '锚定线被写成了业务关系').toEqual([]);

  // ---- 悬停 10 次:节点投影不重建、视口不动、不发请求 ----
  const node = page.locator('.react-flow__node[data-id="reasoning:r1"]');
  await expect(routeCard(page)).toBeVisible();
  // 等测量与初始定位都安静下来。
  await page.waitForTimeout(500);

  await node.evaluate((element) => {
    (element as unknown as Record<string, unknown>).__zhituStableMark = true;
  });
  const buildsBefore = await projectionBuilds(page);
  const focusBuildsBefore = await edgeFocusBuilds(page);
  const viewportBefore = await viewportTransform(page);
  const rectBefore = await node.boundingBox();
  if (!rectBefore) throw new Error('推理节点没有边界框');

  const apiRequests: string[] = [];
  const listener = (request: Request) => {
    if (request.url().includes('/api/')) apiRequests.push(request.url());
  };
  page.on('request', listener);
  for (let i = 0; i < 10; i += 1) {
    await routeCard(page).hover();
    await page.mouse.move(4, 4);
  }
  page.off('request', listener);

  expect(
    await edgeFocusBuilds(page),
    '悬停没有到达 React(边高亮那条路没跑)—— 这条测试的前提没成立',
  ).toBeGreaterThan(focusBuildsBefore);
  expect(await projectionBuilds(page), '悬停/移出重建了节点投影 —— 这正是闪烁的来源').toBe(buildsBefore);
  expect(await viewportTransform(page), '悬停改变了画布视口(或触发了 fitView)').toBe(viewportBefore);
  expect(apiRequests, '悬停产生了额外请求').toEqual([]);
  const rectAfter = await node.boundingBox();
  expect(rectAfter, '推理节点在悬停后消失了').not.toBeNull();
  expect(Math.round(rectAfter!.x), '推理节点横向位置在悬停后变了').toBe(Math.round(rectBefore.x));
  expect(Math.round(rectAfter!.y), '推理节点纵向位置在悬停后变了').toBe(Math.round(rectBefore.y));
  expect(
    await node.evaluate((element) => (element as unknown as Record<string, unknown>).__zhituStableMark === true),
    '推理节点在悬停时被卸载/重挂载了',
  ).toBe(true);

  // ---- 点击一次:只出现一个详情面板;只按下指针不打开(重复入口已删除) ----
  const detail = page.locator('.reasoning-detail');
  await routeCard(page).dispatchEvent('pointerdown');
  await expect(detail, '按下指针不该打开详情').toHaveCount(0);
  await routeCard(page).click();
  await expect(detail, '点击一次应该只出现一个详情面板').toHaveCount(1);
  await expect(detail).toContainText('目标推理');
  await expect(detail.getByLabel('维度标题')).toHaveValue('推荐路线：约 10 周从基础到可展示项目');
});

/**
 * 旧地图过渡:阶段 8 之前留下的“散乱一级维度图”不能再一直污染新体验。
 *
 * 这里用 `page.route` 把推理地图的读取与“重新生成”的 agent turn 换成固定载荷 ——
 * 旧地图是**存量数据**,隔离栈里造不出来(新首轮已经被服务端强制成路线图)。这一条
 * 验的是:界面能认出旧地图、点“重新生成战略路线”后主画布切成路线、旧节点不丢。
 */
test('旧地图可识别并重新生成战略路线，旧节点留在思考层', async ({ page }) => {
  const { token } = await registerAccount(page, 'legacy-map');
  const id = await createWorkspace(page, token, '旧地图空间');
  const root = (await getPlan(page, token, id)).nodes[0];
  const now = new Date().toISOString();

  const node = (handle: string, title: string, nodeType: string, parentHandle: string | null) => ({
    id: `${handle}-id`,
    handle,
    parentHandle,
    linkedPlanNodeId: null,
    title,
    summary: null,
    userDescription: null,
    nodeType,
    status: 'exploring',
    nextAction: 'analyze',
    importance: 4,
    uncertainty: 3,
    urgency: 1,
    impact: 4,
    confidence: 2,
    priority: 3,
    rationale: null,
    assumptions: [],
    evidence: [],
    timeframe: null,
    deliverable: null,
    passCriteria: null,
    source: 'agent',
    version: 1,
    updatedAt: now,
  });

  const legacyView = {
    workspaceId: id,
    sessionId: 'legacy-session',
    rootPlanNodeId: root.id,
    phase: 'strategic_exploration',
    turnAction: 'analyze',
    status: 'ready',
    mapVersion: 1,
    focusHandle: 'old1',
    focusReason: '旧焦点',
    nodes: [1, 2, 3, 4].map(i => node(`old${i}`, `旧版维度 ${i}`, 'dimension', null)),
    links: [],
    error: null,
  };
  const roadmapView = {
    ...legacyView,
    phase: 'roadmap_draft',
    turnAction: 'ask_user',
    focusHandle: 'r2',
    nodes: [
      node('r1', '推荐路线：约 10 周先打通最小闭环', 'route', null),
      node('r2', '阶段 1：打基础', 'stage', 'r1'),
      node('r3', '阶段 2：做一次实战', 'stage', 'r1'),
      node('r4', '阶段 3：补齐与展示', 'stage', 'r1'),
      ...legacyView.nodes,
    ],
  };

  let regenerated = false;
  await page.route('**/api/workspaces/*/reasoning', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(regenerated ? roadmapView : legacyView),
    });
  });
  await page.route('**/api/workspaces/*/agent/turn', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    regenerated = true;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reasoning: roadmapView,
        message: null,
        question: null,
        replayed: false,
        degraded: false,
        degradedReason: null,
        retryable: false,
        changed: true,
        proposalErrors: [],
      }),
    });
  });

  await openSpacePage(page, '/workbench', id);
  await waitForRealPlan(page);

  const regenerate = page.getByRole('button', { name: /重新生成战略路线/ });
  await expect(regenerate, '旧地图没有重生成入口').toBeVisible({ timeout: 20000 });
  await regenerate.click();
  await backToPath(page);

  // 主画布切换成路线 + 阶段,入口消失。
  await expect(page.locator('.reasoning-node').filter({ hasText: '推荐路线' })).toBeVisible();
  await expect(page.getByRole('button', { name: /重新生成战略路线/ })).toHaveCount(0);

  // 旧节点不删:默认折叠,点开思考层后仍看得到。
  await expect(page.locator('.reasoning-node').filter({ hasText: '旧版维度 1' })).toHaveCount(0);
  await page.getByRole('button', { name: /思考层/ }).click();
  await expect(page.locator('.reasoning-node').filter({ hasText: '旧版维度 1' })).toBeVisible();
});
