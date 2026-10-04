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

test('专注思考:居中放大,关闭后回到右侧,内容不丢', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-focus-thinking');
  const workspaceId = await createWorkspace(page, token, '专注思考', '我想学 Python 用于自动化');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;

  await installMock(
    page,
    baseView(workspaceId, rootId, {
      v1Stage: 'strategy_draft',
      v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
      v1StrategyUnderstanding: {
        goal: '用 Python 自动化一件重复小事',
        keyConflict: '不确定值不值得投入',
        mainLine: '先用最小脚本跑通',
        deferOrAvoid: '暂不系统学语法',
        riskControl: '每两周复盘',
        confirmed: false,
      },
      v1Dimensions: [
        dimension('true_intent', '真实意图', '想要自动化省时间。', true),
        dimension('key_conflict', '核心矛盾', '怕学了用不上。', true),
        dimension('goal_definition', '目标定义', '做出一个自动化小工具。', true, true),
      ],
    }),
    [],
  );

  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  await expect(page.getByTestId('v1-strategy-understanding')).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('focus-thinking')).toHaveCount(0);

  await page.getByTestId('focus-thinking-open').first().click();
  const modal = page.getByTestId('focus-thinking');
  await expect(modal).toBeVisible();
  await expect(modal.getByTestId('v1-strategy-understanding')).toBeVisible();
  await page.getByTestId('focus-thinking-close').click();
  await expect(page.getByTestId('focus-thinking')).toHaveCount(0);
  // 关闭后回到右侧 Dock,战略理解仍在。
  await expect(page.getByTestId('v1-strategy-understanding').first()).toBeVisible();
});

test('时间架构共创:先对齐节奏,认可后才生成时间线', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-timeline-align');
  const workspaceId = await createWorkspace(page, token, '时间共创', '我想学 Python 用于自动化');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;

  const alignmentView = baseView(workspaceId, rootId, {
    v1Stage: 'timeline_alignment',
    v1WorkflowNext: 'confirm_timeline_alignment',
    v1TimelineAlignment: {
      summary: '按每周一个可验收小闭环推进。',
      totalSpan: '约 6 周',
      cadence: '每周 1 个可验收小闭环',
      phaseCount: 3,
      biggestRisk: '投入不稳定',
      assumptions: [
        { text: '用户想尽快出成果', source: 'user_fact' },
        { text: '我暂定每周 6 小时', source: 'ai_assumption' },
      ],
      question: '更希望更快见成果,还是更稳打基础?',
      options: ['先快后稳', '先稳后快'],
      answer: '',
      confirmed: false,
    },
    v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
    v1Dimensions: [dimension('goal_definition', '目标定义', '做出一个自动化小工具。', true, true)],
  });
  await installMock(page, alignmentView, []);

  // 对齐接口返回“已生成粗时间线”的视图。
  const generatedView = baseView(workspaceId, rootId, {
    v1Stage: 'coarse_timeline_review',
    v1Status: 'awaiting_user_confirmation',
    v1WorkflowNext: 'confirm_timeline',
    v01TimelineProposalId: 'p-aligned',
    v01Timeline: [
      { id: 'phase-1', index: 1, title: '定位', kind: 'phase', category: '定位', startWeek: 1, endWeek: 1, startDate: null, endDate: null, goal: '定题', deliverable: '一句问题', completionCriteria: '能说清', status: 'draft', planNodeId: null },
      { id: 'phase-2', index: 2, title: '闭环', kind: 'phase', category: '基础闭环', startWeek: 2, endWeek: 3, startDate: null, endDate: null, goal: '跑通', deliverable: '一张图', completionCriteria: '能复现', status: 'draft', planNodeId: null },
      { id: 'phase-3', index: 3, title: '产出', kind: 'phase', category: '产出', startWeek: 4, endWeek: 4, startDate: null, endDate: null, goal: '展示', deliverable: '一页报告', completionCriteria: '能讲清', status: 'draft', planNodeId: null },
    ],
  });
  await page.route('**/agent/v1/timeline/align*', async route => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ reasoning: generatedView }),
    });
  });

  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  const card = page.getByTestId('v1-timeline-alignment');
  await expect(card).toBeVisible({ timeout: 20000 });
  await expect(card.getByTestId('v1-timeline-assumptions')).toContainText('你说过');
  await expect(card.getByTestId('v1-timeline-assumptions')).toContainText('AI 暂定');
  await expect(card).toContainText('更希望更快见成果');

  await card.getByRole('button', { name: '认可默认节奏' }).click();
  // 对齐后共创卡片消失(provider 已换成粗时间线视图)。
  await expect(page.getByTestId('v1-timeline-alignment')).toHaveCount(0, { timeout: 20000 });
  // 客户端切到时间线(不重载,保留新 reasoning):应出现 3 个阶段。
  await page.getByRole('tab', { name: '时间线' }).click();
  await expect(page.locator('[data-timeline-card]')).toHaveCount(3, { timeout: 20000 });
  await expect(page.getByText(/阶段 1/).first()).toBeVisible();
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
  const view = page.getByTestId('timeline-view');
  await expect(view).toBeVisible({ timeout: 20000 });

  // 不再是 V01TimelineAxis 小组件。
  await expect(page.getByTestId('v01-timeline')).toHaveCount(0);
  // 草案状态条:预测时间轴 + 起点可调整 + 正确的操作文案(压缩在顶部)。
  const bar = page.getByTestId('v1-timeline-draft-bar');
  await expect(bar).toBeVisible();
  await expect(bar).toContainText('预测时间轴');
  await expect(bar).toContainText('等待你确认');
  await expect(bar.getByRole('button', { name: '确认时间架构' })).toBeVisible();
  await expect(bar.getByRole('button', { name: '调整时间架构' })).toBeVisible();
  await expect(bar.getByLabel('预测起点日期')).toBeVisible();
  // 五档快捷尺度。
  const presets = page.getByTestId('timeline-presets');
  await expect(presets).toBeVisible();
  await expect(presets.getByRole('button')).toHaveCount(5);
  // 阶段卡片全部画在主轴上,且不出现“另有 N 项”。
  await expect(page.locator('[data-timeline-card]')).toHaveCount(3);
  await expect(page.getByText(/另有 \d+ 项/)).toHaveCount(0);
  // 刻度随缩放稀疏显示(不把所有周标签塞一行)。
  const ticks = page.getByTestId('timeline-tick');
  expect(await ticks.count()).toBeGreaterThan(0);
  expect(await ticks.count()).toBeLessThan(120);
  // 切到“天”尺度后,刻度变成日期 + 星期。
  await presets.getByRole('button', { name: '天' }).click();
  await expect(view).toHaveAttribute('data-zoom', 'day');
  // 底部不再有常驻阶段详情;点卡片才出现轻量浮层。
  await expect(page.getByTestId('v1-timeline-detail')).toHaveCount(0);
  await expect(page.getByTestId('v1-phase-detail')).toHaveCount(0);
  await page.locator('[data-timeline-card]').first().click();
  const popover = page.getByTestId('v1-phase-detail');
  await expect(popover).toBeVisible();
  await expect(popover).toContainText('目标：');
  await expect(popover).toContainText('完成标准：');
  // 旧的顶部预览卡片不出现。
  await expect(page.getByTestId('strategy-architecture-preview')).toHaveCount(0);
});
