/**
 * 时间线按尺度直接写入正式计划:定向 E2E。
 *
 * 只验本轮新增的一件事 —— **时间线不再只是查看入口**:
 * - 月:点空白建月度里程碑 / 任务,写进正式 `PlanNode`,刷新后仍可见;
 * - 周:建本周计划,同周再点不重复;在某周计划下加任务,任务面板「本周」可见;
 * - 日:建今天的工作块,写进 `scheduled_sessions`,日轨道与首页「今天」可见。
 *
 * ## 为什么用 `page.route` 固定 `/reasoning`
 *
 * 时间线只在 V1 粗时间架构(`v1Stage` 非空且有 `v01Timeline`)下才走可创建的形态。
 * 跑一整条 OpenJiuwen 对话来得到它既慢又依赖模型;这里直接把**已经确认**的
 * 时间线投影喂给前端 —— `planNodeId` 指向用接口真实建出来的阶段节点,所以
 * 用户点的每一个创建动作,落的都是真库。
 */
import { expect, test, type Page } from '@playwright/test';
import {
  assertBackendRunning,
  createNode,
  createWorkspace,
  dayOffset,
  getPlan,
  getToday,
  openSpacePage,
  registerAccount,
} from './support/session';
import { dayNumber, weekBounds } from '../src/features/growth/timeline';

/** 喂给前端的 `/reasoning` 投影。字段形状对齐 `GoalReasoningView` 里前端真的会读的那些。 */
function reasoningView(workspaceId: string, rootId: string, phaseId: string, title: string, start: string, end: string) {
  return {
    workspaceId,
    sessionId: 's-timeline-create',
    rootPlanNodeId: rootId,
    phase: 'roadmap_draft',
    turnAction: 'analyze',
    status: 'ready',
    mapVersion: 5,
    focusHandle: null,
    focusReasoningNodeId: null,
    focusReason: null,
    intakeQuestionsAsked: 0,
    intakeQuestionLimit: 5,
    pendingIntake: null,
    workflowStage: null,
    discoveryQuestions: [],
    v1Stage: 'weekly_execution',
    v1Status: 'idle',
    v1WorkflowNext: null,
    v1VisibleAnalysisKeys: [],
    v1HiddenAnalysisCount: 7,
    v1ActualPendingQuestionCount: 0,
    v1FocusKey: null,
    v1Dimensions: [],
    v1StrategicThesis: null,
    v1CandidateDirections: null,
    v1Strategy: null,
    v1RequireOpenjiuwen: false,
    v01Timeline: [
      {
        id: 'rv-phase-1',
        title,
        kind: 'phase',
        index: 1,
        category: '基础闭环',
        startWeek: 1,
        endWeek: 4,
        startDate: start,
        endDate: end,
        goal: '跑通一个最小闭环',
        deliverable: '一个可展示的小工具',
        completionCriteria: '能演示',
        status: 'planned',
        planNodeId: phaseId,
      },
    ],
    v01TimelineProposalId: null,
    datesCalibrated: false,
    inputVersion: 'v1',
    strategyProposalId: null,
    exploredAt: null,
    lastEvaluatedAt: null,
    nodes: [],
    links: [],
    error: null,
  };
}

async function installTimelineMock(page: Page, view: Record<string, unknown>) {
  await page.route('**/api/workspaces/*/reasoning', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(view) });
  });
  await page.route('**/api/workspaces/*/questions*', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ questions: [], truncated: false }) });
  });
  // 进入空间会自动跑一轮 agent turn。它**不改变**这里的时间线投影,固定成同一份即可;
  // 不挡的话那几轮会打到真后端,推理视图被换成别的形状,测试就不再是在验这一条链。
  await page.route('**/api/workspaces/*/agent/turn', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ reasoning: view, message: null, question: null, replayed: false, degraded: false, degradedReason: null, retryable: false, changed: false, proposalErrors: [] }),
    });
  });
}

/** 点画布上第 `day` 天、纵向 `yFraction` 处的空白。x 由当前视口现算,不写死像素。 */
async function clickBlankAtDay(page: Page, day: number, yFraction = 0.96) {
  const canvas = page.getByTestId('timeline-canvas');
  const start = Number(await canvas.getAttribute('data-start'));
  const density = Number(await canvas.getAttribute('data-density'));
  const box = await canvas.boundingBox();
  if (!box) throw new Error('时间线画布没有尺寸');
  const x = box.x + Math.max(12, Math.min(box.width - 12, (day - start) * density));
  await page.mouse.click(x, box.y + box.height * yFraction);
}

