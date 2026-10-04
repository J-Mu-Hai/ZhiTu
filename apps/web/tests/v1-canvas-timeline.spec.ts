/**
 * V1 画布与时间线投影修复:定向 E2E。
 *
 * 只验本轮修复的两件事:
 * 1. 根画布按**服务端 V1 可见性投影**显示三个已分析核心节点(不再用 sourceNodeId 猜);
 * 2. V1 粗时间架构走**同一条中央主轴**(不再是 V01TimelineAxis 小组件),草案可确认。
 *
 * 数据用 `page.route` 固定 `/reasoning` 与 `/questions`;后端只需要活着并提供一个
 * 真实空间(根节点)。
 */
import { expect, test, type Page } from '@playwright/test';
import { assertBackendRunning, createWorkspace, getPlan, registerAccount } from './support/session';

const NOW = new Date().toISOString();

function dimension(key: string, title: string, judgment: string, visible: boolean, focus = false) {
  return {
    key,
    title,
    judgment,
    status: judgment ? 'resolved' : 'pending',
    visible,
    isFocus: focus,
    hasPendingQuestion: false,
    discussionSummary: judgment ? '已讨论 1 次' : '',
    internal: !['true_intent', 'key_conflict', 'goal_definition'].includes(key),
    questionId: `q-${key}`,
    knownFacts: [`关于 ${title} 的已确认事实`],
    assumptions: [`关于 ${title} 的 AI 假设`],
    importanceReason: '它决定整条路线。',
    requiresResponse: false,
  };
}

function question(workspaceId: string, rootId: string, key: string, title: string, judgment: string) {
  return {
    id: `q-${key}`,
    workspaceId,
    sourceNodeId: rootId,
    sourceMessageId: null,
    reasoningNodeId: null,
    question: title,
    whyNow: '',
    analysisSummary: judgment,
    recommendation: '',
    decisionImpact: '',
    confidenceNote: null,
    responseMode: 'free_text',
    options: [],
    allowCustomInput: true,
    status: 'resolved',
    answer: null,
    v1Key: key,
    v1Analysis: {
      judgment,
      knownFacts: [`关于 ${title} 的已确认事实`],
      assumptions: [],
      evidence: [],
      status: 'resolved',
      discussionCount: 1,
    },
    v1Title: title,
    v1Visible: true,
    v1RequiresResponse: false,
    createdAt: NOW,
    updatedAt: NOW,
    answeredAt: null,
  };
}

function baseView(workspaceId: string, rootId: string, overrides: Record<string, unknown>) {
  return {
    workspaceId,
    sessionId: 's-v1',
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
    v1Stage: null,
    v1Status: 'idle',
    v1WorkflowNext: null,
    v1VisibleAnalysisKeys: [],
    v1HiddenAnalysisCount: 7,
    v1ActualPendingQuestionCount: 0,
    v1FocusKey: 'goal_definition',
    v1Dimensions: [],
    v1StrategicThesis: '先做出一个能展示的最小项目。',
    v1CandidateDirections: null,
    v1Strategy: { mainLine: '先跑通最小闭环', riskControl: '第 1 周末设检查点' },
    v1RequireOpenjiuwen: true,
    v01Timeline: [],
    v01TimelineProposalId: null,
    datesCalibrated: false,
    inputVersion: 'v1',
    strategyProposalId: null,
    exploredAt: null,
    lastEvaluatedAt: null,
    nodes: [],
    links: [],
    error: null,
    ...overrides,
  };
}

async function installMock(
  page: Page,
  view: Record<string, unknown>,
  questions: unknown[],
) {
  await page.route('**/api/workspaces/*/reasoning', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(view) });
  });
  await page.route('**/api/workspaces/*/questions*', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ questions, truncated: false }),
    });
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

