/**
 * 首页「本周计划 / 本日计划」两个页签:跨空间聚合、可编辑、可同步。
 *
 * ## 这一条钉的是什么
 *
 * 1. 三个旧卡片(当前阶段 / 下一里程碑 / 本周重点)不再出现;
 * 2. 本周计划页签:两个空间的任务同时出现、按空间分组、没有日期的任务也在、
 *    归档的不在;添加后立即出现;
 * 3. 本日计划页签:两个空间的今日工作块同时出现;添加今日任务后,首页、对应空间
 *    日级时间线、任务面板「今天」都看得到;
 * 4. 勾选完成在首页与任务面板之间同步。
 *
 * 数据全部由真实接口建出来;`/reasoning` 固定成一份已确认的 V1 时间线,好让工作台
 * 的日级时间线走 V1 形态(日轨道),不跑一整条对话。
 */
import { expect, test, type Page } from '@playwright/test';
import {
  api,
  assertBackendRunning,
  createNode,
  createWorkspace,
  dayOffset,
  getPlan,
  openSpacePage,
  registerAccount,
} from './support/session';
import { weekBounds } from '../src/features/growth/timeline';

function reasoningView(workspaceId: string, rootId: string, phaseId: string, title: string, start: string, end: string) {
  return {
    workspaceId,
    sessionId: 's-home-tabs',
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
    v1HiddenAnalysisCount: 0,
    v1ActualPendingQuestionCount: 0,
    v1FocusKey: null,
    v1Dimensions: [],
    v1StrategicThesis: null,
    v1CandidateDirections: null,
    v1Strategy: null,
    v1RequireOpenjiuwen: false,
    v01Timeline: [
      {
        id: 'home-phase-1',
        title,
        kind: 'phase',
        index: 1,
        category: '基础闭环',
        startWeek: 1,
        endWeek: 4,
        startDate: start,
        endDate: end,
        goal: '跑通最小闭环',
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

async function installReasoningMock(page: Page, view: Record<string, unknown>) {
  await page.route('**/api/workspaces/*/reasoning', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(view) });
  });
  await page.route('**/api/workspaces/*/agent/turn', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ reasoning: view, message: null, question: null, replayed: false, degraded: false, degradedReason: null, retryable: false, changed: false, proposalErrors: [] }),
    });
  });
}

async function createWeekPlan(page: Page, token: string, workspaceId: string, parentId: string, weekStart: string): Promise<string> {
  const created = await api<{ node: { id: string } }>(page, token, `/api/workspaces/${workspaceId}/week-plans`, {
    method: 'POST',
    data: { parentId, weekStart },
  });
  return created.node.id;
}

async function createSession(page: Page, token: string, workspaceId: string, nodeId: string, scheduledDate: string, plannedMinutes: number): Promise<void> {
  await api(page, token, `/api/workspaces/${workspaceId}/sessions`, {
    method: 'POST',
    data: { nodeId, scheduledDate, plannedMinutes },
  });
}

test.beforeAll(async ({ request }) => { await assertBackendRunning(request); });

