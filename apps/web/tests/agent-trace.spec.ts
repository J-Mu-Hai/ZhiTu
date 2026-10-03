/**
 * 本地运行记录(阶段 9)。
 *
 * ## 验什么
 *
 * 1. 诊断入口是**服务端开关**决定的:隔离栈是 `APP_ENV=development`,所以默认可见;
 * 2. 打开后看到的是**服务端真实执行边界**产生的 turn(触发来源、步骤、耗时),
 *    而不是前端自己编的一段进度;
 * 3. 它**不显示用户原文** —— 这是这一组存在的最重要一条。
 *
 * 这一条不需要脚本:rule 兜底也会真的走一轮 `agent_loop_service`,落一个 completed
 * 的轨迹。它不依赖任何模型 key。
 */

import { expect, test, type Page } from '@playwright/test';
import {
  API_BASE,
  assertBackendRunning,
  createWorkspace,
  getPlan,
  registerAccount,
  waitForRealPlan,
} from './support/session';

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('运行记录:入口可见、状态来自服务端、不泄漏用户原文', async ({ page }) => {
  const { token } = await registerAccount(page, 'agent-trace');
  const workspaceId = await createWorkspace(page, token, '运行记录空间');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 没有问题时,标题下不应该有那条状态条。
  await expect(page.locator('.question-status-bar')).toHaveCount(0);

  // 发一句 —— 产生一个真实 turn。
  const utterance = '这是一句只属于用户的私密原文 ZEBRA-9911';
  await page.getByRole('textbox', { name: '给 AI 的消息' }).fill(utterance);
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(
    page.locator('.floating-conversation .message').filter({ hasText: 'ZEBRA-9911' }),
  ).toBeVisible({ timeout: 20000 });

  // 入口在服务端开关探测完成后出现。
  const entry = page.getByTestId('trace-entry');
  await expect(entry).toBeVisible();

  await entry.click();
  const inspector = page.getByTestId('trace-inspector');
  await expect(inspector).toBeVisible();
  await expect(inspector).toContainText('运行记录');

  // 至少有一个 turn,而且触发来源是真实的服务端记录。
  const turn = inspector.getByTestId('trace-turn').first();
  await expect(turn).toBeVisible();
  await expect(turn).toHaveAttribute('data-trigger', 'user_message');
  // 状态是服务端终态,不是"永远转圈"。
  await expect(turn).toHaveAttribute('data-status', /completed|failed|timed_out/);
  await expect(turn).toContainText('秒', { timeout: 20000 });

  // **脱敏红线**:诊断抽屉里不能出现用户的原文。
  await expect(inspector).not.toContainText('ZEBRA-9911');
  await expect(inspector).not.toContainText('私密原文');

  // Escape 关闭,不挤压画布。
  await page.keyboard.press('Escape');
  await expect(inspector).toHaveCount(0);
  // 关掉之后对话正文还在(抽屉没有把状态带走)。
  await expect(
    page.locator('.floating-conversation .message').filter({ hasText: 'ZEBRA-9911' }),
  ).toBeVisible();
});

test('运行记录展示地图轮触发来源(进入空间)', async ({ page }) => {
  const { token } = await registerAccount(page, 'agent-trace-map');
  const workspaceId = await createWorkspace(page, token, '地图轮轨迹空间');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  /*
   * 进入空间时服务端会自动跑一轮地图探索(`space_entered`,幂等)。它**不经过**
   * 普通对话循环,但必须出现在同一个抽屉里。
   *
   * 先直接轮询接口等它落库,再开抽屉 —— 否则"抽屉在轨迹创建前打开"会让它读不到,
   * 而那不是产品问题,是测试的时序。
   */
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `${API_BASE}/api/workspaces/${workspaceId}/agent/trace`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!response.ok()) return 0;
        const body = (await response.json()) as { turns: { trigger: string }[] };
        return body.turns.filter(turn => turn.trigger === 'space_entered').length;
      },
      { timeout: 20000 },
    )
    .toBeGreaterThan(0);

  const entry = page.getByTestId('trace-entry');
  await expect(entry).toBeVisible();
  await entry.click();
  const inspector = page.getByTestId('trace-inspector');
  await expect(inspector).toBeVisible();

  const mapTurn = inspector.locator('[data-testid="trace-turn"][data-trigger="space_entered"]');
  await expect(mapTurn.first()).toBeVisible();
  // 触发来源是给人看的中文标签,不是枚举值。
  await expect(mapTurn.first()).toContainText('进入空间');
  // 终态来自服务端(rule 兜底下通常是 failed/ROADMAP_INVALID,同样可读)。
  await expect(mapTurn.first()).toHaveAttribute('data-status', /completed|failed|timed_out/);
  await expect(mapTurn.first().locator('.trace-summary')).not.toHaveCount(0);
});

