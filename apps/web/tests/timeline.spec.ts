import { test, expect } from '@playwright/test';
import {
  assertBackendRunning,
  createNode,
  createWorkspace,
  dayOffset,
  getPlan,
  openSpacePage,
  registerAccount,
} from './support/session';
import { anchoredZoom, dateToX, dayNumber, getVisibleItems, layoutItems, timelineItems, timelineTicks, xToDate, todayInTimeZone } from '../src/features/growth/timeline';
import type { GrowthNode, GrowthState } from '../src/types/growth';

/**
 * 时间线(阶段 9:中央唯一时间轴)。
 *
 * ## 第一条是纯函数,单位是"天"
 *
 * 日历映射、跨年刻度、语义层级、密集布局 —— 这些都不需要浏览器,也不需要一个空间。
 * 它就地搭一份最小计划喂给 `timeline.ts`,断言的是**布局算法本身**。
 *
 * ## 第二条走真实空间
 *
 * 时间线整条都改成真实空间了(见 `TimelineView.tsx`):卡片画的永远是计划里真实
 * 的那一天。所以这条测试也必须在真实空间里跑 —— 空间和节点由它自己通过接口搭出来。
 *
 * 阶段 9 删掉了左上尺度按钮、右上导航/缩放组、顶部 ruler 与底部 overview。**删控件
 * 不等于删能力**:这一条改用键盘 `+`/`-`/方向键/`Home` 与滚轮来验同一批交互。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

function node(id: string, title: string, type: GrowthNode['type'], extra: Partial<GrowthNode> = {}): GrowthNode {
  return { id, title, type, status: 'pending', priority: 'medium', ...extra };
}

/**
 * 一份写死的最小计划。日期都是绝对的 —— 这是纯函数测试,和"今天"无关。
 *
 * 三类节点各有各的用处:目标与重要节点(年为单位的层级)、有区间的一件事(要在
 * 月层级画出来)、同一天挤了十几个的小行动(布局放不下,必须有一部分进"另有 N 项")。
 */
const dense = Object.fromEntries(
  Array.from({ length: 12 }, (_, index) => {
    const id = `dense-${index}`;
    return [id, node(id, `同日的第 ${index} 件事`, 'task', { parentId: 'goal', startDate: '2026-10-19' })];
  }),
);
const actions = Object.fromEntries(
  Array.from({ length: 3 }, (_, index) => {
    const id = `action-${index}`;
    return [id, node(id, `当天的小行动 ${index}`, 'task', { parentId: 'goal', timelineLevel: 'action', scheduledDate: '2026-08-22' })];
  }),
);
const fixture: GrowthState = {
  id: 'space',
  title: '时间线单测空间',
  goalId: 'goal',
  currentStageId: 'goal',
  edges: [],
  nodes: {
    goal: node('goal', '读研准备', 'goal', { startDate: '2026-09-01', endDate: '2027-06-30' }),
    major: node('major', '拿到推免资格', 'milestone', { parentId: 'goal', timelineLevel: 'major', startDate: '2027-02-01' }),
    project: node('project', '科研项目', 'task', { parentId: 'goal', startDate: '2026-10-18', endDate: '2026-11-20' }),
    ...dense,
    ...actions,
  },
};

test('calendar mapping, cross-year ticks, semantic levels and dense layout', () => {
  const start = dayNumber('2026-12-01'), anchor = 263, density = 4;
  const next = anchoredZoom(start, density, 20, anchor);
  expect(dateToX(xToDate(anchor, start, density), next, 20)).toBeCloseTo(anchor);
  const ticks = timelineTicks(start, dayNumber('2027-02-01'), 'month').filter(t => t.major);
  expect(ticks.map(t => [t.label, t.year])).toEqual([['12月',2026],['1月',2027],['2月',2027]]);

  const items = timelineItems(fixture, 'goal');
  // 年这一层只画目标与重要节点。同一天那十二件事**不在** —— 它们属于更细的尺度,
  // 不是被丢了(下面 month 那一层它们全在)。
  expect(getVisibleItems(items, 'year').map(i => i.node.id).sort()).toEqual(['goal', 'major']);
  // 天这一层相反:只剩"小行动"这一类。
  const daily = getVisibleItems(items, 'day');
  expect(daily.map(i => i.node.id).sort()).toEqual(['action-0', 'action-1', 'action-2']);
  expect(daily.every(i => i.node.timelineLevel === 'action')).toBeTruthy();

  const monthly = getVisibleItems(items, 'month');
  const layout = layoutItems(monthly, dayNumber('2026-08-22'), 4, 760, 'project', 164, 2);
  // 选中并落在窗口里的那件事一定画得出来。
  expect(layout.placed.some(i => i.item.node.id === 'project')).toBeTruthy();
  // 而同一天挤了十二件 —— 层数是有上限的,放不下的必须**进得去"另有 N 项"**,
  // 不能被静默丢掉。少了这条,一个"只画得下的前几件、其余返回空"的实现也全绿。
  expect(layout.hidden.length).toBeGreaterThan(0);
  expect(layout.placed.length + layout.hidden.length).toBe(monthly.filter(i => i.end >= dayNumber('2026-08-22') && i.start <= dayNumber('2026-08-22') + 190).length);
  // 同一条道里的卡片不许叠在一起。
  for (const a of layout.placed) for (const b of layout.placed) {
    if (a !== b && a.lane === b.lane) expect(Math.abs(a.left - b.left)).toBeGreaterThanOrEqual(176);
  }
});