test('首页两个页签:跨空间聚合、无日期任务、归档排除、添加任务立即可见', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));

  const { token } = await registerAccount(page, 'home-tabs');
  const workspaceA = await createWorkspace(page, token, '空间甲');
  const workspaceB = await createWorkspace(page, token, '空间乙');
  const today = dayOffset(0);
  const monday = weekBounds(today).start;

  const rootA = (await getPlan(page, token, workspaceA)).nodes.find(node => node.parentId === null)!.id;
  const phaseA = await createNode(page, token, workspaceA, { parentId: rootA, title: '阶段甲', nodeType: 'stage' });
  const weekA = await createWeekPlan(page, token, workspaceA, phaseA, monday);
  await createNode(page, token, workspaceA, { parentId: weekA, title: '甲·无日期任务', nodeType: 'task' });
  const todayA = await createNode(page, token, workspaceA, { parentId: weekA, title: '甲·今日工作块', nodeType: 'task' });
  await createSession(page, token, workspaceA, todayA, today, 45);
  // 归档的任务不该出现在首页。
  const archivedA = await createNode(page, token, workspaceA, { parentId: weekA, title: '甲·归档任务', nodeType: 'task' });
  await api(page, token, `/api/workspaces/${workspaceA}/nodes/${archivedA}`, { method: 'PATCH', data: { status: 'archived' } });

  const rootB = (await getPlan(page, token, workspaceB)).nodes.find(node => node.parentId === null)!.id;
  const phaseB = await createNode(page, token, workspaceB, { parentId: rootB, title: '阶段乙', nodeType: 'stage' });
  const weekB = await createWeekPlan(page, token, workspaceB, phaseB, monday);
  await createNode(page, token, workspaceB, { parentId: weekB, title: '乙·无日期任务', nodeType: 'task' });
  const todayB = await createNode(page, token, workspaceB, { parentId: weekB, title: '乙·今日工作块', nodeType: 'task' });
  await createSession(page, token, workspaceB, todayB, today, 30);

  await installReasoningMock(page, reasoningView(workspaceB, rootB, phaseB, '阶段乙', dayOffset(-14), dayOffset(14)));

  await page.goto(`/today?workspace=${workspaceA}`);

  // 三个旧卡片不再出现。
  await expect(page.getByTestId('plan-focus')).toHaveCount(0);
  await expect(page.getByText('当前阶段')).toHaveCount(0);
  await expect(page.getByText('下一里程碑')).toHaveCount(0);
  await expect(page.getByText('本周重点')).toHaveCount(0);

  const plan = page.getByTestId('today-plan');
  await expect(plan).toBeVisible({ timeout: 20000 });

  // 默认打开本日计划:两个空间的今日工作块同时出现。
  await expect(plan.getByRole('tab', { name: '本日计划' })).toHaveAttribute('aria-selected', 'true');
  await expect(plan.getByTestId('day-item').filter({ hasText: '甲·今日工作块' })).toBeVisible();
  await expect(plan.getByTestId('day-item').filter({ hasText: '乙·今日工作块' })).toBeVisible();
  await expect(plan.getByTestId('day-item').filter({ hasText: '甲·今日工作块' })).toContainText('空间甲');

  // 本周计划:两个空间分组,无日期任务可见,归档不可见。
  await plan.getByRole('tab', { name: '本周计划' }).click();
  await expect(plan.getByTestId('week-groups')).toBeVisible();
  await expect(plan.locator('.today-plan-group')).toHaveCount(2);
  await expect(plan.getByTestId('week-task').filter({ hasText: '甲·无日期任务' })).toBeVisible();
  await expect(plan.getByTestId('week-task').filter({ hasText: '乙·无日期任务' })).toBeVisible();
  await expect(plan.getByTestId('week-task').filter({ hasText: '甲·归档任务' })).toHaveCount(0);

  // 添加本周任务到空间乙。
  await plan.getByRole('button', { name: '添加本周任务' }).first().click();
  const form = page.getByTestId('today-plan-form');
  await expect(form).toBeVisible();
  await form.getByLabel('空间').selectOption({ label: '空间乙' });
  await form.getByLabel('所属阶段').selectOption({ label: '阶段乙' });
  await form.getByLabel('标题').fill('乙·新加的本周任务');
  await form.getByRole('button', { name: '添加', exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(plan.getByTestId('week-task').filter({ hasText: '乙·新加的本周任务' })).toBeVisible();
  await expect.poll(async () => (await getPlan(page, token, workspaceB)).nodes.some(node => node.title === '乙·新加的本周任务')).toBe(true);

  // 添加今日任务到空间乙。
  await plan.getByRole('tab', { name: '本日计划' }).click();
  await plan.getByRole('button', { name: '添加今日任务' }).first().click();
  const dayForm = page.getByTestId('today-plan-form');
  await expect(dayForm).toBeVisible();
  await dayForm.getByLabel('空间').selectOption({ label: '空间乙' });
  await dayForm.getByLabel('所属阶段').selectOption({ label: '阶段乙' });
  await dayForm.getByLabel('标题').fill('乙·新加的今日任务');
  await dayForm.getByRole('button', { name: '添加', exact: true }).click();
  await expect(dayForm).toHaveCount(0);
  await expect(plan.getByTestId('day-item').filter({ hasText: '乙·新加的今日任务' })).toBeVisible();

  // 对应空间的日级时间线立即出现。
  await openSpacePage(page, '/workbench', workspaceB, { view: 'timeline' });
  await page.getByTestId('timeline-presets').getByRole('button', { name: '天', exact: true }).click();
  await expect(page.getByTestId('timeline-view')).toHaveAttribute('data-zoom', 'day');
  await expect(page.getByTestId('v1-day-block').filter({ hasText: '乙·新加的今日任务' })).toBeVisible();

  // 任务面板「今天」也看得到。
  await openSpacePage(page, '/workbench', workspaceB, { view: 'tasks' });
  await page.getByRole('button', { name: '今天', exact: true }).click();
  await expect(page.locator('.task-view')).toContainText('乙·新加的今日任务');

  expect(errors).toEqual([]);
});

test('首页勾选完成与任务面板同步', async ({ page }) => {
  const { token } = await registerAccount(page, 'home-tabs-toggle');
  const workspace = await createWorkspace(page, token, '同步空间');
  const today = dayOffset(0);
  const monday = weekBounds(today).start;

  const root = (await getPlan(page, token, workspace)).nodes.find(node => node.parentId === null)!.id;
  const phase = await createNode(page, token, workspace, { parentId: root, title: '同步阶段', nodeType: 'stage' });
  const week = await createWeekPlan(page, token, workspace, phase, monday);
  const task = await createNode(page, token, workspace, { parentId: week, title: '要勾掉的任务', nodeType: 'task' });
  await createSession(page, token, workspace, task, today, 20);

  await page.goto(`/today?workspace=${workspace}`);
  const plan = page.getByTestId('today-plan');
  await expect(plan).toBeVisible({ timeout: 20000 });
  const row = plan.getByTestId('day-item').filter({ hasText: '要勾掉的任务' });
  await expect(row).toBeVisible();

  // 勾上:首页行进入完成态。
  await row.getByRole('button', { name: /完成要勾掉的任务/ }).click();
  await expect(row).toHaveClass(/done/, { timeout: 20000 });
  await expect.poll(async () => (await getPlan(page, token, workspace)).nodes.find(node => node.id === task)?.status).toBe('completed');

  // 任务面板「今天」里也是完成态。
  await openSpacePage(page, '/workbench', workspace, { view: 'tasks' });
  await page.getByRole('button', { name: '今天', exact: true }).click();
  const taskRow = page.locator('.task-row', { hasText: '要勾掉的任务' });
  await expect(taskRow).toHaveClass(/completed-row/);

  // 再打开首页:状态仍然是完成(真相在库里)。
  await page.goto(`/today?workspace=${workspace}`);
  await expect(page.getByTestId('today-plan').getByTestId('day-item').filter({ hasText: '要勾掉的任务' })).toHaveClass(/done/, { timeout: 20000 });
});
