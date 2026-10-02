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
import { anchoredZoom, dateToX, dayNumber, getVisibleItems, layoutItems, timelineItems, timelineTicks, xToDate } from '../src/features/growth/timeline';
import type { GrowthNode, GrowthState } from '../src/types/growth';

/**
 * 时间线。
 *
 * ## 第一条是纯函数,单位是"天"
 *
 * 日历映射、跨年刻度、语义层级、密集布局 —— 这些都不需要浏览器,也不需要一个空间。
 * 上一版它读的是示例空间那份快照(`src/mock/growth-state.ts` 的 `initialGrowth`),
 * 而那份快照已经删掉了。这里改成**就地搭一份最小计划**:被测的东西(`timeline.ts`)
 * 一个字没动,换掉的只是喂给它的数据。用示例数据当夹具还有个隐患 —— 夹具一变,
 * 断言里的数字要跟着变,而失败信息看起来像"布局算错了"。
 *
 * ## 第二条走真实空间
 *
 * 时间线整条都改成真实空间了(见 `TimelineView.tsx`):卡片画的永远是计划里真实
 * 的那一天。所以这条测试也必须在真实空间里跑 —— 空间和节点由它自己通过接口搭出来。
 *
 * 上一版这条测试的后半段点的是**示例空间的本地提案**:"查看影响"→ 预览幽灵 →
 * "接受调整"。那套东西整个删了(它改的是一场真实存在的安排,而写入路径是「排期」,
 * 后端根本没有那个字段)。它的位置现在由「改截止时间」接手 —— 那是**真的会写进
 * 后端**的那一条路;而"改日期"那条路现在只会给出一句诚实的说明,这一条也钉在下面。
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

test('语义缩放、锚点、平移、概览、卡片布局，以及改截止时间真的写进后端', async ({ page }) => {
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
  // 同一天挤十件:月这一层画不下,必须有一部分进"另有 N 项"。事件在**真实空间**
  // 里也要能展开得到 —— 这正是不该拿示例数据当夹具的原因。
  for (let index = 0; index < 10; index++) {
    await createNode(page, token, workspaceId, { parentId: root.id, title: `同日安排 ${index}`, nodeType: 'task', deadline: crowded });
  }

  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });

  // **计划到了没有,这条测试要自己认。** `waitForRealPlan` 读的是 `.react-flow__node`,
  // 那是路径视图的东西 —— 时间线这一页画布上一个 ReactFlow 节点都没有,拿它当门会
  // 一直等到超时,而报出来的是"计划没有从后端到达"。这里等的是**这个空间里那件
  // 有截止日的事**被画出来:它出现就等于后端那一份计划到了。
  //
  // 而"看得见"要判在**卡片**上,不是外面那层 `[data-timeline-item]`:外层 div 里装的
  // 全是绝对定位的子元素,它自己高度是 0 —— Playwright 判它"不可见",而它在屏幕上
  // 明明画着。用 `toHaveCount` 数外层是可以的,判可见性不行。
  const timeline = page.getByTestId('timeline-view'), canvas = page.getByTestId('timeline-canvas');
  await expect(page.locator(`[data-timeline-item="${dated}"] [data-timeline-card]`)).toBeVisible();
  await expect(timeline).toHaveAttribute('data-zoom', 'month');
  await expect(canvas).toHaveAttribute('data-ready', 'true');
  await expect(page.getByTestId('today-marker')).toBeVisible();

  // 卡片互不重叠 —— 这是"信息分层"这个设计的全部意义。
  const cards = await page.locator('[data-timeline-card]').evaluateAll(elements => elements.map(e => { const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, right: r.right, bottom: r.bottom }; }));
  expect(cards.length).toBeGreaterThan(0);
  for (let i = 0; i < cards.length; i++) for (let j = i + 1; j < cards.length; j++) {
    const a = cards[i], b = cards[j];
    expect(a.right <= b.x || b.right <= a.x || a.bottom <= b.y || b.bottom <= a.y, '两张卡片叠在一起了').toBeTruthy();
  }

  // --- 语义缩放:换一层,画的东西跟着换 ----------------------------------------
  await page.getByRole('group', { name: '时间尺度' }).getByRole('button', { name: '年', exact: true }).click();
  await expect(timeline).toHaveAttribute('data-zoom', 'year');
  // 年这一层画的是目标,不画具体任务。**不画不等于丢了** —— 它们在天那一层。
  await expect(page.locator(`[data-timeline-item="${root.id}"]`)).toHaveCount(1);
  await expect(page.locator(`[data-timeline-item="${dated}"]`)).toHaveCount(0);
  // 季度这一层画的是阶段与重要节点:阶段出来了,挂在它下面的具体任务还没出来。
  await page.getByRole('group', { name: '时间尺度' }).getByRole('button', { name: '季度', exact: true }).click();
  await expect(timeline).toHaveAttribute('data-zoom', 'quarter');
  await expect(page.locator(`[data-timeline-item="${stage}"]`)).toHaveCount(1);
  await page.getByRole('group', { name: '时间尺度' }).getByRole('button', { name: '月', exact: true }).click();
  await expect(page.locator(`[data-timeline-item="${stage}"]`)).toHaveCount(0);
  await expect(page.locator(`[data-timeline-item="${dated}"]`)).toHaveCount(1);

  // --- 放不下的那一批:界面上必须有一条出路 -------------------------------------
  await page.getByRole('button', { name: /另有 \d+ 项/ }).click();
  const panel = page.locator('[data-cluster-panel]');
  await expect(panel).toBeVisible();
  // 面板里每条安排都是一个带 `<small>`(日期)的按钮;表头那个"关闭"按钮没有。
  const hiddenButtons = panel.locator('button:has(small)');
  expect(await hiddenButtons.count()).toBeGreaterThan(0);
  const firstHidden = (await hiddenButtons.first().innerText()).split('\n')[0].trim();
  await hiddenButtons.first().click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId('date-inspector')).toContainText(firstHidden);

  // --- 今天 + 天尺度 --------------------------------------------------------------
  await page.getByRole('button', { name: '今天', exact: true }).click();
  await page.getByRole('group', { name: '时间尺度' }).getByRole('button', { name: '天', exact: true }).click();
  await expect(timeline).toHaveAttribute('data-zoom', 'day');

  // --- Ctrl + 滚轮缩放:锚点底下那一天不能动 --------------------------------------
  await page.getByRole('group', { name: '时间尺度' }).getByRole('button', { name: '月', exact: true }).click();
  const box = await canvas.boundingBox();
  if (!box) throw new Error('时间线画布没有尺寸');
  const clientX = Math.round(box.x + box.width * .65), pointerX = clientX - box.x;
  const before = { start: Number(await canvas.getAttribute('data-start')), density: Number(await canvas.getAttribute('data-density')) };
  await canvas.dispatchEvent('wheel', { clientX, clientY: box.y + 100, deltaY: -80, ctrlKey: true });
  await expect.poll(async () => Number(await canvas.getAttribute('data-density'))).toBeGreaterThan(before.density);
  const after = { start: Number(await canvas.getAttribute('data-start')), density: Number(await canvas.getAttribute('data-density')) };
  // 缩放前指针底下是 `before.start + pointerX/before.density` 那一天,缩放后还得是它。
  expect(after.start + pointerX / after.density).toBeCloseTo(before.start + pointerX / before.density, 5);

  // --- 拖动空白平移 ---------------------------------------------------------------
  await page.mouse.move(box.x + box.width * .8, box.y + box.height - 22);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * .8 - 100, box.y + box.height - 22, { steps: 5 });
  await page.mouse.up();
  await expect.poll(async () => Number(await canvas.getAttribute('data-start'))).toBeGreaterThan(after.start);

  const panStart = Number(await canvas.getAttribute('data-start'));
  const overview = page.getByRole('slider', { name: '概览视窗位置' });
  await overview.focus();
  await overview.press('ArrowLeft');
  await expect.poll(async () => Number(await canvas.getAttribute('data-start'))).toBeLessThan(panStart);

  // --- 改截止时间:这是**真的会写进后端**的那条路 --------------------------------
  await page.getByRole('button', { name: '今天', exact: true }).click();
  await page.locator(`[data-timeline-item="${dated}"] [data-timeline-card]`).click();
  const inspector = page.getByTestId('date-inspector');
  await expect(inspector).toContainText('有截止日的任务');
  // 说"截止 2026-11-05（还没排出具体安排）",而不是一个光秃秃的 "2026-11-05" ——
  // 后者读起来像"这一天要做这件事",而那一天其实什么都没排。
  await expect(inspector).toContainText('还没排出具体安排');
  // 截止日**不是**排期,所以这里不给"编辑日期"这个入口。给了的话,用户会以为自己
  // 在改安排,而写下去的其实是截止时间。
  await expect(page.getByRole('button', { name: '编辑日期' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '改截止时间' })).toBeVisible();

  await page.getByRole('button', { name: '改截止时间' }).click();
  const moved = dayOffset(60);
  await page.getByLabel('截止日期').fill(moved);
  await page.getByRole('button', { name: '应用' }).click();
  // 真值从**计划**里读:界面上的日期可能是乐观更新,后端那一行才算数。
  await expect.poll(async () => (await getPlan(page, token, workspaceId)).nodes.find(item => item.id === dated)?.deadline).toBe(moved);

  // --- 示例空间那套"本地提案"没有回来 --------------------------------------------
  //
  // 这里原来是这条测试的后半段:点开卡片 → "查看影响" → 预览幽灵 → "接受调整",
  // 把卡片拖到 2027-02-01。那套东西改的是一场真实存在的安排,而写入路径是「排期」,
  // 后端根本没有 `startDate` 这个字段 —— 悄悄拿 `deadline` 当排期用会改掉用户设的
  // 截止时间,界面上的说辞却是"调整了安排"。所以它整个删了。
  //
  // 这一节钉的是**它没有以任何形式回来**:草案、幽灵、接受按钮,一个都不该在。
  await expect(page.getByTestId('plan-ghost')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '查看影响' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '接受调整' })).toHaveCount(0);

  // 刷新之后截止日还是新的那个:真相在后端,不在浏览器内存里。
  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });
  await expect(page.locator(`[data-timeline-item="${dated}"]`)).toHaveAttribute('data-start-date', moved);

  expect(errors).toEqual([]);
});

/**
 * 时间线工具栏不被工作台的浮动组件盖住。
 *
 * ## 为什么单开一条,而不是塞进上面那条
 *
 * 上面那条测的是**行为**;这一条测的是**几何**。混在一起的话,一次"工具栏被盖住"
 * 会先红在某个点击上,报出来是"元素不可见/点不动",读的人要顺着堆栈往回找才会想到
 * 是布局 —— 它长得像被测功能坏了。
 *
 * ## 这条几何到底在防什么
 *
 * 时间线是这一页里唯一**从上往下排**的视图。`.view-tabs`(四个视图入口)是绝对定位的
 * 浮动组件,`top:16px`,`z-index:25`;时间线的工具栏在普通流里,顶边原本只有 20px
 * 内边距 —— 于是整组"年/季度/月/周/天"落在它底下,而点击被它接走。**失败的样子是
 * "按钮点不动"**,不是"两个东西叠在一起",因为叠在上面的那一层本身就是可交互的元素
 * (四个视图标签)。
 *
 * ## 两条断言,一近一远
 *
 * 1. **结构性不变量**:工具栏的顶边必须在浮动组件的下沿之下。
 *    这条说的是"为什么",而且**不依赖浮动的宽度** —— 面包屑里的空间名短的时候,
 *    它自己可能只有两百来像素宽,按钮不一定落在它底下,`elementFromPoint` 就未必
 *    红。不变量是"无论多宽都不许叠",所以拿它兜底。
 * 2. **后果**:`elementFromPoint` 打在「年」按钮正中心,命中的必须是那个按钮自己。
 *    这条说的是"用户真的点得到",和上面那条是两件事 —— 一条守意图,一条守结果。
 *
 * ## 两档视口,同一组断言
 *
 * 这一条原来只跑默认视口(1440),注释里写着 ≤720 那一档"和手机工作台一起重做" ——
 * 手机工作台就是这一批。<=480 那一档浮动组件收成了**一行**(见 `ui-refresh.css`
 * 里那一档),但"工具栏要让开浮动组件的下沿"这件事没有变,于是同一组断言在两档上
 * 都跑一遍:1440 先跑,再把视口收成 390 复跑。**不是两次点击,是同一个判据在两种
 * 布局下都必须成立** —— 只钉一档的话,改窄屏样式的人没有任何东西会红。
 *
 * 断点生效的等待信号是".conversation-overlay 收起来了":≤480 那一档 AI 面板默认
 * 收起(`Workbench.tsx` 里 `chatChoice ?? !narrow`),而桌面默认是展开的 —— 它变成
 * `hidden` 就说明 `useMobileLayout` 真的重渲染过了,后面的几何才是新布局的几何。
 */
