import { expect, test, type Page } from '@playwright/test';
import { assertBackendRunning, createWorkspace, getPlan, registerAccount } from './support/session';

const NOW = new Date().toISOString();

function dimension(key: string, title: string, judgment: string, visible: boolean, focus = false) {
  return { key, title, judgment, status: 'resolved', visible, isFocus: focus, hasPendingQuestion: false, discussionSummary: '', internal: false, questionId: `q-${key}`, knownFacts: [], assumptions: [], importanceReason: '', requiresResponse: false };
}
function question(workspaceId: string, rootId: string, key: string, title: string, status: string) {
  return { id: `q-${key}`, workspaceId, sourceNodeId: rootId, sourceMessageId: null, reasoningNodeId: null, presentation: 'canvas_question', question: `${title}具体是什么?`, whyNow: '它决定路线。', analysisSummary: `${title}的判断。`, recommendation: '', decisionImpact: '', confidenceNote: null, responseMode: 'free_text', options: [], allowCustomInput: true, status, answer: null, v1Key: key, v1Analysis: { judgment: `${title}的判断。`, knownFacts: [], assumptions: [], evidence: [], status, discussionCount: 0 }, v1Title: title, v1Visible: true, v1RequiresResponse: status === 'pending', createdAt: NOW, updatedAt: NOW, answeredAt: null };
}
function baseView(workspaceId: string, rootId: string, overrides: Record<string, unknown>) {
  return { workspaceId, sessionId: 's', rootPlanNodeId: rootId, phase: 'roadmap_draft', turnAction: 'analyze', status: 'ready', mapVersion: 5, focusHandle: null, focusReasoningNodeId: null, focusReason: null, intakeQuestionsAsked: 0, intakeQuestionLimit: 5, pendingIntake: null, workflowStage: null, discoveryQuestions: [], v1Stage: null, v1Status: 'idle', v1WorkflowNext: null, v1VisibleAnalysisKeys: [], v1HiddenAnalysisCount: 0, v1ActualPendingQuestionCount: 0, v1FocusKey: null, v1Dimensions: [], v1StrategicThesis: '', v1CandidateDirections: null, v1Strategy: null, v1RequireOpenjiuwen: true, v01Timeline: [], v01TimelineProposalId: null, datesCalibrated: false, inputVersion: 'v1', strategyProposalId: null, exploredAt: null, lastEvaluatedAt: null, nodes: [], links: [], error: null, ...overrides };
}
async function mock(page: Page, view: Record<string, unknown>, questions: unknown[]) {
  await page.route('**/api/workspaces/*/reasoning', r => r.request().method() === 'GET' ? r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(view) }) : r.continue());
  await page.route('**/api/workspaces/*/questions*', r => r.request().method() === 'GET' ? r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ questions, truncated: false }) }) : r.continue());
  await page.route('**/api/workspaces/*/agent/turn', r => r.request().method() === 'POST' ? r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ reasoning: view, message: null, question: null, replayed: true, degraded: false, degradedReason: null, retryable: false, changed: false, proposalErrors: [] }) }) : r.continue());
  await page.route('**/agent/v1/interaction', r => r.fulfill({ status: 204, body: '' }));
}
const dims = [dimension('true_intent', '真实意图', '想省时间。', true, true), dimension('key_conflict', '核心矛盾', '怕学了用不上。', true), dimension('goal_definition', '目标定义', '做出一个自动化小工具。', true)];

test.beforeAll(async ({ request }) => { await assertBackendRunning(request); });

test('first screen: only root + ready prompt, no phases', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-phase-init');
  const workspaceId = await createWorkspace(page, token, '首屏', '我想学 Python 用于自动化');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(n => n.parentId === null)!.id;
  await mock(page, baseView(workspaceId, rootId, { v1Stage: 'initial_thinking', v1Dimensions: [] }), []);
  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  await expect(page.locator('.v1-phase-node')).toHaveCount(0, { timeout: 20000 });
  await expect(page.getByTestId('v1-initial-guide')).toContainText('准备好开始了吗');
  await expect(page.locator('.react-flow__node-question')).toHaveCount(0);
});