// ---------------------------------------------------------------------------------
// 阶段 9.2:运行记录可用性状态 + 统一 Agent 活动状态
//
// 这些用例用 `page.route` 把 `/agent/trace` 换成受控响应:不是为了绕过后端,而是
// 为了造出“旧后端 404 / 网络不可达 / 模型还在等”这些单测与真实栈都不容易稳定复现的
// 状态。断言的是**界面如何呈现服务端给的事实**。
// ---------------------------------------------------------------------------------

type TraceResponder = () => { status: number; body: unknown } | 'abort';

function makeTraceView(workspaceId: string, turns: unknown[]) {
  return {
    workspaceId,
    workspaceShortId: workspaceId.slice(0, 8),
    enabled: true,
    generatedAt: new Date().toISOString(),
    limit: 20,
    turns,
  };
}

function traceTurn(overrides: Record<string, unknown> = {}) {
  return {
    id: 'turn-1',
    shortId: 'turn-1',
    trigger: 'space_entered',
    triggerLabel: '进入空间',
    currentStep: 'waiting_model',
    stepLabel: '等待模型',
    status: 'running',
    terminal: false,
    steps: ['queued', 'resolving_context', 'waiting_model'],
    startedAt: new Date().toISOString(),
    finishedAt: null,
    durationMs: null,
    lastProgressAt: new Date().toISOString(),
    waitingSeconds: 12,
    waitingTooLong: false,
    attempt: 1,
    terminalCode: null,
    safeSummary: null,
    tools: [],
    retryable: false,
    ...overrides,
  };
}

/** 安装一个**可变**的轨迹 mock;返回 setter,测试中途可以切换响应。 */
async function installTraceMock(page: Page, initial: TraceResponder) {
  let responder = initial;
  await page.route('**/api/workspaces/*/agent/trace*', async route => {
    const result = responder();
    if (result === 'abort') return route.abort('failed');
    return route.fulfill({
      status: result.status,
      contentType: 'application/json',
      body: JSON.stringify(result.body),
    });
  });
  return {
    set(next: TraceResponder) {
      responder = next;
    },
  };
}

/** 让自动 `space_entered` 不再触发:把地图读成“已经就绪”。 */
async function installReadyReasoningMock(page: Page, workspaceId: string, rootId: string) {
  await page.route('**/api/workspaces/*/reasoning', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        workspaceId,
        sessionId: 'session-ready',
        rootPlanNodeId: rootId,
        phase: 'orientation',
        turnAction: 'analyze',
        status: 'ready',
        mapVersion: 1,
        focusHandle: null,
        focusReasoningNodeId: null,
        focusReason: null,
        inputVersion: 'v1',
        strategyProposalId: null,
        exploredAt: null,
        lastEvaluatedAt: null,
        nodes: [],
        links: [],
        error: null,
      }),
    });
  });
}

/** 让自动 `space_entered` 一定触发;同时按住 `/agent/turn` 的响应。 */
async function installDelayedMapTurn(page: Page) {
  await page.route('**/api/workspaces/*/reasoning', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        workspaceId: '00000000-0000-0000-0000-000000000000',
        sessionId: null,
        rootPlanNodeId: null,
        phase: 'orientation',
        turnAction: 'analyze',
        status: 'idle',
        mapVersion: 0,
        focusHandle: null,
        focusReasoningNodeId: null,
        focusReason: null,
        inputVersion: null,
        strategyProposalId: null,
        exploredAt: null,
        lastEvaluatedAt: null,
        nodes: [],
        links: [],
        error: null,
      }),
    });
  });
  let release = () => {};
  const gate = new Promise<void>(resolve => { release = resolve; });
  let delay = true;
  await page.route('**/api/workspaces/*/agent/turn', async route => {
    if (delay) await gate;
    await route.continue();
  });
  return {
    release() {
      delay = false;
      release();
    },
  };
}

