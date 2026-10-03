/**
 * 阶段 8:路线优先的战略规划闭环。
 *
 * 验的是产品行为(脚本是测试脚手架,后端会记 `model_source=scripted`):
 *
 * 1. 第一轮先给**推荐路线 + 3–5 个阶段**,每阶段有粗粒度时间与成果物;
 * 2. 战略阶段不问执行细节(最多一个活动问题,且不问每周投入);
 * 3. 确认战略前没有任何 PlanNode 执行写入;
 * 4. 确认战略走既有 proposal → 校验 → 用户确认链路;
 * 5. **用户明确点“细化第一阶段”之后**,才生成阶段/里程碑计划条目。
 *
 * ## 脚本游标:自动探索也吃一轮
 *
 * `space_entered`(进入目标时的自动探索)本身会调一次 reasoner,按空间占用脚本的
 * **第一轮**。所以本 fixture 的第一轮就是路线图,第二轮留给“细化第一阶段”。
 * 之前 strategy-layer 的旧 fixture 把第一轮当成用户消息轮,结果被自动探索吃掉,
 * 第二轮“细化”拿到空脚本 —— 现在按这个顺序写死,不再靠运气。
 *
 * 没有脚本时整组 `skip`(与 `interview-loop.spec.ts` 同一条纪律)。
 */

import { expect, test, type Page } from '@playwright/test';
import {
  assertBackendRunning,
  createWorkspace,
  getPlan,
  registerAccount,
  waitForRealPlan,
} from './support/session';

const SCRIPTED = process.env.ZHITU_SCRIPTED_ACTIONS ?? '';

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
  test.skip(
    !SCRIPTED.includes('strategy-script'),
    '这一条要 --script=apps/web/tests/fixtures/strategy-script.json 才跑得起来',
  );
});

const routeCard = (page: Page) => page.locator('.reasoning-node').filter({ hasText: '推荐路线' });
const stageCards = (page: Page) => page.locator('.reasoning-node .rn-type').filter({ hasText: '阶段' });
const questionCard = (page: Page) => page.locator('.canvas-question-node');

