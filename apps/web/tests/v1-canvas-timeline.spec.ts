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

test('三阶段边:根 flow 节点直接分出 think / plan / do', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-phase-edges');
  const workspaceId = await createWorkspace(page, token, '三阶段边', '我想学 Python 用于自动化');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;
  await installMock(
    page,
    baseView(workspaceId, rootId, {
      v1Stage: 'strategy_draft',
      v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
      v1Dimensions: [
        dimension('true_intent', '真实意图', '想要能展示的成果。', true, true),
        dimension('key_conflict', '关键矛盾', '目标太大、反馈太慢。', true),
        dimension('goal_definition', '目标定义', '做出可展示的小工具。', true),
      ],
    }),
    [],
  );

  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  await expect(page.locator('.v1-phase-node')).toHaveCount(3, { timeout: 20000 });
  // 三条边必须**实际存在**于 ReactFlow edges 中,source 是真实根 flow 节点。
  for (const key of ['think', 'plan', 'do'] as const) {
    const edge = page.locator(`.react-flow__edge[data-id="v1phase-root:${key}"]`);
    await expect(edge, `缺少根→${key}的边`).toHaveCount(1);
    await expect(edge).toHaveAttribute('aria-label', `Edge from ${rootId} to v1phase:${key}`);
    // 可见的实线:stroke 不是 transparent / 0。
    const stroke = await edge.locator('.react-flow__edge-path').evaluate(el => getComputedStyle(el).stroke);
    expect(stroke).not.toBe('none');
    expect(stroke).not.toBe('rgba(0, 0, 0, 0)');
  }
  // 不允许再出现 think → plan → do 的视觉链条。
  await expect(page.locator('.react-flow__edge[data-id^="v1phase-chain:"]')).toHaveCount(0);
});

test('conversation 型 interaction 只在右侧对话区,画布不建同一份控件', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-conversation-channel');
  const workspaceId = await createWorkspace(page, token, '对话渠道', '我想学 Python 用于自动化');
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
        prompt: '你真正担心的是什么?',
        options: [],
        recommendedOption: null,
        focusKey: 'true_intent',
        status: 'active',
        presentation: 'focus_modal',
        answerChannel: 'conversation',
      },
      v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
      v1Dimensions: [dimension('true_intent', '真实意图', '想省时间。', true, true)],
    }),
    [question(workspaceId, rootId, 'true_intent', '真实意图', '想省时间。')],
  );

  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  const chatQuestion = page.getByTestId('chat-conversation-question');
  await expect(chatQuestion).toBeVisible({ timeout: 20000 });
  await expect(chatQuestion).toContainText('需要在对话中回答');
  await expect(chatQuestion).toContainText('你真正担心的是什么?');
  // 对话区不给“定位到节点”,也没有结构化控件。
  await expect(page.getByTestId('chat-action-notice')).toHaveCount(0);
  await expect(page.locator('.floating-conversation .cq-direction')).toHaveCount(0);
  // 画布上同一个问题**不**建 active 节点/控件。
  await expect(page.locator('.canvas-question-node.is-active')).toHaveCount(0);
  await expect(page.locator('[data-testid="v1interaction"]')).toHaveCount(0);
  // 用户可以直接在输入框自由作答。
  await page.getByLabel('给 AI 的消息').fill('我最担心的是坚持不下来');
  await expect(page.getByLabel('发送消息')).toBeEnabled();
});

