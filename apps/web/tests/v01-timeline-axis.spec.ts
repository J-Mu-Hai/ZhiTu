/**
 * 规划智能体 V0.1:日期主轴的**最小定向验证**。
 *
 * 只验一件事:有 deadline 时,阶段被画进**主轴本身**,而不是时间线上方的
 * `StrategyArchitecturePreview` 预览卡片。数据来自 `/reasoning` 的
 * `v01Timeline`,前端不解析自然语言。
 *
 * 用 `page.route` 固定 `v01Timeline`(隔离栈没有模型 key,造不出真实时间线)。
 */
import { expect, test, type Page } from '@playwright/test';
import { assertBackendRunning, createWorkspace, getPlan, registerAccount } from './support/session';

const NOW = new Date().toISOString();

function timelineItem(
  id: string, title: string, startDate: string, endDate: string, status: string,
) {
  return {
    id,
    title,
    kind: 'phase',
    startWeek: null,
    endWeek: null,
    startDate,
    endDate,
    goal: `${title}的目标`,
    deliverable: `${title}的成果`,
    completionCriteria: `${title}的完成标准`,
    status,
    planNodeId: null,
  };
}

async function installDatedTimelineMock(page: Page, workspaceId: string, rootId: string, items: unknown[]) {
  const view = {
    workspaceId,
    sessionId: 's-v01',
    rootPlanNodeId: rootId,
    phase: 'temporal_architecture_draft',
    turnAction: 'confirm',
    status: 'ready',
    mapVersion: 3,
    focusHandle: 'r2',
    focusReasoningNodeId: null,
    focusReason: null,
    datesCalibrated: true,
    inputVersion: 'v1',
    strategyProposalId: null,
    intakeQuestionsAsked: 0,
    intakeQuestionLimit: 5,
    pendingIntake: null,
    workflowStage: 'timeline_review',
    discoveryQuestions: [],
    v01TimelineProposalId: 'p-v01',
    v01Timeline: items,
    nodes: [],
    links: [],
    error: null,
  };
  await page.route('**/api/workspaces/*/reasoning', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(view) });
  });
  await page.route('**/api/workspaces/*/agent/turn', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reasoning: view,
        message: null,
        question: null,
        replayed: true,
        degraded: false,
        degradedReason: null,
        retryable: false,
        changed: false,
        proposalErrors: [],
      }),
    });
  });
}

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('有 deadline 时阶段渲染在主轴内,不在顶部预览卡片里', async ({ page }) => {
  const { token } = await registerAccount(page, 'v01-date-axis');
  const workspaceId = await createWorkspace(page, token, 'V0.1 日期轴空间', '30 天学习 Python');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;
  await installDatedTimelineMock(page, workspaceId, rootId, [
    timelineItem('phase-1', '阶段 1:打基础', '2026-10-01', '2026-10-14', 'draft'),
    timelineItem('phase-2', '阶段 2:做实战', '2026-10-15', '2026-11-04', 'draft'),
  ]);

  await page.goto(`/workbench?workspace=${workspaceId}&view=timeline`);

  const axis = page.getByTestId('v01-timeline');
  await expect(axis).toBeVisible({ timeout: 20000 });
  // 日期轴而非相对周轴。
  await expect(axis).toHaveAttribute('data-mode', 'dated');

  const bars = page.getByTestId('v01-phase-bar');
  await expect(bars).toHaveCount(2);
  for (const bar of await bars.all()) {
    expect(await bar.getAttribute('data-start')).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(await bar.getAttribute('data-end')).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    const box = await bar.boundingBox();
    expect(box, '区间条没有边界框').not.toBeNull();
    expect(box!.width, '区间条宽度必须大于 0').toBeGreaterThan(0);
  }

  // 每个阶段终点有一个里程碑点。
  await expect(page.getByTestId('v01-milestone')).toHaveCount(2);
  // 旧的顶部预览卡片不应出现。
  await expect(page.getByTestId('strategy-architecture-preview')).toHaveCount(0);
});