test('运行记录探测:500 不再伪装成可用,重试后恢复', async ({ page }) => {
  const { token } = await registerAccount(page, 'trace-availability');
  const workspaceId = await createWorkspace(page, token, '运行记录可用性空间');
  const mock = await installTraceMock(page, () => ({
    status: 500,
    body: { error: { code: 'DB_UNAVAILABLE', message: '数据库暂时不可用。' } },
  }));

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  const chip = page.getByTestId('trace-unavailable');
  await expect(chip).toBeVisible({ timeout: 15000 });
  // **不显示正常的“运行记录”入口** —— 那会把一次 500 说成可用。
  await expect(page.getByTestId('trace-entry')).toHaveCount(0);

  // 展开:脱敏错误码、HTTP 状态、后端地址、操作建议。
  await chip.getByRole('button', { name: '运行记录暂不可用' }).click();
  const detail = page.getByTestId('trace-unavailable-detail');
  await expect(detail).toBeVisible();
  await expect(detail.getByTestId('trace-error-code')).toHaveText('DB_UNAVAILABLE');
  await expect(detail.getByTestId('trace-error-status')).toHaveText('500');
  await expect(detail.getByTestId('trace-error-api')).toHaveText(API_BASE);
  await expect(detail).toContainText('请确认本地后端已重启');
  // 不泄漏凭据。
  await expect(detail).not.toContainText('Bearer');
  await expect(detail).not.toContainText(token);
  await page.keyboard.press('Escape');
  await expect(detail).toHaveCount(0);

  // 后端恢复 -> 点“重试连接”自动转成正常入口。
  mock.set(() => ({ status: 200, body: makeTraceView(workspaceId, []) }));
  await chip.getByRole('button', { name: '重试连接' }).click();
  await expect(page.getByTestId('trace-entry')).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId('trace-unavailable')).toHaveCount(0);
});

test('运行记录探测:旧后端 404 与网络不可达各有可读状态', async ({ page }) => {
  const { token } = await registerAccount(page, 'trace-old-backend');
  const workspaceId = await createWorkspace(page, token, '旧后端排查空间');
  const mock = await installTraceMock(page, () => ({ status: 404, body: { detail: 'Not Found' } }));

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  const chip = page.getByTestId('trace-unavailable');
  await expect(chip).toBeVisible({ timeout: 15000 });
  await chip.getByRole('button', { name: '运行记录暂不可用' }).click();
  const detail = page.getByTestId('trace-unavailable-detail');
  await expect(detail.getByTestId('trace-error-code')).toHaveText('HTTP_404');
  await expect(detail.getByTestId('trace-error-status')).toHaveText('404');
  await page.keyboard.press('Escape');

  // 网络不可达(连接被拒)。
  mock.set(() => 'abort');
  await chip.getByRole('button', { name: '重试连接' }).click();
  await expect(chip).toBeVisible();
  await chip.getByRole('button', { name: '运行记录暂不可用' }).click();
  await expect(detail.getByTestId('trace-error-code')).toHaveText('NETWORK_UNREACHABLE');
  await expect(detail.getByTestId('trace-error-status')).toHaveText('—');
});

test('运行记录探测:TRACE_DISABLED 时一个入口都不显示', async ({ page }) => {
  const { token } = await registerAccount(page, 'trace-disabled');
  const workspaceId = await createWorkspace(page, token, '诊断关闭空间');
  await installTraceMock(page, () => ({
    status: 404,
    body: { error: { code: 'TRACE_DISABLED', message: '本地诊断入口没有开启。' } },
  }));

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  await expect(page.getByTestId('trace-entry')).toHaveCount(0);
  await expect(page.getByTestId('trace-unavailable')).toHaveCount(0);
});

test('自动地图轮:不打开抽屉也显示服务端运行状态,并在终态收口', async ({ page }) => {
  const { token } = await registerAccount(page, 'trace-live-map');
  const workspaceId = await createWorkspace(page, token, '运行中状态空间');
  const mock = await installTraceMock(page, () => ({
    status: 200,
    body: makeTraceView(workspaceId, [traceTurn()]),
  }));
  const turn = await installDelayedMapTurn(page);

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  const bar = page.getByTestId('agent-activity');
  await expect(bar).toBeVisible({ timeout: 15000 });
  await expect(bar).toHaveAttribute('data-tone', 'running');
  await expect(bar).toContainText('正在等待模型响应');
  // 等待秒数**来自服务端**,不是前端自己数的。
  await expect(bar).toContainText('已 12 秒');
  // 抽屉没有被打开。
  await expect(page.getByTestId('trace-inspector')).toHaveCount(0);

  // 服务端转成终态 -> 请求放行 -> 状态条短暂显示“已完成”后自然消失。
  mock.set(() => ({
    status: 200,
    body: makeTraceView(workspaceId, [
      traceTurn({
        id: 'turn-done',
        currentStep: 'completed',
        stepLabel: '已完成',
        status: 'completed',
        terminal: true,
        terminalCode: 'COMPLETED',
        safeSummary: '本轮已完成，等待你确认或无需更改。',
        waitingSeconds: null,
      }),
    ]),
  }));
  await expect(bar).toHaveAttribute('data-tone', 'completed', { timeout: 10000 });
  turn.release();
  await expect(bar).toHaveCount(0, { timeout: 8000 });
});