test('canvas_node 型 interaction 只在画布节点,对话区只给定位', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-canvas-channel');
  const workspaceId = await createWorkspace(page, token, '画布渠道', '我想学 Python 用于自动化');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;

  await installMock(
    page,
    baseView(workspaceId, rootId, {
      v1Stage: 'goal_reframe',
      v1CurrentInteraction: {
        id: 'ci-candidate-1',
        nonce: 'turn-1',
        kind: 'candidate_selection',
        priority: 'high',
        title: '请选择一个起点',
        context: '你想要自动化省时间。',
        whyNow: '它决定第一周先做什么。',
        prompt: '选一个候选方向。',
        options: [
          { key: 'tool', title: '做一个小工具', reason: '最快看到成果', impact: '先窄后宽' },
          { key: 'system', title: '学一套体系', reason: '基础更牢', impact: '见效慢' },
        ],
        recommendedOption: 'tool',
        focusKey: 'true_intent',
        status: 'active',
        presentation: 'focus_modal',
        answerChannel: 'canvas_node',
      },
      v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
      v1Dimensions: [dimension('true_intent', '真实意图', '想省时间。', true, true)],
    }),
    [question(workspaceId, rootId, 'true_intent', '真实意图', '想省时间。')],
  );

  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  // 对话区:只有位置提示 + 定位,没有“在对话中回答”,没有选项。
  const notice = page.getByTestId('chat-action-notice');
  await expect(notice).toBeVisible({ timeout: 20000 });
  await expect(notice).toContainText('真实意图');
  await expect(notice.getByRole('button', { name: '定位到节点' })).toBeVisible();
  await expect(page.getByTestId('chat-conversation-question')).toHaveCount(0);
  await expect(page.locator('.floating-conversation .cq-direction')).toHaveCount(0);
  // 画布:唯一 active 节点,点开后才有候选方向。
  const activeNode = page.locator('.canvas-question-node.is-active');
  await expect(activeNode).toHaveCount(1);
  await activeNode.click();
  await expect(activeNode.locator('.cq-direction')).toHaveCount(2);
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
  // 客户端切到时间线(不重载,保留新 reasoning):应出现 3 个阶段覆盖条。
  await page.getByRole('tab', { name: '时间线' }).click();
  await expect(page.getByTestId('v1-phase-bar')).toHaveCount(3, { timeout: 20000 });
  await expect(page.locator('[data-testid="v1-phase-card"]').first()).toContainText('定位');
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
  // 阶段只保留**一条主表现**:有高度的覆盖条,且不出现“另有 N 项”。
  await expect(page.getByTestId('v1-phase-bar')).toHaveCount(3);
  await expect(page.locator('[data-timeline-card]')).toHaveCount(0);
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
  await page.getByTestId('v1-phase-bar').first().click();
  const popover = page.getByTestId('v1-phase-detail');
  await expect(popover).toBeVisible();
  await expect(popover).toContainText('目标：');
  await expect(popover).toContainText('完成标准：');
  // 旧的顶部预览卡片不出现。
  await expect(page.getByTestId('strategy-architecture-preview')).toHaveCount(0);
});

test('V1 根画布不投影旧版固定分组容器(目标重构 / 问题结构 / 战略路径)', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-hide-groups');
  const workspaceId = await createWorkspace(page, token, '隐藏旧分组', '我想学 Python 用于自动化');
  const plan = await getPlan(page, token, workspaceId);
  const rootId = plan.nodes.find(node => node.parentId === null)!.id;

  // 用**真实** plan 再叠加旧版三个分组容器(nodeType=capability / purpose=information / v1Key=…)。
  const group = (key: string, title: string, orderIndex: number) => ({
    id: `group-${key}`,
    parentId: rootId,
    title,
    description: '',
    acceptanceCriteria: null,
    nodeType: 'capability',
    purpose: 'information',
    planningLevel: null,
    status: 'pending',
    priority: 'medium',
    estimateMinutes: null,
    deadline: null,
    depth: 1,
    orderIndex,
    origin: 'ai',
    completedAt: null,
    createdAt: new Date().toISOString(),
    contentVersion: 1,
    v1Key: key,
    v1Analysis: null,
  });
  const planWithGroups = {
    ...plan,
    nodes: [
      ...plan.nodes,
      group('goal_reframe', '目标重构', 0),
      group('problem_structure', '问题结构', 1),
      group('strategy_path', '战略路径', 2),
    ],
  };
  await page.route('**/api/workspaces/*/plan', route =>
    route.request().method() === 'GET'
      ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(planWithGroups) })
      : route.continue(),
  );
  await installMock(
    page,
    baseView(workspaceId, rootId, {
      v1Stage: 'strategy_draft',
      v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
      v1Dimensions: [
        dimension('true_intent', '真实意图', '想要能展示的成果。', true, true),
        dimension('key_conflict', '关键矛盾', '目标太大、反馈太慢。', true),
        dimension('goal_definition', '目标定义', '做出可展示的小工具。', true),
      ],
    }),
    [
      question(workspaceId, rootId, 'true_intent', '真实意图', '想要能展示的成果。'),
      question(workspaceId, rootId, 'key_conflict', '关键矛盾', '目标太大、反馈太慢。'),
      question(workspaceId, rootId, 'goal_definition', '目标定义', '做出可展示的小工具。'),
    ],
  );

  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  // 三阶段骨架与三个基石问题仍保留。
  await expect(page.locator('.v1-phase-node')).toHaveCount(3, { timeout: 20000 });
  await expect(page.locator('.react-flow__node-question')).toHaveCount(3);

  // 旧分组容器不在画布里(节点、缩略图同源,都用这份投影)。
  await expect(page.locator('.react-flow__node[data-id^="group-"]')).toHaveCount(0);
  const growthTitles = await page
    .locator('.react-flow__node-growth .node-title')
    .evaluateAll(els => els.map(el => (el.textContent ?? '').trim()));
  expect(growthTitles).not.toContain('目标重构');
  expect(growthTitles).not.toContain('问题结构');
  expect(growthTitles).not.toContain('战略路径');

  // 没有悬空线:不存在指向/来自被隐藏分组的边。
  const edgeIds = await page.locator('.react-flow__edge').evaluateAll(
    els => els.map(el => el.getAttribute('data-id') ?? ''),
  );
  expect(edgeIds.some(id => id.includes('group-'))).toBe(false);
});