async function zoomTo(page: Page, label: '月' | '周' | '天') {
  await page.getByTestId('timeline-presets').getByRole('button', { name: label, exact: true }).click();
  await expect(page.getByTestId('timeline-view')).toHaveAttribute(
    'data-zoom',
    label === '月' ? 'month' : label === '周' ? 'week' : 'day',
  );
}

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('月 / 周 / 日三个尺度都能直接写入正式计划,并同步到任务面板与首页', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));

  const { token } = await registerAccount(page, 'timeline-create');
  const workspaceId = await createWorkspace(page, token, '时间线写入验收空间', '直接把计划写在时间线上');
  const root = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!;

  // 阶段横跨今天前后各两周,好让月 / 周 / 日切过去时今天都在视野附近。
  const phaseTitle = '阶段一 · 打基础';
  const phaseId = await createNode(page, token, workspaceId, { parentId: root.id, title: phaseTitle, nodeType: 'stage' });
  const phaseStart = dayOffset(-14);
  const phaseEnd = dayOffset(14);
  await installTimelineMock(page, reasoningView(workspaceId, root.id, phaseId, phaseTitle, phaseStart, phaseEnd));

  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });
  const canvas = page.getByTestId('timeline-canvas');
  await expect(canvas).toHaveAttribute('data-ready', 'true');
  await expect(page.getByTestId('timeline-view')).toHaveAttribute('data-zoom', 'month');
  await expect(page.locator(`[data-testid="v1-phase-bar"][data-phase-id="rv-phase-1"]`)).toBeVisible();

  // ---- 月:创建月度里程碑 -------------------------------------------------------
  await clickBlankAtDay(page, dayNumber(dayOffset(3)));
  const monthPopover = page.getByTestId('timeline-create-popover');
  await expect(monthPopover).toBeVisible();
  await expect(monthPopover).toHaveAttribute('data-scale', 'month');
  await expect(monthPopover).toContainText(phaseTitle);
  await monthPopover.getByLabel('新建标题').fill('月度里程碑验收');
  await monthPopover.getByRole('button', { name: '创建', exact: true }).click();
  await expect(monthPopover).toHaveCount(0);

  // 真值在后端,不在浏览器内存里。
  await expect.poll(async () => (await getPlan(page, token, workspaceId)).nodes.find(node => node.title === '月度里程碑验收')?.nodeType).toBe('milestone');
  const milestone = (await getPlan(page, token, workspaceId)).nodes.find(node => node.title === '月度里程碑验收')!;
  expect(milestone.parentId).toBe(phaseId);
  expect(milestone.planningLevel).toBe('month');
  // 刷新之后时间线上仍可见(月轨道)。
  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });
  await expect(page.locator(`[data-testid="v1-month-node"][data-node-id="${milestone.id}"]`)).toBeVisible();

  // 任务面板也看得到它。
  await openSpacePage(page, '/workbench', workspaceId, { view: 'tasks' });
  await expect(page.locator('.task-view')).toContainText('月度里程碑验收');

  // ---- 周:创建该阶段的本周计划,同周再点不重复 --------------------------------
  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });
  await zoomTo(page, '周');
  const weekStart = weekBounds(dayOffset(7)).start;
  const weekMid = dayNumber(weekStart) + 2;

  await clickBlankAtDay(page, weekMid);
  const weekPopover = page.getByTestId('timeline-create-popover');
  await expect(weekPopover).toBeVisible();
  await expect(weekPopover).toHaveAttribute('data-scale', 'week');
  await expect(weekPopover).toContainText('这一周还没有周计划');
  await weekPopover.getByRole('button', { name: '创建本周计划' }).click();
  await expect(weekPopover).toHaveCount(0);

  await expect.poll(async () => (await getPlan(page, token, workspaceId)).nodes.filter(node => node.nodeType === 'stage' && node.title.startsWith(`本周计划:${phaseTitle}`)).length).toBe(1);

  // 同一周再点:浮层变成"加入本周计划",不会多出第二份周计划。
  await clickBlankAtDay(page, weekMid);
  const again = page.getByTestId('timeline-create-popover');
  await expect(again).toBeVisible();
  await expect(again.getByRole('button', { name: '加入本周计划' })).toBeVisible();
  await expect(again.getByRole('button', { name: '创建本周计划' })).toHaveCount(0);
  await again.getByLabel('新建标题').fill('本周验收任务');
  await again.getByRole('button', { name: '加入本周计划' }).click();
  await expect(again).toHaveCount(0);

  const weeks = (await getPlan(page, token, workspaceId)).nodes.filter(node => node.nodeType === 'stage' && node.title.startsWith(`本周计划:${phaseTitle}`));
  expect(weeks).toHaveLength(1);
  const weekPlan = weeks[0];
  const weekTask = (await getPlan(page, token, workspaceId)).nodes.find(node => node.title === '本周验收任务')!;
  expect(weekTask.parentId).toBe(weekPlan.id);
  // 周轨道显示真实创建的那份与它的任务摘要。
  await expect(page.locator(`[data-testid="v1-week-bar"][data-week-id="${weekPlan.id}"]`)).toBeVisible();

  // 「本周」筛选必须能看到**没有具体日期、但挂在本周计划下**的任务。
  await openSpacePage(page, '/workbench', workspaceId, { view: 'tasks' });
  await page.getByRole('button', { name: '本周', exact: true }).click();
  await expect(page.locator('.task-view')).toContainText('本周验收任务');

  // ---- 日:创建今天的工作块 -----------------------------------------------------
  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });
  await zoomTo(page, '天');
  const todayDay = dayNumber(dayOffset(0));
  await clickBlankAtDay(page, todayDay);
  const dayPopover = page.getByTestId('timeline-create-popover');
  await expect(dayPopover).toBeVisible();
  await expect(dayPopover).toHaveAttribute('data-scale', 'day');
  await dayPopover.getByLabel('新建标题').fill('今天的工作块');
  await dayPopover.getByLabel('工作块分钟数').fill('45');
  await dayPopover.getByRole('button', { name: '排入这一天' }).click();
  await expect(dayPopover).toHaveCount(0);

  // 写入的是既有排期 session(带 scheduledDate),不是另一套时间线任务。
  await expect.poll(async () => (await getToday(page, token)).items.some(item => item.nodeTitle === '今天的工作块')).toBe(true);
  const today = await getToday(page, token);
  expect(today.items.find(item => item.nodeTitle === '今天的工作块')?.plannedMinutes).toBe(45);
  // 日轨道立刻显示。
  await expect(page.getByTestId('v1-day-block').filter({ hasText: '今天的工作块' })).toBeVisible();

  // 首页「今天」也能看到它。
  await page.goto(`/today?workspace=${workspaceId}`);
  await expect(page.locator('.today-item-title').filter({ hasText: '今天的工作块' })).toBeVisible({ timeout: 20000 });

  expect(errors).toEqual([]);
});

