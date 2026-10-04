import { expect, test, type Page } from '@playwright/test';
import { assertBackendRunning, createWorkspace, registerAccount } from './support/session';

/**
 * TaskView 分组回归。
 *
 * ## 这条测试防的是什么
 *
 * 旧 `TaskView` 把**所有**非示例分类的桶都写成 `id: 'general'`。真实空间的节点
 * 没有示例分类,靠 `stageId` 兜底分组 —— 于是“阶段 A 的任务”和“阶段 B 的任务”
 * 是两个不同的桶,却拿到同一个 React key `general`,触发
 * `Encountered two children with the same key, general`,任务组会重复/缺失/错位。
 *
 * 这里用**两个阶段各挂一个任务**、各自带一场本周的排期,把那个场景固定下来:
 * 分组 id 必须唯一(`general:<stageId>`),两个任务都必须出现,完成进度正确。
 */

const NOW = new Date().toISOString();
const TODAY = new Intl.DateTimeFormat('en-CA', {
  year: 'numeric', month: '2-digit', day: '2-digit',
}).format(new Date());

function node(over: Record<string, unknown>) {
  return {
    description: null, acceptanceCriteria: null, purpose: 'planning', planningLevel: null,
    status: 'pending', priority: 'medium', estimateMinutes: null, deadline: null,
    depth: 0, orderIndex: 0, origin: 'user', completedAt: null, createdAt: NOW,
    contentVersion: 1, v1Key: null, v1Analysis: null, ...over,
  };
}

function session(id: string, nodeId: string, nodeTitle: string) {
  return {
    id, nodeId, workspaceId: 'w', nodeTitle, scheduledDate: TODAY, plannedMinutes: 30,
    bufferMinutes: 0, actualMinutes: null, seq: 0, status: 'planned', locked: false,
    lockReason: null, origin: 'scheduler', startMinute: null, endMinute: null, completedAt: null,
  };
}

function baseReasoning(workspaceId: string, rootId: string) {
  return {
    workspaceId, sessionId: 's', rootPlanNodeId: rootId, phase: 'roadmap_draft', turnAction: 'analyze',
    status: 'ready', mapVersion: 1, focusHandle: null, focusReasoningNodeId: null, focusReason: null,
    intakeQuestionsAsked: 0, intakeQuestionLimit: 5, pendingIntake: null, workflowStage: null,
    discoveryQuestions: [], v1Stage: null, v1Status: 'idle', v1WorkflowNext: null,
    v1VisibleAnalysisKeys: [], v1HiddenAnalysisCount: 0, v1ActualPendingQuestionCount: 0,
    v1FocusKey: null, v1Dimensions: [], v1StrategicThesis: '', v1CandidateDirections: null,
    v1Strategy: null, v1RequireOpenjiuwen: true, v01Timeline: [], v01TimelineProposalId: null,
    datesCalibrated: false, inputVersion: 'v1', strategyProposalId: null, exploredAt: null,
    lastEvaluatedAt: null, nodes: [], links: [], error: null,
  };
}

async function installPlanMock(page: Page, workspaceId: string, rootId: string, payload: Record<string, unknown>) {
  await page.route('**/api/workspaces/*/plan', route =>
    route.request().method() === 'GET'
      ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) })
      : route.continue(),
  );
  await page.route('**/api/workspaces/*/reasoning', route =>
    route.request().method() === 'GET'
      ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(baseReasoning(workspaceId, rootId)) })
      : route.continue(),
  );
  await page.route('**/api/workspaces/*/questions*', route =>
    route.request().method() === 'GET'
      ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ questions: [], truncated: false }) })
      : route.continue(),
  );
}

test.beforeAll(async ({ request }) => { await assertBackendRunning(request); });

test('两个阶段各一个任务:分组 id 唯一,无 duplicate key,切换范围任务不重不漏', async ({ page }) => {
  const { token } = await registerAccount(page, 'task-groups');
  const workspaceId = await createWorkspace(page, token, '任务分组', '两个阶段各一个任务');
  const rootId = `root-${workspaceId}`;

  const payload = {
    workspaceId,
    revisionVersion: 1,
    nodes: [
      node({ id: rootId, parentId: null, title: '根目标', nodeType: 'goal', depth: 0 }),
      node({ id: `stage-a-${workspaceId}`, parentId: rootId, title: '阶段 A', nodeType: 'stage', depth: 1, orderIndex: 0 }),
      node({ id: `stage-b-${workspaceId}`, parentId: rootId, title: '阶段 B', nodeType: 'stage', depth: 1, orderIndex: 1 }),
      node({ id: `task-a-${workspaceId}`, parentId: `stage-a-${workspaceId}`, title: '任务甲', nodeType: 'task', depth: 2, orderIndex: 0, status: 'completed' }),
      node({ id: `task-b-${workspaceId}`, parentId: `stage-b-${workspaceId}`, title: '任务乙', nodeType: 'task', depth: 2, orderIndex: 1 }),
    ],
    dependencies: [],
    relations: [],
    brief: { version: 1, goal: null, deadline: null, weeklyAvailableMinutes: null, currentLevel: null, successCriteria: null, constraints: [], missing: [] },
    sessions: [
      session(`s-a-${workspaceId}`, `task-a-${workspaceId}`, '任务甲'),
      session(`s-b-${workspaceId}`, `task-b-${workspaceId}`, '任务乙'),
    ],
    totalNodes: 2,
    completedNodes: 1,
  };
  await installPlanMock(page, workspaceId, rootId, payload);

  const consoleErrors: string[] = [];
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });

  await page.goto(`/workbench?workspace=${workspaceId}&view=tasks`);
  await expect(page.locator('.task-view')).toBeVisible({ timeout: 20000 });

  // 两个分组、两个任务:不重复、不丢失。
  await expect(page.locator('.task-group')).toHaveCount(2);
  await expect(page.locator('.task-row')).toHaveCount(2);
  await expect(page.getByText('任务甲')).toBeVisible();
  await expect(page.getByText('任务乙')).toBeVisible();

  // 两个分组的数据必须唯一:标题各不相同,任务 id 也不重复。
  const groupTitles = await page.locator('.task-group header h3').allTextContents();
  expect(new Set(groupTitles).size).toBe(2);
  expect(groupTitles.sort()).toEqual(['阶段 A', '阶段 B']);

  // 完成进度:任务甲已完成,且它所在分组的计数是 1 / 1。
  await expect(page.locator('.task-row.completed-row')).toHaveCount(1);
  await expect(page.locator('.task-group', { hasText: '任务甲' }).locator('header small')).toHaveText('1 / 1');

  // 切换范围:全部 / 本周 / 今天 —— 都不重不漏。
  for (const label of ['全部', '本周', '今天']) {
    await page.getByRole('button', { name: label, exact: true }).click();
    await expect(page.locator('.task-row')).toHaveCount(2);
    await expect(page.locator('.task-group')).toHaveCount(2);
  }

  // 全程没有 duplicate key 警告。
  const duplicateKeyErrors = consoleErrors.filter(text => /same key|duplicate key/i.test(text));
  expect(duplicateKeyErrors, duplicateKeyErrors.join('\n')).toHaveLength(0);
});