test('时间线阶段:细覆盖条 + 独立方框卡,点击开详情,点空白 / Esc 关闭', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-timeline-bars');
  const workspaceId = await createWorkspace(page, token, 'V1 阶段条', '30 天做出一个数据分析小工具');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;

  // 三个阶段时长刻意不同:覆盖 1 周 / 4 周 / 2 周。宽度必须跟时长走。
  const phase = (index: number, title: string, startWeek: number, endWeek: number) => ({
    id: `phase-${index}`, title, kind: 'phase', startWeek, endWeek,
    startDate: null, endDate: null, goal: `${title}的目标`, deliverable: `${title}的成果`,
    completionCriteria: `${title}的完成标准`, status: 'draft', planNodeId: null,
  });
  await installMock(
    page,
    baseView(workspaceId, rootId, {
      v1Stage: 'coarse_timeline_review',
      v1Status: 'awaiting_user_confirmation',
      v01Timeline: [phase(1, '阶段一:起步', 1, 2), phase(2, '阶段二:建设', 3, 6), phase(3, '阶段三:收尾', 7, 8), phase(4, '阶段四:复盘', 9, 10)],
      v01TimelineProposalId: 'p-bars',
    }),
    [],
  );

  await page.goto(`/workbench?workspace=${workspaceId}&view=timeline`);
  const view = page.getByTestId('timeline-view');
  await expect(view).toBeVisible({ timeout: 20000 });

  // 一条阶段 = 覆盖条 + 方框卡(各 3 份,不重复)。
  const bars = page.getByTestId('v1-phase-bar');
  const cards = page.getByTestId('v1-phase-card');
  await expect(bars).toHaveCount(4);
  await expect(cards).toHaveCount(4);
  // 引线把卡片接到自己的覆盖条。
  expect(await page.locator('[class*="phaseConnector"]').count()).toBeGreaterThanOrEqual(4);
  // 不存在“第三份重复大卡”。
  await expect(page.locator('[data-timeline-card]')).toHaveCount(0);

  // 覆盖条是干净的横向色带:有宽度、高度只有 10–14px 左右。
  const boxes = await bars.evaluateAll(els => els.map(el => {
    const rect = el.getBoundingClientRect();
    return { id: el.getAttribute('data-phase-id'), width: rect.width, height: rect.height };
  }));
  for (const box of boxes) {
    expect(box.width, `${box.id} 太窄`).toBeGreaterThan(26);
    expect(box.height, `${box.id} 不是干净的色带`).toBeGreaterThanOrEqual(10);
    expect(box.height, `${box.id} 又变成文字卡了`).toBeLessThanOrEqual(18);
  }
  // 时长 4 周的那条比 1 周 / 2 周的宽。
  const widthOf = (id: string) => boxes.find(b => b.id === id)!.width;
  expect(widthOf('phase-2')).toBeGreaterThan(widthOf('phase-1'));
  expect(widthOf('phase-2')).toBeGreaterThan(widthOf('phase-3'));

  // 方框卡:标题完整可读(>=13px,不被裁掉),时间范围与摘要各一行。
  const card2 = page.locator('[data-testid="v1-phase-card"][data-phase-id="phase-2"]');
  await expect(card2.locator('strong')).toHaveText('阶段二:建设');
  const titleSize = await card2.locator('strong').evaluate(el => parseFloat(getComputedStyle(el).fontSize));
  expect(titleSize).toBeGreaterThanOrEqual(13);
  await expect(card2).toContainText('的成果');
  const cardBox = (await card2.boundingBox())!;
  expect(cardBox.width).toBeGreaterThanOrEqual(190);

  // 点击方框卡 → 详情出现。
  await card2.click();
  await expect(page.getByTestId('v1-phase-detail')).toBeVisible();
  await expect(page.getByTestId('v1-phase-detail')).toContainText('完成标准');
  // 点击时间轴空白 → 详情关闭。
  const canvasBox = (await page.getByTestId('timeline-canvas').boundingBox())!;
  await page.mouse.click(canvasBox.x + 14, canvasBox.y + 14);
  await expect(page.getByTestId('v1-phase-detail')).toHaveCount(0);
  // 点击覆盖条同样打开 → Escape 关闭。
  await bars.first().click();
  await expect(page.getByTestId('v1-phase-detail')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('v1-phase-detail')).toHaveCount(0);

  // 缩放到月 / 周 / 日,条与卡仍在。
  const presets = page.getByTestId('timeline-presets');
  for (const label of ['月', '周', '天']) {
    await presets.getByRole('button', { name: label }).click();
    expect(await page.getByTestId('v1-phase-bar').count()).toBeGreaterThan(0);
    expect(await page.getByTestId('v1-phase-card').count()).toBeGreaterThan(0);
  }
});