test('自动地图轮:超过阈值提示“仍在等待”并可打开运行记录', async ({ page }) => {
  const { token } = await registerAccount(page, 'trace-wait-long');
  const workspaceId = await createWorkspace(page, token, '长时间等待空间');
  await installTraceMock(page, () => ({
    status: 200,
    body: makeTraceView(workspaceId, [traceTurn({ waitingSeconds: 30, waitingTooLong: true })]),
  }));
  await installDelayedMapTurn(page);

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  const bar = page.getByTestId('agent-activity');
  await expect(bar).toBeVisible({ timeout: 15000 });
  await expect(bar).toHaveAttribute('data-tone', 'waiting_long');
  await expect(bar).toContainText('仍在等待模型响应');
  await expect(bar).toContainText('已 30 秒');
  // 未超时**不显示失败**,超时后给一个通往运行记录的入口。
  await expect(bar.getByRole('button', { name: '打开运行记录' })).toBeVisible();
  await expect(bar.getByRole('button', { name: '重试' })).toHaveCount(0);
});

test('自动地图轮:结构校验失败保留可关闭结论与服务端允许的重试', async ({ page }) => {
  const { token } = await registerAccount(page, 'trace-map-failed');
  const workspaceId = await createWorkspace(page, token, '结构失败空间');
  const mock = await installTraceMock(page, () => ({
    status: 200,
    body: makeTraceView(workspaceId, [traceTurn()]),
  }));
  const turn = await installDelayedMapTurn(page);

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  const bar = page.getByTestId('agent-activity');
  await expect(bar).toBeVisible({ timeout: 15000 });
  mock.set(() => ({
    status: 200,
    body: makeTraceView(workspaceId, [
      traceTurn({
        id: 'turn-failed',
        currentStep: 'failed',
        stepLabel: '失败',
        status: 'failed',
        terminal: true,
        terminalCode: 'ROADMAP_INVALID',
        safeSummary: '模型给出的路线结构不合法，本轮没有写入任何节点，可以重试。',
        retryable: true,
        waitingSeconds: null,
      }),
    ]),
  }));
  await expect(bar).toHaveAttribute('data-tone', 'failed', { timeout: 10000 });
  await expect(bar).toContainText('路线结构不合法');
  await expect(bar.getByRole('button', { name: '打开运行记录' })).toBeVisible();
  await expect(bar.getByRole('button', { name: '重试' })).toBeVisible();

  turn.release();
  // 可关闭,而且关闭后不再挂着。
  await bar.getByRole('button', { name: '关闭状态' }).click();
  await expect(bar).toHaveCount(0);
});

test('普通消息也走同一活动状态机制,且不泄露用户原文', async ({ page }) => {
  const { token } = await registerAccount(page, 'trace-message');
  const workspaceId = await createWorkspace(page, token, '消息活动空间');
  const plan = await getPlan(page, token, workspaceId);
  const rootId = plan.nodes.find(node => node.parentId === null)!.id;
  await installReadyReasoningMock(page, workspaceId, rootId);
  const mock = await installTraceMock(page, () => ({ status: 200, body: makeTraceView(workspaceId, []) }));

  let release = () => {};
  const gate = new Promise<void>(resolve => { release = resolve; });
  let released = false;
  await page.route('**/api/workspaces/*/messages', async route => {
    if (route.request().method() === 'POST' && !released) await gate;
    await route.continue();
  });

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  await expect(page.getByTestId('agent-activity')).toHaveCount(0);

  const secret = '只属于用户的私密原文 MESSAGE-SECRET-4477';
  mock.set(() => ({
    status: 200,
    body: makeTraceView(workspaceId, [
      traceTurn({ id: 'turn-msg', trigger: 'user_message', triggerLabel: '用户消息', currentStep: 'waiting_model', waitingSeconds: 7 }),
    ]),
  }));

  await page.getByRole('textbox', { name: '给 AI 的消息' }).fill(secret);
  await page.getByRole('button', { name: '发送消息', exact: true }).click();

  const bar = page.getByTestId('agent-activity');
  await expect(bar).toBeVisible({ timeout: 15000 });
  await expect(bar).toHaveAttribute('data-tone', 'running');
  await expect(bar).toContainText('正在等待模型响应');
  await expect(bar).toContainText('已 7 秒');
  // **脱敏红线**:状态条里没有用户原文。
  await expect(bar).not.toContainText('MESSAGE-SECRET-4477');

  mock.set(() => ({
    status: 200,
    body: makeTraceView(workspaceId, [
      traceTurn({
        id: 'turn-msg-done',
        trigger: 'user_message',
        triggerLabel: '用户消息',
        currentStep: 'completed',
        stepLabel: '已完成',
        status: 'completed',
        terminal: true,
        terminalCode: 'COMPLETED',
        safeSummary: '本轮已完成，等待你确认或无需更改。',
        waitingSeconds: null,
      }),
    ]),
  }));
  await expect(bar).toHaveAttribute('data-tone', 'completed', { timeout: 10000 });
  released = true;
  release();
  await expect(bar).toHaveCount(0, { timeout: 8000 });
});
