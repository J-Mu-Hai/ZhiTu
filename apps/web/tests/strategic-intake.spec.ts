/**
 * 阶段 11:Strategic Intake → 时间架构(隔离 E2E)。
 *
 * 用 `page.route` 把 `/reasoning` 与 `/questions` 换成受控响应:为的是稳定造出
 * intake(有会话、无路线)与时间架构(有 route+stage、无日期)这两个状态 ——
 * 隔离栈的规则兜底不会产出结构化问题或架构。
 *
 * 验的是界面如何呈现服务端给的事实:
 * 1. intake 关键问题只在对话区(橙色),不生成画布 Question Node;
 * 2. 时间架构生成后自动切到时间线一次,并显示“草案，尚未写入计划”;
 * 3. 没有日期时显示相对周并标“日期待校准”。
 */

import { expect, test, type Page } from '@playwright/test';
import {
  assertBackendRunning,
  createWorkspace,
  getPlan,
  registerAccount,
  waitForRealPlan,
} from './support/session';

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

const NOW = new Date().toISOString();

function reasoningView(workspaceId: string, rootId: string, overrides: Record<string, unknown>) {
  return {
    workspaceId,
    sessionId: 's-e2e',
    rootPlanNodeId: rootId,
    phase: 'intake',
    turnAction: 'ask_user',
    status: 'ready',
    mapVersion: 1,
    focusHandle: null,
    focusReasoningNodeId: null,
    focusReason: null,
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

function stage(handle: string, title: string, startWeek: number, endWeek: number) {
  return {
    id: `${handle}-id`,
    handle,
    parentHandle: 'r1',
    linkedPlanNodeId: null,
    title,
    summary: null,
    userDescription: null,
    nodeType: 'stage',
    status: 'unexplored',
    nextAction: 'analyze',
    importance: 4,
    uncertainty: 2,
    urgency: 1,
    impact: 4,
    confidence: 3,
    priority: 3,
    rationale: null,
    assumptions: [],
    evidence: [],
    timeframe: `第 ${startWeek}–${endWeek} 周`,
    deliverable: `${title}的成果`,
    passCriteria: null,
    timeframeKind: 'relative',
    startWeek,
    endWeek,
    startDate: null,
    endDate: null,
    source: 'agent',
    version: 1,
    updatedAt: NOW,
  };
}

function routeNode() {
  return {
    ...stage('r1', '推荐路线:约 8 周', 1, 8),
    id: 'r1-id',
    parentHandle: null,
    nodeType: 'route',
    title: '推荐路线:约 8 周',
    timeframe: '约 8 周',
    deliverable: '一个可展示的作品',
    startWeek: null,
    endWeek: null,
  };
}

async function installReasoningMock(page: Page, body: Record<string, unknown>) {
  await page.route('**/api/workspaces/*/reasoning', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

async function installIntakeQuestionMock(page: Page, workspaceId: string, rootId: string) {
  const question = {
    id: 'q-intake',
    workspaceId,
    sourceNodeId: rootId,
    sourceMessageId: null,
    reasoningNodeId: null,
    presentation: 'conversation_intake',
    question: '你希望最终获得什么成果?',
    whyNow: '它会改变总时长与阶段成果',
    analysisSummary: '你已给出目标方向;不同成果会改变阶段顺序。',
    recommendation: '先明确要交出的东西,再定阶段。',
    decisionImpact: '不同成果会改变阶段 2 的素材与阶段 3 的形式。',
    confidenceNote: '“尽快见效”是推断。',
    responseMode: 'single_select',
    options: [
      { id: 'portfolio', label: '一个能展示的作品', recommended: true },
      { id: 'automation', label: '自动化脚本', recommended: false },
    ],
    allowCustomInput: true,
    status: 'pending',
    answer: null,
    createdAt: NOW,
    updatedAt: NOW,
    answeredAt: null,
  };
  await page.route('**/api/workspaces/*/questions*', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ questions: [question], truncated: false }),
    });
  });
  // `ensureReasoningMap` 只有在 turn 响应带 question 时才会重拉 `/questions`。
  await page.route('**/api/workspaces/*/agent/turn', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reasoning: reasoningView(workspaceId, rootId, { sessionId: null, status: 'idle' }),
        message: null,
        question,
        replayed: false,
        degraded: false,
        degradedReason: null,
        retryable: false,
        changed: false,
        proposalErrors: [],
      }),
    });
  });
}

test('intake 关键问题只在对话区(橙色),不生成画布节点', async ({ page }) => {
  const { token } = await registerAccount(page, 'intake-e2e');
  const workspaceId = await createWorkspace(page, token, 'intake 空间', '我想学习 Python');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;
  await installReasoningMock(page, reasoningView(workspaceId, rootId, { sessionId: null, status: 'idle' }));
  await installIntakeQuestionMock(page, workspaceId, rootId);

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  const card = page.getByTestId('intake-card');
  await expect(card).toBeVisible({ timeout: 20000 });
  await expect(card).toContainText('战略校准');
  await expect(card).toContainText('AI 判断');
  await expect(card).toContainText('推荐');
  await expect(card).toContainText('你的选择会影响');
  // 推荐项明确标记。
  await expect(card.locator('.intake-option.is-recommended')).toHaveCount(1);
  // **不生成画布 Question Node。**
  await expect(page.locator('.react-flow__node-question')).toHaveCount(0);
  // 橙色边框(战略校准中)。
  const border = await card.evaluate(element => getComputedStyle(element).borderLeftColor);
  expect(border).not.toBe('rgba(0, 0, 0, 0)');
});

test('时间架构生成后自动切到时间线,显示草案预览与相对周', async ({ page }) => {
  const { token } = await registerAccount(page, 'intake-arch-e2e');
  const workspaceId = await createWorkspace(page, token, '时间架构空间', '我想学习 Python');
  const rootId = (await getPlan(page, token, workspaceId)).nodes.find(node => node.parentId === null)!.id;
  await installReasoningMock(
    page,
    reasoningView(workspaceId, rootId, {
      phase: 'temporal_architecture_draft',
      mapVersion: 3,
      nodes: [routeNode(), stage('r2', '阶段 1:打基础', 1, 2), stage('r3', '阶段 2:做项目', 3, 5), stage('r4', '阶段 3:收作品', 6, 8)],
      focusHandle: 'r2',
    }),
  );

  await page.goto(`/workbench?workspace=${workspaceId}&view=path`);

  // 自动切到时间线(不先等路径画布 —— 它会因为切视图被卸载)。
  await expect(page).toHaveURL(/view=timeline/, { timeout: 20000 });
  const preview = page.getByTestId('strategy-architecture-preview');
  await expect(preview).toBeVisible();
  await expect(preview).toContainText('草案，尚未写入计划');
  await expect(preview).toContainText('推荐路线:约 8 周');
  await expect(preview).toContainText('第 1–2 周');
  // 无日期 -> 相对周 + 日期待校准。
  await expect(page.getByTestId('arch-dates-pending')).toBeVisible();

  // 阶段卡点击回到路径页。
  await preview.locator('.arch-stage').first().click();
  await expect(page).toHaveURL(/view=path/);
});