test('根画布只显示服务端投影里的三个已分析核心节点', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-canvas-projection');
  const workspaceId = await createWorkspace(page, token, 'V1 画布投影', '30 天做出一个数据分析小工具');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;

  const dims = [
    dimension('true_intent', '真实意图', '你想要一个能展示的成果,而不是学完语法。', true),
    dimension('key_conflict', '核心矛盾', '目标太大、反馈太慢。', true),
    dimension('goal_definition', '目标定义', '30 天做出一个可展示的分析小工具。', true, true),
    dimension('major_risks', '主要风险', '容易只看教程不动手。', false),
    dimension('hard_constraints', '硬约束', '每天只有 1 小时。', false),
  ];
  const questions = [
    question(workspaceId, rootId, 'true_intent', '真实意图', '你想要一个能展示的成果,而不是学完语法。'),
    question(workspaceId, rootId, 'key_conflict', '核心矛盾', '目标太大、反馈太慢。'),
    question(workspaceId, rootId, 'goal_definition', '目标定义', '30 天做出一个可展示的分析小工具。'),
    question(workspaceId, rootId, 'major_risks', '主要风险', '容易只看教程不动手。'),
  ];
  await installMock(
    page,
    baseView(workspaceId, rootId, {
      v1Stage: 'strategy_draft',
      v1WorkflowNext: 'confirm_strategy',
      v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
      v1Dimensions: dims,
    }),
    questions,
  );

  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  const nodes = page.locator('.react-flow__node-question');
  // 三个核心(+ focus 已含在核心内);其余内部维度不默认上画布。
  await expect(nodes).toHaveCount(3, { timeout: 20000 });
  await expect(page.getByText('目标定义', { exact: false }).first()).toBeVisible();
  await expect(page.locator('.react-flow__node-question', { hasText: '主要风险' })).toHaveCount(0);
});

test('V1 粗时间架构走中央主轴,草案可确认,相对周刻度稀疏', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-timeline-axis');
  const workspaceId = await createWorkspace(page, token, 'V1 时间线主轴', '30 天做出一个数据分析小工具');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;

  const phase = (index: number, title: string) => ({
    id: `phase-${index}`,
    title,
    kind: 'phase',
    startWeek: index * 2 - 1,
    endWeek: index * 2,
    startDate: null,
    endDate: null,
    goal: `${title}的目标`,
    deliverable: `${title}的成果`,
    completionCriteria: `${title}的完成标准`,
    status: 'draft',
    planNodeId: null,
  });
  await installMock(
    page,
    baseView(workspaceId, rootId, {
      v1Stage: 'coarse_timeline_review',
      v1Status: 'awaiting_user_confirmation',
      v1WorkflowNext: 'confirm_timeline',
      v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
      v1Dimensions: [
        dimension('true_intent', '真实意图', '想要能展示的成果。', true),
        dimension('key_conflict', '核心矛盾', '目标太大。', true),
        dimension('goal_definition', '目标定义', '做出可展示的小工具。', true, true),
      ],
      v01Timeline: [phase(1, '阶段一:跑通最小闭环'), phase(2, '阶段二:做深分析'), phase(3, '阶段三:收尾展示')],
      v01TimelineProposalId: 'p-v1-timeline',
    }),
    [],
  );

  await page.goto(`/workbench?workspace=${workspaceId}&view=timeline`);
  await expect(page.getByTestId('timeline-view')).toBeVisible({ timeout: 20000 });

  // 不再是 V01TimelineAxis 小组件。
  await expect(page.getByTestId('v01-timeline')).toHaveCount(0);
  // 草案状态条 + 正确的操作文案。
  const bar = page.getByTestId('v1-timeline-draft-bar');
  await expect(bar).toBeVisible();
  await expect(bar).toContainText('等待你确认');
  await expect(bar.getByRole('button', { name: '确认时间架构' })).toBeVisible();
  await expect(bar.getByRole('button', { name: '调整时间架构' })).toBeVisible();
  // 阶段卡片全部画在主轴上(不互相遮挡由布局引擎负责)。
  await expect(page.locator('[data-timeline-card]')).toHaveCount(3);
  // 相对周刻度,且按缩放稀疏显示(不是 24 个标签挤一行)。
  const weekLabels = page.locator('[data-testid="timeline-canvas"] span', { hasText: /^第 \d+ 周$/ });
  expect(await weekLabels.count()).toBeGreaterThan(0);
  expect(await weekLabels.count()).toBeLessThan(8);
  // 旧的顶部预览卡片不出现。
  await expect(page.getByTestId('strategy-architecture-preview')).toHaveCount(0);
});
