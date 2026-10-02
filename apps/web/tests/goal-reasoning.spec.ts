/**
 * 阶段 7:目标推理智能体的隔离 E2E。
 *
 * 用 `--script=apps/web/tests/fixtures/goal-reasoning-script.json` 起隔离栈:
 * 没有真实模型、没有真实联网,脚本化 reasoner 按轮次给出问题地图操作。
 *
 * 覆盖:进入目标自动生成问题地图(幂等)、焦点与战略取舍问题、回答后地图状态推进、
 * 战略确认走既有 proposal、未确认不写业务计划、确认后才写战略节点并出现"细化第一阶段"。
 */

import { expect, test, type Page, type Request } from '@playwright/test';
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

test('进入目标自动生成问题地图,重复进入不重复', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'goal-reasoning-enter');
  const workspaceId = await createWorkspace(page, token, '目标推理空间', '我想系统学习 Python');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  await waitForMap(page);
  // 画布上已经有问题地图、但还没有任何**业务计划子节点** —— 空态必须完全不渲染。
  await expect(page.locator('.empty-space-note'), '有推理地图时不该再显示空态').toHaveCount(0);
  // 恰好一个焦点,而且焦点有"为什么先处理它"。
  await expect(page.locator('.reasoning-node.is-focus')).toHaveCount(1, { timeout: 20000 });
  await expect(page.locator('.reasoning-node.is-focus')).toContainText('用途');

  // 战略阶段先问取舍 —— 不能先问每周投入。
  await expect(questionCard(page)).toHaveCount(1, { timeout: 20000 });
  const questionText = await questionCard(page).innerText();
  expect(questionText).toContain('哪条路线');
  expect(questionText, '战略层不该先问每周投入').not.toContain('每周');

  // QuestionStatusHint 必须跟暖白主题一致,而且"定位到画布"要有足够对比度。
  const hint = page.locator('.question-hint');
  await expect(hint).toBeVisible({ timeout: 20000 });
  const styles = await hint.evaluate((element) => {
    const parse = (value: string) => {
      const numbers = (value.match(/[\d.]+/g) ?? []).map(Number);
      // Chrome 对 `color-mix(...)` 的结果返回 `color(srgb r g b)`(0–1),
      // 不是 `rgb(r g b)`(0–255)。不归一化会把浅色读成近黑。
      return value.trim().startsWith('color(srgb') ? numbers.map((n) => n * 255) : numbers;
    };
    const luminance = (rgb: number[]) => {
      const [r, g, b] = rgb.slice(0, 3).map((channel) => {
        const s = channel / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const ratio = (a: number[], b: number[]) => {
      const [lighter, darker] = [luminance(a), luminance(b)].sort((x, y) => y - x);
      return (lighter + 0.05) / (darker + 0.05);
    };
    const computed = getComputedStyle(element);
    const background = parse(computed.backgroundColor);
    const button = element.querySelector('.question-locate');
    const buttonColor = button ? parse(getComputedStyle(button).color) : [];
    return {
      background: computed.backgroundColor,
      backgroundLuminance: luminance(background),
      textContrast: ratio(parse(computed.color), background),
      buttonContrast: buttonColor.length ? ratio(buttonColor, background) : 0,
    };
  });
  expect(styles.background, '问题提示还在用旧的深色硬编码背景').not.toBe('rgb(26, 24, 16)');
  expect(styles.backgroundLuminance, '问题提示背景不是浅色').toBeGreaterThan(0.6);
  expect(styles.textContrast, '问题提示正文对比度不足').toBeGreaterThanOrEqual(4.5);
  expect(styles.buttonContrast, '“定位到画布”对比度不足').toBeGreaterThanOrEqual(4.5);

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

test('顶层讨论锚定到目标根节点,悬停不重建画布、点击只开一次详情', async ({ page }) => {
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
  await waitForMap(page);

  const plan = await getPlan(page, token, workspaceId);
  const root = plan.nodes.find((node) => node.parentId === null)!;
  await expect(page.locator(`.react-flow__node[data-id="${root.id}"]`)).toBeVisible();

  // 每个顶层推理节点都有一条 `reasoning-anchor:*` 锚定边,source 是当前目标根节点。
  for (const handle of ['r1', 'r2', 'r3', 'r4']) {
    const edge = page.locator(`.react-flow__edge[data-id="reasoning-anchor:${handle}"]`);
    await expect(edge, `顶层推理节点 ${handle} 缺少锚定线`).toHaveCount(1);
    await expect(edge).toHaveAttribute('aria-label', `Edge from ${root.id} to reasoning:${handle}`);
  }
  await expect(page.locator('.react-flow__edge.reasoning-anchor-edge')).toHaveCount(4);
  // 内部真实 links 仍在(r1→r3),锚定线没有替代它的语义。
  await expect(page.locator('.react-flow__edge-reasoningLink')).toHaveCount(1);

  // 锚定线不是普通关系:正式 plan 里 relations 为空,也没发过 POST /relations。
  expect((await getPlan(page, token, workspaceId)).relations).toHaveLength(0);
  expect(relationPosts, '锚定线被写成了业务关系').toEqual([]);

  // ---- 悬停 10 次:节点投影不重建、视口不动、不发请求 ----
  const card = reasoningCard(page).filter({ hasText: '目标用途' });
  const node = page.locator('.react-flow__node[data-id="reasoning:r1"]');
  await expect(card).toBeVisible();
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
    await card.hover();
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
  // 去掉了 `onPointerDown` 的重复入口:只按下指针(不松开)不该打开详情。
  await card.dispatchEvent('pointerdown');
  await expect(detail, '按下指针不该打开详情').toHaveCount(0);
  // 完整一次点击:恰好一个详情面板。用 `.click()`(它自带滚动与稳定性等待)——
  // 推理节点在 960 高的视口里靠下,直接拿坐标点会落在视口外。
  await card.click();
  await expect(detail, '点击一次应该只出现一个详情面板').toHaveCount(1);
  await expect(detail).toContainText('目标推理');
  // 面板里就是刚才那个节点(输入框的值不属于 text content,单独验)。
  await expect(detail.getByLabel('维度标题')).toHaveValue('目标用途');
});
