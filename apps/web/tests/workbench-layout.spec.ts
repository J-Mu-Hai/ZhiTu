import { expect, test } from '@playwright/test';
import { assertBackendRunning, createNode, createWorkspace, getPlan, registerAccount, waitForRealPlan } from './support/session';

/**
 * 工作台的两条布局/状态验收。
 *
 * ## 这个文件接的是 `demo2-layout` 和 `demo3-layout` 的班
 *
 * 那两个文件整份都写在示例空间上(`?workspace=primary`),因为那段演示数据"节点最多,
 * 画得满画布"。示例空间删掉之后,同一批断言改在**真实空间**里跑:空间和节点由这个
 * 测试自己通过接口搭出来。验的东西一个字没改 —— 对话卡片让出多少画布、卡片之间
 * 会不会叠、一处发出的消息在收起和换视图之后还在不在。
 *
 * ## 一条踩过的坑,留在这里免得再踩一次
 *
 * **不能点完马上量。** `.workspace` 的右内边距带 200ms 过渡,而 `toBeHidden()` 在
 * `hidden` 属性写上那一刻就通过了 —— 那时过渡才刚开始,量到的宽度还是收起之前那一帧
 * 的值,于是"变宽了"这条断言永远不成立。那是量早了一步,不是画布真的没让出空间。
 * 所以下面用的是 `expect.poll`,量到的是稳态值。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

/**
 * 一个"画得满"的空间:根目标下挂三个阶段,每个阶段再挂两个任务。
 *
 * 用接口建而不是点界面:这两条验的是布局,节点只是背景板;从界面建的话,建失败时
 * 报出来的是"某个按钮点不动",和要验的东西无关。
 */
async function seedBusyWorkspace(page: import('@playwright/test').Page, token: string, title: string): Promise<string> {
  const workspaceId = await createWorkspace(page, token, title);
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  for (const stage of ['阶段一 · 打基础', '阶段二 · 做项目', '阶段三 · 收尾']) {
    const stageId = await createNode(page, token, workspaceId, { parentId: root.id, title: stage, nodeType: 'stage' });
    for (const task of ['任务 A', '任务 B']) {
      await createNode(page, token, workspaceId, { parentId: stageId, title: `${stage.slice(0, 3)}-${task}`, nodeType: 'task' });
    }
  }
  return workspaceId;
}

test('对话卡片让出画布空间，而且不叠在一起', async ({ page }) => {
  const { token } = await registerAccount(page, 'layout');
  const workspaceId = await seedBusyWorkspace(page, token, '布局验收空间');

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 顶栏是薄薄一条,没有那种占掉一大块的侧边栏。
  const navigation = await page.locator('.top-navigation').boundingBox();
  expect(navigation!.height).toBeLessThanOrEqual(68);
  await expect(page.locator('.sidebar')).toHaveCount(0);

  const canvas = page.locator('.react-flow');
  const panel = page.getByRole('region', { name: '浮动对话卡片群' });
  const openCanvas = await canvas.boundingBox();
  expect(openCanvas!.width).toBeGreaterThan(700);
  expect(openCanvas!.width).toBeLessThan(1440);
  await expect(panel).toBeVisible();

  await page.getByRole('button', { name: '让对话内容消失' }).click();
  await expect(panel).toBeHidden();
  await expect
    .poll(async () => (await canvas.boundingBox())!.width, { message: '收起对话后画布没有变宽' })
    .toBeGreaterThan(openCanvas!.width);

  await page.getByRole('button', { name: '展开对话' }).click();
  await expect(panel).toBeVisible();
  await expect.poll(async () => (await canvas.boundingBox())!.width).toBeCloseTo(openCanvas!.width, 0);

  // 手机宽度下卡片也不能跑出屏幕 —— 溢出到视口外的那部分用户根本点不到。
  await page.setViewportSize({ width: 390, height: 700 });
  const mobile = await panel.boundingBox();
  expect(mobile!.width).toBeLessThanOrEqual(390);
  expect(mobile!.x).toBeGreaterThanOrEqual(0);
  expect(mobile!.y + mobile!.height).toBeLessThanOrEqual(700);
});

test('一处发出的消息，收起对话、换视图之后仍然在同一份列表里', async ({ page }) => {
  const { token } = await registerAccount(page, 'layout-chat');
  const workspaceId = await seedBusyWorkspace(page, token, '对话连续性空间');

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  const cards = page.locator('.floating-conversation .message');
  // 新空间是**一条消息都没有**的(这一点由 `space-bootstrap.spec.ts` 钉着),
  // 所以先自己发两条,卡片才有得量。上一版这段直接读示例空间预置的四条 ——
  // 那是"量一份别人的数据",不是量布局。
  await page.getByLabel('给 AI 的消息').fill('先整理导师资料');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(cards.filter({ hasText: '先整理导师资料' })).toBeVisible();
  await page.getByLabel('给 AI 的消息').fill('再确认一下时间');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(cards.filter({ hasText: '再确认一下时间' })).toBeVisible();

  const bounds = await cards.evaluateAll(elements => elements.map(element => {
    const rect = element.getBoundingClientRect();
    return { top: rect.top, bottom: rect.bottom };
  }));
  expect(bounds.length, '两条消息应该画出至少两张卡片').toBeGreaterThanOrEqual(2);
  for (let i = 1; i < bounds.length; i++) {
    expect(bounds[i].top - bounds[i - 1].bottom, '两张卡片叠在一起了').toBeGreaterThanOrEqual(20);
  }

  // 卡片群整体能用键盘挪位置 —— 拖拽之外的唯一入口。
  const field = page.locator('.floating-conversation');
  const before = await field.boundingBox();
  await page.getByRole('button', { name: '整体移动对话' }).focus();
  await page.getByRole('button', { name: '整体移动对话' }).press('ArrowLeft');
  expect((await field.boundingBox())!.x).toBe(before!.x - 10);

  await page.getByRole('button', { name: '让对话内容消失' }).click();
  await expect(field).toBeHidden();
  await page.getByRole('tab', { name: '时间线', exact: true }).click();
  await expect(page.getByTestId('timeline-view')).toBeVisible();
  await page.getByRole('button', { name: '展开对话' }).click();
  await page.getByLabel('给 AI 的消息').fill('继续讨论');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(cards.filter({ hasText: '继续讨论' })).toBeVisible();
  // 上一条也还在。少了这句,"收起对话"和换视图各清一次消息也照样全绿。
  await expect(cards.filter({ hasText: '先整理导师资料' })).toBeVisible();

  // **真正的判据不是它在屏幕上,是它在后端。** 上面那几条只说明这个组件没有把状态
  // 弄丢;刷新之后还在,才说明这份对话的真相不在浏览器里。
  await page.reload();
  await expect(cards.filter({ hasText: '先整理导师资料' })).toBeVisible();
  await expect(cards.filter({ hasText: '继续讨论' })).toBeVisible();
});
