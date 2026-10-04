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

test('当前 interaction 只在画布节点里,对话区只有位置提示', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-current-interaction');
  const workspaceId = await createWorkspace(page, token, '当前行动固定', '我想学 Python 用于自动化');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;

  await installMock(
    page,
    baseView(workspaceId, rootId, {
      v1Stage: 'goal_reframe',
      v1CurrentInteraction: {
        id: 'ci-strategic_question-abc',
        nonce: 'turn-1',
        kind: 'strategic_question',
        priority: 'high',
        title: '需要你回答一个关键问题',
        context: '你想要自动化省时间,但还没说清具体是哪件事。',
        whyNow: '它决定第一周先做什么。',
        prompt: '你想自动化的具体是哪一件重复工作?',
        options: [],
        recommendedOption: null,
        focusKey: 'true_intent',
        status: 'active',
        presentation: 'focus_modal',
      },
      v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
      v1Dimensions: [dimension('true_intent', '真实意图', '想省时间。', true, true)],
    }),
    [question(workspaceId, rootId, 'true_intent', '真实意图', '想省时间。')],
  );

  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  // ---- 真实 ReactFlow 边:根 flow 节点直接分出三阶段,不是串成一条链 ----
  await expect(page.locator('.v1-phase-node')).toHaveCount(3, { timeout: 20000 });
  for (const key of ['think', 'plan', 'do'] as const) {
    const edge = page.locator(`.react-flow__edge[data-id="v1phase-root:${key}"]`);
    await expect(edge, `缺少根→${key}的边`).toHaveCount(1);
    // source 必须是**真实根 flow 节点**(rootId),不是猜的 spaceId。
    await expect(edge).toHaveAttribute('aria-label', `Edge from ${rootId} to v1phase:${key}`);
  }
  // 不允许再出现 think → plan → do 的视觉链条。
  await expect(page.locator('.react-flow__edge[data-id^="v1phase-chain:"]')).toHaveCount(0);

  // 对话区只有一句“决定放在哪个节点”的位置提示:没有固定交互卡、没有排队输入。
  const notice = page.getByTestId('chat-action-notice');
  await expect(notice).toBeVisible({ timeout: 20000 });
  await expect(notice).toContainText('真实意图');
  await expect(notice.getByRole('button', { name: '定位到节点' })).toBeVisible();
  await expect(page.locator('[data-testid="current-interaction-card"]')).toHaveCount(0);
  await expect(page.getByTestId('focus-thinking')).toHaveCount(0);
  await expect(page.locator('.floating-conversation .cq-direction')).toHaveCount(0);

  // 结构化回答只在画布节点里:点开 active 节点才有输入。
  const activeNode = page.locator('.canvas-question-node.is-active');
  await expect(activeNode).toHaveCount(1);
  await activeNode.click();
  await expect(activeNode.getByLabel('补充你的回答')).toBeVisible();
});

test('时间架构共创:先对齐节奏,认可后才生成时间线', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-timeline-align');
  const workspaceId = await createWorkspace(page, token, '时间共创', '我想学 Python 用于自动化');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;

  const alignmentView = baseView(workspaceId, rootId, {
    v1Stage: 'timeline_alignment',
    v1WorkflowNext: 'confirm_timeline_alignment',
    v1CurrentInteraction: {
      id: 'ci-timeline-alignment',
      nonce: 'turn-1',
      kind: 'timeline_alignment',
      priority: 'high',
      title: '对齐时间节奏',
      context: '按每周一个可验收小闭环推进。',
      whyNow: '先对齐节奏,才生成粗时间架构。',
      prompt: '更希望更快见成果,还是更稳打基础?',
      options: [
        { key: '0', title: '先快后稳' },
        { key: '1', title: '先稳后快' },
      ],
      recommendedOption: null,
      focusKey: null,
      status: 'active',
      presentation: 'focus_modal',
    },
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
  // 对话区不再承载时间节奏共创卡片 —— 它只留聊天与定位。
  await expect(page.getByTestId('v1-timeline-alignment')).toHaveCount(0);
  await expect(page.getByTestId('chat-action-notice')).toContainText('排出来');

  // 结构化动作只在「排出来」下面的子问题节点里，阶段卡本身只做导航。
  const activeNode = page.locator('.v1-interaction-node[data-phase="plan"]');
  await expect(activeNode).toHaveCount(1);
  await activeNode.click();
  await expect(activeNode).toContainText('更希望更快见成果');
  await activeNode.getByRole('button', { name: '认可默认节奏' }).click();
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
    dimension('major_risks', '主要风险', '容易只看教程不动手。', true),
    dimension('hard_constraints', '硬约束', '每天只有 1 小时。', true),
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
  // 画布**只保留三个基石节点**;其余维度(即使服务端投影 visible)不再单独成卡。
  await expect(nodes).toHaveCount(3, { timeout: 20000 });
  await expect(page.getByText('目标定义', { exact: false }).first()).toBeVisible();
  await expect(page.locator('.react-flow__node-question', { hasText: '主要风险' })).toHaveCount(0);
  await expect(page.locator('.react-flow__node-question', { hasText: '硬约束' })).toHaveCount(0);

  // 它们被“链接”到关键矛盾节点上:紧凑时显示数量,点开后以关联讨论列出。
  const conflictNode = page.locator('.react-flow__node-question', { hasText: '核心矛盾' });
  await expect(conflictNode.getByTestId('cq-linked-count')).toContainText('2');
  await conflictNode.click();
  const linked = conflictNode.getByTestId('cq-linked');
  await expect(linked).toBeVisible();
  await expect(linked).toContainText('主要风险');
  await expect(linked).toContainText('硬约束');
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