test('路线优先:先给推荐路线与阶段,确认战略后才允许细化', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'strategy-roadmap');
  const workspaceId = await createWorkspace(
    page,
    token,
    '战略路线空间',
    '我想学习 Python 做数据分析，每周 150 分钟。',
  );
  await page.goto(`/workbench?workspace=${workspaceId}`);
  // 阶段 12:时间架构生成后工作台会自动切到时间线;这条用例验的是路径画布,
  // 所以等自动切换发生后切回路径页。
  await expect(page).toHaveURL(/view=timeline/, { timeout: 25000 });
  await page.getByRole('tab', { name: '路径', exact: true }).click();
  await expect(page).toHaveURL(/view=path/);
  await waitForRealPlan(page);
  await expect(routeCard(page), '没有出现推荐路线').toBeVisible({ timeout: 25000 });
  await expect(stageCards(page), '阶段数量不对').toHaveCount(4);
  // 每阶段有粗粒度时间带与成果物。
  await expect(routeCard(page)).toContainText('约 12 周');
  await expect(page.locator('.reasoning-node .rn-timeframe').first()).toContainText('周');
  await expect(page.locator('.reasoning-node .rn-deliverable').first()).toContainText('成果');
  // 路线/阶段不是业务计划任务。
  const roadmapPlan = await getPlan(page, token, workspaceId);
  expect(
    roadmapPlan.nodes.some((node) => ['phase', 'week', 'day'].includes(node.planningLevel ?? '')),
    '路线阶段不该被写进执行计划',
  ).toBe(false);

  // ---- 1b. 思考层默认折叠:风险/维度不占主画布，点开才出现 ----
  const thinkingToggle = page.getByRole('button', { name: /思考层/ });
  await expect(thinkingToggle, '没有思考层开关').toBeVisible();
  const riskCard = page.locator('.reasoning-node').filter({ hasText: '时间可能不够' });
  await expect(riskCard, '风险节点默认不该占主画布').toHaveCount(0);
  await thinkingToggle.click();
  await expect(page.getByRole('button', { name: '收起思考层' })).toBeVisible();
  await expect(riskCard, '展开思考层后应该看得到风险节点').toBeVisible();
  await page.getByRole('button', { name: '收起思考层' }).click();
  await expect(riskCard).toHaveCount(0);

  // ---- 1d. 研究只做证据，不是主路线上的独立节点 ----
  // 没有配置联网时路线照常生成（上面已经看到）；来源只折叠在阶段的“资料与依据”里。
  const evidence = page.locator('.reasoning-node').filter({ hasText: '阶段 1' }).locator('.rn-evidence');
  await expect(evidence, '阶段缺少“资料与依据”').toHaveCount(1);
  await expect(evidence.locator('ul'), '依据默认应该是折叠的').not.toBeVisible();
  await evidence.locator('summary').click();
  await expect(evidence.locator('ul')).toBeVisible();
  // 主路线节点里没有“研究/来源”这一类独立大节点。
  await expect(page.locator('.reasoning-node .rn-type').filter({ hasText: '研究' })).toHaveCount(0);

  // ---- 1c. 主路线节点之间不重叠 ----
  const boxes = await page.locator('.react-flow__node-reasoning').evaluateAll((els) =>
    els.map((el) => {
      const rect = el.getBoundingClientRect();
      return { x: rect.x, y: rect.y, w: rect.width, h: rect.height };
    }),
  );
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const a = boxes[i];
      const b = boxes[j];
      const overlap = a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
      expect(overlap, `推理节点重叠:${JSON.stringify(a)} vs ${JSON.stringify(b)}`).toBe(false);
    }
  }

  // ---- 2. 战略阶段不问执行细节 ----
  // 最多一个活动问题;而且不问“每周投入”(用户已经给了 150 分钟)。
  await expect(questionCard(page), '战略阶段最多一个问题').toHaveCount(1);
  await expect(questionCard(page)).not.toContainText('每周');

  // ---- 3. 确认战略前没有任何执行写入 ----
  const beforeConfirm = await getPlan(page, token, workspaceId);
  expect(beforeConfirm.nodes.some((node) => node.planningLevel === 'strategy')).toBe(false);

  // ---- 4. 确认战略:点路线 -> 确认这条战略 -> 提案 -> 确认写入 ----
  await routeCard(page).click();
  const detail = page.locator('.reasoning-detail');
  await expect(detail).toBeVisible();
  await detail.getByRole('button', { name: '确认这条战略' }).click();

  const proposal = page.locator('.proposal').filter({ hasText: '推荐路线' }).first();
  await expect(proposal).toBeVisible({ timeout: 20000 });
  // 提案确认之前,战略节点仍未写入。
  const stillBefore = await getPlan(page, token, workspaceId);
  expect(stillBefore.nodes.some((node) => node.planningLevel === 'strategy')).toBe(false);

  await proposal.getByRole('button', { name: '确认，写入计划' }).click();
  await expect
    .poll(
      async () => (await getPlan(page, token, workspaceId)).nodes.some((node) => node.planningLevel === 'strategy'),
      { message: '确认之后画布上还是没有战略节点', timeout: 20000 },
    )
    .toBe(true);

  // ---- 5. 只有用户明确点“细化第一阶段”之后,才生成阶段计划 ----
  const refine = page.getByRole('button', { name: '细化第一阶段' });
  await expect(refine, '战略确认后没有出现“细化第一阶段”').toBeVisible({ timeout: 20000 });
  await refine.click();

  const phaseProposal = page.locator('.proposal').filter({ hasText: '第一阶段' }).first();
  await expect(phaseProposal, '细化之后没有生成阶段提案').toBeVisible({ timeout: 20000 });
  await phaseProposal.getByRole('button', { name: '确认，写入计划' }).click();
  await expect
    .poll(
      async () => (await getPlan(page, token, workspaceId)).nodes.some((node) => node.planningLevel === 'phase'),
      { message: '确认之后还是没有阶段层节点', timeout: 20000 },
    )
    .toBe(true);
});