test('时间线工具栏不被浮动路径与视图切换盖住', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));

  const { token } = await registerAccount(page, 'timeline-geometry');
  const workspaceId = await createWorkspace(page, token, '时间线几何验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  // 建一个节点:让这一页落在**计划已经到达**的状态上,而不是那份还没连上后端的
  // 占位树。占位状态下工具栏也在,但拿一个"还没准备好"的界面验几何,红了分不清
  // 是布局错了还是页面还没到。
  const dated = await createNode(page, token, workspaceId, {
    parentId: root.id, title: '几何用例的节点', nodeType: 'task', deadline: dayOffset(20),
  });

  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });
  await expect(page.locator(`[data-timeline-item="${dated}"] [data-timeline-card]`)).toBeVisible();

  const scales = page.getByRole('group', { name: '时间尺度' });
  // `exact` 是必须的:五个尺度里「季度」也含「年」字,不精确匹配会同时命中两个。
  const year = scales.getByRole('button', { name: '年', exact: true });
  await expect(year).toBeVisible();

  /**
   * 一档视口上把两件事都量一遍。两档共用它,是为了让"这一条到底主张什么"只有一处定义。
   * @param label 报错时用来说清是**哪一档**红的 —— 只说"被盖住了"的话,读的人第一件
   *   事是回去数视口宽度。
   */
  const assertToolbarClear = async (label: string) => {
    // --- 1. 结构性不变量:工具栏顶边在浮动组件下沿之下 ----------------------------
    //
    // 比的是 `getBoundingClientRect` 意义上的**同一坐标系**(都以视口左上角为原点),
    // 所以三个 `boundingBox()` 可以直接比。
    const [toolbar, tabs] = await Promise.all([
      scales.boundingBox(), page.locator('.view-tabs').boundingBox(),
    ]);
    if (!toolbar) throw new Error(`${label}:时间线工具栏没有尺寸,几何断言无从谈起`);
    if (!tabs) throw new Error(`${label}:浮动组件「视图切换」没有尺寸,几何断言无从谈起`);
    expect(
      toolbar.y,
      `${label}:时间线工具栏的顶边(${Math.round(toolbar.y)}px)落在「视图切换」的下沿(${Math.round(tabs.y + tabs.height)}px)之上`,
    ).toBeGreaterThanOrEqual(tabs.y + tabs.height);

    // --- 2. 后果:按钮正中心的那一点,归按钮自己 --------------------------------
    const hit = await year.evaluate(element => {
      const rect = element.getBoundingClientRect();
      const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return {
        // `contains` 是给按钮内部那个文字/图标节点留的余地 —— 命中的是它自己或它的后代
        // 都算"点得到它"。
        self: top === element || element.contains(top),
        // 没命中时把**到底盖着什么**报出来。只报一句 `expected true` 的话,下一个人
        // 还得自己去猜是哪一个浮动组件压上来了。
        blockedBy: top ? `${top.tagName.toLowerCase()}.${String((top as HTMLElement).className)}` : 'null',
        covered: top ? (top.textContent ?? '').trim().slice(0, 20) : '',
      };
    });
    expect(hit.self, `${label}:「年」按钮的中心被 ${hit.blockedBy}${hit.covered ? `(${hit.covered})` : ''} 盖住了`).toBe(true);
  };

  await assertToolbarClear('桌面 1440');

  // --- 3. 同一组断言,手机那一档再来一遍 ----------------------------------------
  await page.setViewportSize({ width: 390, height: 844 });
  /*
   * 等到断点真的生效。**不用定时器**:`≤480` 那一档 AI 面板默认收起,而桌面默认展开,
   * 它变成 `hidden` 就是"`useMobileLayout` 已经按新宽度重渲染过"的证据。
   */
  await expect(page.locator('.conversation-overlay')).toBeHidden();
  // 四个视图入口一个不少 —— 窄屏只是把文字收进图标里,`aria-label` 还在
  // (`Workbench.tsx` 里那段),所以这里按名字找仍然找得到。
  for (const tab of ['路径', '时间线', '任务', '排期']) await expect(page.getByRole('tab', { name: tab, exact: true })).toBeVisible();
  await assertToolbarClear('手机 390');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);

  expect(errors).toEqual([]);
});