// ---- 时间线缩放层级:滚轮 / 周 / 日 ----

const TODAY_ISO = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());

function planNode(over: Record<string, unknown>) {
  return {
    description: null, acceptanceCriteria: null, purpose: 'planning', planningLevel: null,
    status: 'pending', priority: 'medium', estimateMinutes: null, deadline: null,
    depth: 0, orderIndex: 0, origin: 'user', completedAt: null, createdAt: new Date().toISOString(),
    contentVersion: 1, v1Key: null, v1Analysis: null, ...over,
  };
}

async function installPlan(page: Page, workspaceId: string, rootId: string, nodes: unknown[], sessions: unknown[] = []) {
  await page.route('**/api/workspaces/*/plan', route =>
    route.request().method() === 'GET'
      ? route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          workspaceId, revisionVersion: 1, nodes, dependencies: [], relations: [],
          brief: { version: 1, goal: null, deadline: null, weeklyAvailableMinutes: null, currentLevel: null, successCriteria: null, constraints: [], missing: [] },
          sessions, totalNodes: nodes.length, completedNodes: 0,
        }),
      })
      : route.continue(),
  );
}

const PHASE_TITLE = '阶段一 · 打基础';

function phaseView() {
  return {
    id: 'phase-1', title: PHASE_TITLE, kind: 'phase', startWeek: 1, endWeek: 2,
    startDate: null, endDate: null, goal: '把基础打牢', deliverable: '一份笔记',
    completionCriteria: '能复述', status: 'planned', planNodeId: null,
  };
}

test('时间线:滚轮直接缩放(无需 Ctrl),拖动空白平移', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-wheel-zoom');
  const workspaceId = await createWorkspace(page, token, '滚轮缩放', '30 天做出一个数据分析小工具');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;
  await installMock(page, baseView(workspaceId, rootId, {
    v1Stage: 'coarse_timeline_review',
    v1Status: 'awaiting_user_confirmation',
    v01Timeline: [phaseView()],
    v01TimelineProposalId: 'p-wheel',
  }), []);

  await page.goto(`/workbench?workspace=${workspaceId}&view=timeline`);
  const canvas = page.getByTestId('timeline-canvas');
  await expect(canvas).toBeVisible({ timeout: 20000 });
  const box = await canvas.evaluate(el => { const r = (el as HTMLElement).getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; });
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  const densityBefore = Number(await canvas.getAttribute('data-density'));
  // **普通垂直滚轮**就缩放,不带 Ctrl/Command。
  await page.mouse.wheel(0, -240);
  await expect.poll(async () => Number(await canvas.getAttribute('data-density'))).toBeGreaterThan(densityBefore);
  // 缩放后阶段仍在视野附近(条还存在)。
  await expect(page.getByTestId('v1-phase-bar')).toHaveCount(1);

  // Shift + 滚轮 = 平移:start 改变。
  // 平移:Shift + 滚轮 → start 改变,缩放级别不变(不会继续放大)。
  const startBefore = Number(await canvas.getAttribute('data-start'));
  const densityAfterZoom = Number(await canvas.getAttribute('data-density'));
  await canvas.evaluate(el => {
    el.dispatchEvent(new WheelEvent('wheel', { deltaX: 0, deltaY: 220, shiftKey: true, bubbles: true, cancelable: true }));
  });
  await expect.poll(async () => Number(await canvas.getAttribute('data-start'))).not.toBe(startBefore);
  expect(Number(await canvas.getAttribute('data-density'))).toBe(densityAfterZoom);
});