test('单轴、键盘缩放/平移、卡片布局，以及改截止时间真的写进后端', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));

  const { token } = await registerAccount(page, 'timeline');
  const workspaceId = await createWorkspace(page, token, '时间线验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  /*
   * **这一条有截止日的任务必须独占一天。**
   *
   * 它原来是和下面那十件挤在同一天的。结果是:那一天只有四张卡放得下,谁放得下由
   * 排道决定,而排道依赖读回来的节点顺序 —— 实测同一条测试连跑三次,**画出来的
   * 那四张每次都不一样**,两次里它的卡片正好没画出来。测试于是红在"计划没有到达"
   * 上,而计划早就到了:它在"另有 N 项"里,那是这一页**正确**的行为。
   *
   * 所以给它自己一天。下面那十件仍然挤在同一天,该验的"放不下要有一条出路"一点没少。
   */
  const deadline = dayOffset(30);
  const crowded = dayOffset(40);
  const dated = await createNode(page, token, workspaceId, { parentId: root.id, title: '有截止日的任务', nodeType: 'task', deadline });
  const stage = await createNode(page, token, workspaceId, { parentId: root.id, title: '一个阶段', nodeType: 'stage' });
  await createNode(page, token, workspaceId, { parentId: stage, title: '阶段下面的任务', nodeType: 'task', deadline: dayOffset(45) });
  await createNode(page, token, workspaceId, { parentId: root.id, title: '还没定日期的任务', nodeType: 'task' });
  // 同一天挤十件:月这一层画不下,必须有一部分进"另有 N 项"。
  for (let index = 0; index < 10; index++) {
    await createNode(page, token, workspaceId, { parentId: root.id, title: `同日安排 ${index}`, nodeType: 'task', deadline: crowded });
  }

  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });

  const timeline = page.getByTestId('timeline-view'), canvas = page.getByTestId('timeline-canvas');
  // 计划到了没有,认那张**卡片**;外层 `[data-timeline-item]` 自己高度为 0,判可见性会假红。
  await expect(page.locator(`[data-timeline-item="${dated}"] [data-timeline-card]`)).toBeVisible();
  await expect(timeline).toHaveAttribute('data-zoom', 'month');
  await expect(canvas).toHaveAttribute('data-ready', 'true');
  await expect(page.getByTestId('today-marker')).toBeVisible();

  // --- 阶段 9:只有一条中轴,没有尺度按钮 / 导航组 / overview / ruler ------------
  await expect(page.getByRole('group', { name: '时间尺度' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '今天', exact: true })).toHaveCount(0);
  await expect(page.getByTestId('timeline-overview')).toHaveCount(0);
  await expect(page.getByRole('slider', { name: '概览视窗位置' })).toHaveCount(0);
  // 交互画布本身不被左上角浮动的视图切换条盖住(删了工具栏之后要自己让开)。
  const [canvasBox, tabsBox] = await Promise.all([canvas.boundingBox(), page.locator('.view-tabs').boundingBox()]);
  expect(canvasBox!.y).toBeGreaterThanOrEqual(tabsBox!.y + tabsBox!.height);

  // 卡片互不重叠 —— 这是"信息分层"这个设计的全部意义。
  const cards = await page.locator('[data-timeline-card]').evaluateAll(elements => elements.map(e => { const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, right: r.right, bottom: r.bottom }; }));
  expect(cards.length).toBeGreaterThan(0);
  for (let i = 0; i < cards.length; i++) for (let j = i + 1; j < cards.length; j++) {
    const a = cards[i], b = cards[j];
    expect(a.right <= b.x || b.right <= a.x || a.bottom <= b.y || b.bottom <= a.y, '两张卡片叠在一起了').toBeTruthy();
  }

  // --- 尺度由密度自动推断:键盘 +/- 改变密度,画的东西跟着换 --------------------
  await canvas.focus();
  const zoomTo = async (target: string) => {
    const order = ['year', 'quarter', 'month', 'week', 'day'];
    for (let i = 0; i < 16; i++) {
      const current = await timeline.getAttribute('data-zoom');
      if (current === target) return;
      await page.keyboard.press(order.indexOf(current!) < order.indexOf(target) ? '+' : '-');
    }
  };
  await zoomTo('year');
  await expect(timeline).toHaveAttribute('data-zoom', 'year');
  // 年这一层画的是目标,不画具体任务。**不画不等于丢了** —— 它们在更细的尺度里。
  await expect(page.locator(`[data-timeline-item="${root.id}"]`)).toHaveCount(1);
  await expect(page.locator(`[data-timeline-item="${dated}"]`)).toHaveCount(0);
  // 季度这一层画的是阶段与重要节点:阶段出来了,挂在它下面的具体任务还没出来。
  await zoomTo('quarter');
  await expect(page.locator(`[data-timeline-item="${stage}"]`)).toHaveCount(1);
  await zoomTo('month');
  await expect(page.locator(`[data-timeline-item="${stage}"]`)).toHaveCount(0);
  await expect(page.locator(`[data-timeline-item="${dated}"]`)).toHaveCount(1);

  // --- 放不下的那一批:界面上必须有一条出路 -------------------------------------
  await page.getByRole('button', { name: /另有 \d+ 项/ }).click();
  const panel = page.locator('[data-cluster-panel]');
  await expect(panel).toBeVisible();
  const hiddenButtons = panel.locator('button:has(small)');
  expect(await hiddenButtons.count()).toBeGreaterThan(0);
  const firstHidden = (await hiddenButtons.first().innerText()).split('\n')[0].trim();
  await hiddenButtons.first().click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId('date-inspector')).toContainText(firstHidden);

  // --- 未排期:低干扰小入口,点开是节点列表,不再是一整条横幅 --------------------
  await expect(page.getByTestId('unscheduled-items')).toBeVisible();
  await page.getByTestId('unscheduled-items').getByRole('button', { name: /未排期 \d+ 项/ }).click();
  await expect(page.locator('[data-unscheduled-panel]')).toContainText('还没定日期的任务');
  await page.locator('[data-unscheduled-panel]').getByRole('button', { name: '关闭未排期列表' }).click();
  await expect(page.locator('[data-unscheduled-panel]')).toHaveCount(0);

  // --- Ctrl + 滚轮缩放:锚点底下那一天不能动 --------------------------------------
  const box = await canvas.boundingBox();
  if (!box) throw new Error('时间线画布没有尺寸');
  const clientX = Math.round(box.x + box.width * .65), pointerX = clientX - box.x;
  const before = { start: Number(await canvas.getAttribute('data-start')), density: Number(await canvas.getAttribute('data-density')) };
  await canvas.dispatchEvent('wheel', { clientX, clientY: box.y + 100, deltaY: -80, ctrlKey: true });
  await expect.poll(async () => Number(await canvas.getAttribute('data-density'))).toBeGreaterThan(before.density);
  const after = { start: Number(await canvas.getAttribute('data-start')), density: Number(await canvas.getAttribute('data-density')) };
  expect(after.start + pointerX / after.density).toBeCloseTo(before.start + pointerX / before.density, 5);

  // --- 方向键平移 / Home 回到今天 -------------------------------------------------
  const panStart = Number(await canvas.getAttribute('data-start'));
  await canvas.focus();
  await page.keyboard.press('ArrowRight');
  await expect.poll(async () => Number(await canvas.getAttribute('data-start'))).toBeGreaterThan(panStart);
  await page.keyboard.press('ArrowLeft');
  await expect.poll(async () => Number(await canvas.getAttribute('data-start'))).toBeLessThan(panStart + 1);

  const densityNow = Number(await canvas.getAttribute('data-density'));
  const home = dayNumber(todayInTimeZone()) - box.width / densityNow * .28;
  await page.keyboard.press('Home');
  await expect.poll(async () => Math.abs(Number(await canvas.getAttribute('data-start')) - home)).toBeLessThan(1.5);

  // --- 拖动空白平移 ---------------------------------------------------------------
  const dragStart = Number(await canvas.getAttribute('data-start'));
  await page.mouse.move(box.x + box.width * .8, box.y + box.height - 22);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * .8 - 100, box.y + box.height - 22, { steps: 5 });
  await page.mouse.up();
  await expect.poll(async () => Number(await canvas.getAttribute('data-start'))).toBeGreaterThan(dragStart);

  // --- 改截止时间:这是**真的会写进后端**的那条路 --------------------------------
  await page.keyboard.press('Home');
  await page.locator(`[data-timeline-item="${dated}"] [data-timeline-card]`).click();
  const inspector = page.getByTestId('date-inspector');
  await expect(inspector).toContainText('有截止日的任务');
  await expect(inspector).toContainText('还没排出具体安排');
  await expect(page.getByRole('button', { name: '编辑日期' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '改截止时间' })).toBeVisible();

  await page.getByRole('button', { name: '改截止时间' }).click();
  const moved = dayOffset(60);
  await page.getByLabel('截止日期').fill(moved);
  await page.getByRole('button', { name: '应用' }).click();
  await expect.poll(async () => (await getPlan(page, token, workspaceId)).nodes.find(item => item.id === dated)?.deadline).toBe(moved);

  // --- 示例空间那套"本地提案"没有回来 --------------------------------------------
  await expect(page.getByTestId('plan-ghost')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '查看影响' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '接受调整' })).toHaveCount(0);

  // 刷新之后截止日还是新的那个:真相在后端,不在浏览器内存里。
  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });
  await expect(page.locator(`[data-timeline-item="${dated}"]`)).toHaveAttribute('data-start-date', moved);

  expect(errors).toEqual([]);
});