test('started: three-phase chain, think active, plan/do locked; no dashboard', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-phase-start');
  const workspaceId = await createWorkspace(page, token, '三阶段', '我想学 Python 用于自动化');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(n => n.parentId === null)!.id;
  await mock(page, baseView(workspaceId, rootId, {
    v1Stage: 'strategy_draft',
    v1StrategicThesis: '先做出一个能展示的最小项目。',
    v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
    v1Dimensions: dims,
  }), [
    question(workspaceId, rootId, 'true_intent', '真实意图', 'pending'),
    question(workspaceId, rootId, 'key_conflict', '核心矛盾', 'resolved'),
    question(workspaceId, rootId, 'goal_definition', '目标定义', 'resolved'),
  ]);
  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  const phases = page.locator('.v1-phase-node');
  await expect(phases).toHaveCount(3, { timeout: 20000 });
  await expect(phases.nth(0)).toContainText('想清楚');
  await expect(phases.nth(1)).toContainText('排出来');
  await expect(phases.nth(2)).toContainText('做起来');
  await expect(phases.nth(0)).toContainText('讨论中');
  await expect(phases.nth(1)).toContainText('已锁定');
  await expect(phases.nth(2)).toContainText('已锁定');
  await expect(page.locator('.v1-phase-node.is-active')).toHaveCount(1);

  // 子节点:三个分析节点锚在想清楚下面。
  await expect(page.locator('.react-flow__node-question')).toHaveCount(3);

  // 对话区没有战略仪表盘。
  const panel = page.locator('.floating-conversation');
  await expect(panel.locator('.v1-thesis')).toHaveCount(0);
  await expect(panel.locator('.v1-understanding')).toHaveCount(0);
  await expect(panel.locator('.v1-strategy-card')).toHaveCount(0);
  await expect(panel.locator('.v1-direction-adopted')).toHaveCount(0);
});

test('strategy confirmed: think done, plan unlocked and awaiting', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-phase-plan');
  const workspaceId = await createWorkspace(page, token, '排出来', '我想学 Python 用于自动化');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(n => n.parentId === null)!.id;
  await mock(page, baseView(workspaceId, rootId, {
    v1Stage: 'coarse_timeline_review',
    v1Status: 'awaiting_user_confirmation',
    v1Strategy: { mainLine: '先跑通最小闭环', confirmed: true },
    v01TimelineProposalId: 'p-1',
    v1CurrentInteraction: {
      id: 'ci-timeline-review', nonce: 'n1', kind: 'timeline_review', priority: 'high',
      title: '确认粗时间架构', context: '先跑通最小闭环。', whyNow: '确认前不写正式计划。',
      prompt: '确认这份粗时间架构。', options: [], recommendedOption: null, focusKey: null,
      status: 'active', presentation: 'focus_modal',
    },
    v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
    v1Dimensions: dims,
  }), []);
  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  const phases = page.locator('.v1-phase-node');
  await expect(phases).toHaveCount(3, { timeout: 20000 });
  await expect(phases.nth(0)).toContainText('已完成');
  await expect(phases.nth(1)).toContainText('等待确认');
  await expect(phases.nth(2)).toContainText('已锁定');
  await expect(page.locator('.v1-phase-node.is-active')).toHaveCount(1);

  // 对话区的「定位到节点」把排出来阶段选中。
  await page.getByTestId('chat-action-notice').getByRole('button', { name: '定位到节点' }).click();
  await expect(page.locator('.v1-phase-node.is-focused')).toHaveCount(1);
  await expect(page.locator('.v1-phase-node.is-focused')).toContainText('排出来');
});

test('粗时间线确认后:做起来阶段出现“是否细化为具体执行计划？”节点', async ({ page }) => {
  const { token } = await registerAccount(page, 'v1-phase-do');
  const workspaceId = await createWorkspace(page, token, '做起来', '我想学 Python 用于自动化');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(n => n.parentId === null)!.id;
  await mock(page, baseView(workspaceId, rootId, {
    v1Stage: 'weekly_execution',
    v1Strategy: { mainLine: '先跑通最小闭环', confirmed: true },
    v1VisibleAnalysisKeys: ['true_intent', 'key_conflict', 'goal_definition'],
    v1Dimensions: dims,
  }), []);
  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);
  const phases = page.locator('.v1-phase-node');
  await expect(phases).toHaveCount(3, { timeout: 20000 });
  await expect(phases.nth(0)).toContainText('已完成');
  await expect(phases.nth(2)).toContainText('讨论中');
  // 做起来阶段下唯一的可操作节点。
  await expect(page.locator('.v1-interaction-node')).toHaveCount(1);
  await expect(page.getByTestId('v1-stage-question')).toContainText('是否细化为具体执行计划');
  await expect(page.getByTestId('v1-stage-question')).toHaveAttribute('data-phase', 'do');
});
