import { expect, test, type Page } from '@playwright/test';
import { assertBackendRunning, createWorkspace, registerAccount } from './support/session';

/**
 * 首页「规划已生效」区域。
 *
 * 粗时间线确认后,首页必须能看到当前阶段 / 下一里程碑;周计划确认后看到本周重点;
 * 没有日工作块时如实说清楚,不伪造今日任务。数据来自和任务面板同一份 `/plan`。
 */

const NOW = new Date().toISOString();
const TODAY = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());

function node(over: Record<string, unknown>) {
  return {
    description: null, acceptanceCriteria: null, purpose: 'planning', planningLevel: null,
    status: 'pending', priority: 'medium', estimateMinutes: null, deadline: null,
    depth: 0, orderIndex: 0, origin: 'user', completedAt: null, createdAt: NOW,
    contentVersion: 1, v1Key: null, v1Analysis: null, ...over,
  };
}

function planPayload(workspaceId: string, sessions: unknown[]) {
  const rootId = `root-${workspaceId}`;
  const phaseId = `phase-${workspaceId}`;
  const weekId = `week-${workspaceId}`;
  return {
    workspaceId,
    revisionVersion: 1,
    nodes: [
      node({ id: rootId, parentId: null, title: '根目标', nodeType: 'goal', depth: 0 }),
      node({
        id: phaseId, parentId: rootId, title: '阶段一 · 打基础', nodeType: 'stage', depth: 1,
        description: '相对范围:第 1–2 周\n目标:把基础打牢\n成果:一份笔记\n完成标准:能复述',
      }),
      node({ id: `milestone-${workspaceId}`, parentId: phaseId, title: '月度里程碑 · 第一版可用', nodeType: 'stage', depth: 2, description: '阶段内关键里程碑\n目标:跑通最小闭环' }),
      node({ id: weekId, parentId: phaseId, title: '本周计划:阶段一 · 第 1 版', nodeType: 'stage', depth: 2 }),
      node({ id: `task-a-${workspaceId}`, parentId: weekId, title: '写第一版脚本', nodeType: 'task', depth: 3, priority: 'high' }),
      node({ id: `task-b-${workspaceId}`, parentId: weekId, title: '整理数据', nodeType: 'task', depth: 3, status: 'completed' }),
    ],
    dependencies: [],
    relations: [],
    brief: { version: 1, goal: null, deadline: null, weeklyAvailableMinutes: null, currentLevel: null, successCriteria: null, constraints: [], missing: [] },
    sessions,
    totalNodes: 2,
    completedNodes: 1,
  };
}

async function mock(page: Page, workspaceId: string, todayItems: unknown[]) {
  await page.route('**/api/workspaces/*/plan', route =>
    route.request().method() === 'GET'
      ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(planPayload(workspaceId, [])) })
      : route.continue(),
  );
  await page.route('**/api/today', route =>
    route.request().method() === 'GET'
      ? route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          today: TODAY, timezone: 'Asia/Shanghai', workspaces: todayItems, plannedMinutes: 30,
          actualMinutes: 0, itemCount: todayItems.length, recordedCount: 0, checkInQuestions: [],
          note: '还没有执行记录。',
        }),
      })
      : route.continue(),
  );
  await page.route('**/api/reminders', route =>
    route.request().method() === 'GET'
      ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ reminders: [], quietHours: null, suppressedCount: 0, note: '' }) })
      : route.continue(),
  );
}

test.beforeAll(async ({ request }) => { await assertBackendRunning(request); });

test('粗时间线 / 周计划确认后:首页展示当前阶段、里程碑与本周重点;无日计划时如实说明', async ({ page }) => {
  const { token } = await registerAccount(page, 'today-plan-focus');
  const workspaceId = await createWorkspace(page, token, '首页规划', '确认规划后首页要跟着变');
  await mock(page, workspaceId, []);
  // 需要一个“当前空间”provider 才不会把 /today 送回 /spaces —— 带上 workspace 参数。
  await page.waitForURL(/\/spaces/, { timeout: 15000 }).catch(() => undefined);
  await page.goto(`/today?workspace=${workspaceId}`);
  const focus = page.getByTestId('plan-focus');
  await expect(focus).toBeVisible({ timeout: 20000 });
  // 当前阶段 + 阶段目标。
  await expect(focus).toContainText('阶段一 · 打基础');
  await expect(focus).toContainText('把基础打牢');
  // 下一里程碑。
  await expect(focus).toContainText('第一版可用');
  // 本周重点:未完成数 + 最高优先级任务 + 任务面板入口。
  await expect(focus).toContainText('还有 1 件事没完成');
  await expect(focus).toContainText('写第一版脚本');
  await expect(focus.getByRole('link', { name: /去任务面板/ })).toBeVisible();
  // 没有日工作块:如实说明,不伪造今日任务。
  await expect(page.getByTestId('plan-focus').getByText('本周计划已确认，尚未生成今天的工作块。')).toBeVisible();
});

test('日计划确认后:首页展示今天的工作块', async ({ page }) => {
  const { token } = await registerAccount(page, 'today-plan-block');
  const workspaceId = await createWorkspace(page, token, '首页今日块', '确认日计划后首页要看到');
  await page.waitForURL(/\/spaces/, { timeout: 15000 }).catch(() => undefined);
  await mock(page, workspaceId, [{
    workspaceId,
    title: '首页今日块',
    items: [{
      sessionId: `s-${workspaceId}`, nodeId: `task-a-${workspaceId}`, workspaceId, workspaceTitle: '首页今日块',
      nodeTitle: '写第一版脚本', plannedMinutes: 30, bufferMinutes: 0, seq: 1,
      startMinute: null, endMinute: null, status: 'planned', locked: false,
      result: null, actualMinutes: null, delayReason: null, recorded: false,
    }],
  }]);

  await page.goto(`/today?workspace=${workspaceId}`);
  // 今天的工作块出现在「今天最重要的事」里。
  await expect(page.locator('.today-item-title').first()).toContainText('写第一版脚本', { timeout: 20000 });
  // 已有日工作块时不再显示“尚未生成”的提示。
  await expect(page.getByTestId('plan-focus').getByText('本周计划已确认，尚未生成今天的工作块。')).toHaveCount(0);
});