/**
 * 保存失败要显示真实失败状态,并保留输入草稿。
 *
 * 直接让 `/sessions` 返回 500:节点其实建成了,但工作块没写成 —— 界面必须如实说,
 * 并把用户填的标题留在输入框里。
 */
test('写入失败时如实报错并保留草稿', async ({ page }) => {
  const { token } = await registerAccount(page, 'timeline-create-fail');
  const workspaceId = await createWorkspace(page, token, '时间线写入失败空间', '失败要说出来');
  const root = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!;
  const phaseTitle = '阶段失败 · 保底';
  const phaseId = await createNode(page, token, workspaceId, { parentId: root.id, title: phaseTitle, nodeType: 'stage' });
  await installTimelineMock(page, reasoningView(workspaceId, root.id, phaseId, phaseTitle, dayOffset(-5), dayOffset(5)));

  // 让日工作块落库失败。
  await page.route('**/api/workspaces/*/sessions', route => route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: { code: 'INTERNAL', message: '服务端没能写入这一场。' } }) }));

  await openSpacePage(page, '/workbench', workspaceId, { view: 'timeline' });
  await zoomTo(page, '天');
  await clickBlankAtDay(page, dayNumber(dayOffset(0)));
  const popover = page.getByTestId('timeline-create-popover');
  await expect(popover).toBeVisible();
  await popover.getByLabel('新建标题').fill('还没写成的块');
  await popover.getByRole('button', { name: '排入这一天' }).click();

  // 浮层没关、草稿还在、错误如实显示。
  await expect(popover).toBeVisible();
  await expect(popover.getByLabel('新建标题')).toHaveValue('还没写成的块');
  await expect(popover.getByRole('alert')).toContainText('服务端没能写入这一场');
});