test('时间线:周尺度显示 某月·第N周 与已确认周计划', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-week-level');
  const workspaceId = await createWorkspace(page, token, '周尺度', '30 天做出一个数据分析小工具');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;
  const phaseId = `phase-${workspaceId}`;
  const weekId = `week-${workspaceId}`;
  await installPlan(page, workspaceId, rootId, [
    planNode({ id: rootId, parentId: null, title: '根目标', nodeType: 'goal', depth: 0 }),
    planNode({ id: phaseId, parentId: rootId, title: PHASE_TITLE, nodeType: 'stage', depth: 1 }),
    planNode({ id: weekId, parentId: phaseId, title: '本周计划:阶段一 · 第 1 版', nodeType: 'stage', depth: 2 }),
  ]);
  await installMock(page, baseView(workspaceId, rootId, {
    v1Stage: 'coarse_timeline_review',
    v01Timeline: [phaseView()],
    v01TimelineProposalId: 'p-week',
  }), []);

  await page.goto(`/workbench?workspace=${workspaceId}&view=timeline`);
  // 先等阶段投影出来(plan + reasoning 都到齐),再切到周尺度。
  await expect(page.getByTestId('v1-phase-bar')).toHaveCount(1, { timeout: 20000 });
  await page.getByTestId('timeline-presets').getByRole('button', { name: '周' }).click();
  await expect(page.getByTestId('timeline-view')).toHaveAttribute('data-zoom', 'week', { timeout: 20000 });
  // 已确认周计划:周条出现在所属阶段条下方。
  const weekBar = page.getByTestId('v1-week-bar');
  await expect(weekBar).toHaveCount(1);
  await expect(weekBar).toContainText('阶段一');
  // 刻度是“某月 · 第 N 周”,不是 10/6。
  const labels = await page.getByTestId('timeline-tick').allTextContents();
  expect(labels.some(label => /月 · 第\d+周/.test(label))).toBe(true);
});

test('时间线:日尺度显示已确认日工作块;没有日计划时不造假', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-day-level');
  const workspaceId = await createWorkspace(page, token, '日尺度', '30 天做出一个数据分析小工具');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;
  const phaseId = `phase-${workspaceId}`;
  const weekId = `week-${workspaceId}`;
  const taskId = `task-${workspaceId}`;
  await installPlan(page, workspaceId, rootId, [
    planNode({ id: rootId, parentId: null, title: '根目标', nodeType: 'goal', depth: 0 }),
    planNode({ id: phaseId, parentId: rootId, title: PHASE_TITLE, nodeType: 'stage', depth: 1 }),
    planNode({ id: weekId, parentId: phaseId, title: '本周计划:阶段一 · 第 1 版', nodeType: 'stage', depth: 2 }),
    planNode({ id: taskId, parentId: weekId, title: '写第一版脚本', nodeType: 'task', depth: 3 }),
  ], [{
    id: `s-${workspaceId}`, nodeId: taskId, workspaceId, nodeTitle: '写第一版脚本',
    scheduledDate: TODAY_ISO, plannedMinutes: 30, bufferMinutes: 0, actualMinutes: null, seq: 0,
    status: 'planned', locked: false, lockReason: null, origin: 'scheduler', startMinute: null, endMinute: null, completedAt: null,
  }]);
  await installMock(page, baseView(workspaceId, rootId, {
    v1Stage: 'coarse_timeline_review',
    v01Timeline: [phaseView()],
    v01TimelineProposalId: 'p-day',
  }), []);

  await page.goto(`/workbench?workspace=${workspaceId}&view=timeline`);
  // 先等阶段投影出来(plan + reasoning 都到齐),再切到日尺度。
  await expect(page.getByTestId('v1-phase-bar')).toHaveCount(1, { timeout: 20000 });
  await page.getByTestId('timeline-presets').getByRole('button', { name: '天' }).click();
  await expect(page.getByTestId('timeline-view')).toHaveAttribute('data-zoom', 'day', { timeout: 20000 });
  const dayBlock = page.getByTestId('v1-day-block');
  await expect(dayBlock).toHaveCount(1);
  await expect(dayBlock).toContainText('写第一版脚本');
  // 今天线仍在:把视口带回到今天再断言(日尺度下阶段中心可能不在今天附近)。
  await page.getByTestId('timeline-canvas').focus();
  await page.keyboard.press('Home');
  await expect(page.getByTestId('today-marker')).toBeVisible();
});