/**
 * 时间线画布不被浮动的视图切换条盖住。
 *
 * ## 为什么单开一条
 *
 * 上面那条测的是**行为**;这一条测的是**几何**。混在一起的话,一次"顶边被盖住"
 * 会先红在某个拖拽/点击上,报出来是"元素点不动",读的人要顺着堆栈往回找才会想到
 * 是布局。阶段 9 删掉了顶部工具栏,但左上角浮动的 `.view-tabs` 还在 —— 交互画布
 * 必须自己让开它,否则顶边那一块就是"看得见但拖不动"。
 *
 * 判据是:画布顶边 >= 浮动组件的下沿。桌面上量一次,收窄成手机再量一次 ——
 * 同一组断言在两种布局下都必须成立。
 */
test('时间线画布不被浮动视图切换盖住', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));

  const { token } = await registerAccount(page, 'timeline-geometry');
  const workspaceId = await createWorkspace(page, token, '时间线几何验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  const dated = await createNode(page, token, workspaceId, {
    parentId: root.id, title: '几何用例的节点', nodeType: 'task', deadline: dayOffset(20),
  });

  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });
  await expect(page.locator(`[data-timeline-item="${dated}"] [data-timeline-card]`)).toBeVisible();

  const canvas = page.getByTestId('timeline-canvas');
  const assertCanvasClear = async (label: string) => {
    const [canvasBox, tabsBox] = await Promise.all([
      canvas.boundingBox(), page.locator('.view-tabs').boundingBox(),
    ]);
    if (!canvasBox) throw new Error(`${label}:时间线画布没有尺寸,几何断言无从谈起`);
    if (!tabsBox) throw new Error(`${label}:浮动组件「视图切换」没有尺寸,几何断言无从谈起`);
    expect(
      canvasBox.y,
      `${label}:画布顶边(${Math.round(canvasBox.y)}px)落在「视图切换」的下沿(${Math.round(tabsBox.y + tabsBox.height)}px)之上`,
    ).toBeGreaterThanOrEqual(tabsBox.y + tabsBox.height);
  };

  await assertCanvasClear('桌面 1440');

  await page.setViewportSize({ width: 390, height: 844 });
  // 断点生效的等待信号:≤480 那一档 AI 面板默认收起。
  await expect(page.locator('.conversation-overlay')).toBeHidden();
  for (const tab of ['路径', '时间线', '任务', '排期']) await expect(page.getByRole('tab', { name: tab, exact: true })).toBeVisible();
  await assertCanvasClear('手机 390');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);

  expect(errors).toEqual([]);
});
