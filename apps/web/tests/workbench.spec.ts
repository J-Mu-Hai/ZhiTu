import { test, expect } from '@playwright/test';
import {
  assertBackendRunning,
  createNode,
  createWorkspace,
  dayOffset,
  enterSpace,
  getPlan,
  openSpacePage,
  registerAccount,
  renderedNodeIds,
  waitForRealPlan,
} from './support/session';

/**
 * "一个状态、多个视图"的总验收。
 *
 * 同一份计划在**路径、时间线、任务**三个视图里必须是同一份:在任务视图里勾掉一件事,
 * 路径视图上的那个节点要变成已完成;换个页面走一圈回来,它还得是已完成。判据不是
 * "屏幕上看着对",而是 `GET /plan` 里那一行 —— 界面上的乐观更新可以骗过眼睛,
 * 骗不过下一份载荷。
 *
 * ## 上一版这条测试走的是示例空间,它验的有一半已经不存在了
 *
 * 旧版走 `?workspace=primary` 那份保研演示数据,后半段干的是:在时间线上把「科研项目」
 * 拖 45 天 → 出现"查看影响"和预览幽灵 → 点"接受调整" → 路径视图认下这个改动。
 *
 * 那套东西整个删了,而且删得有理由:它改的是一场真实存在的安排,而后端**根本没有**
 * `startDate` 这个字段 —— 拖一下改到的是 `deadline`(截止时间),界面上的说辞却是
 * "调整了安排"。所以这一版把它反过来钉:**拖卡片不会改动任何东西**,连同版本号
 * 一起(mock 的乐观更新会推版本号,真的一笔写入也会)。
 *
 * 三个视图仍然是同一份计划 —— 这件事一个字没改,只是现在由测试自己通过接口搭出
 * 那棵树,而不是借一份别人的数据。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('同一份计划在路径、时间线、任务三个视图里是同一份，换个页面回来还在', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));

  const { token } = await registerAccount(page, 'workbench');
  const workspaceId = await createWorkspace(page, token, '多视图验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  const stageId = await createNode(page, token, workspaceId, { parentId: root.id, title: '科研能力', nodeType: 'stage' });
  const project = await createNode(page, token, workspaceId, { parentId: stageId, title: '科研项目', nodeType: 'task', deadline: dayOffset(30) });
  const mentor = await createNode(page, token, workspaceId, { parentId: stageId, title: '联系导师', nodeType: 'task' });

  // 落点是"成长空间"列表,不是工作台 —— 刚注册的账户一个空间都没有。
  await page.goto('/');
  await expect(page).toHaveURL(/\/spaces$/);

  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);

  // --- 第一层:根 + 阶段。任务属于阶段自己的空间,不画在这一层 -------------------
  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id, stageId].sort());

  // --- 进阶段:两级画布是同一份计划的两个窗口,不是两份数据 ---------------------
  //
  // 走节点右上角那个箭头进去 —— **不是单击**:单击现在是"打开正文与详情",那个弹窗
  // 会盖在画布上,后面点视图页签会被它截住(双击那条路在步骤 4 已经拆掉了)。
  await enterSpace(page, stageId);
  await expect.poll(() => renderedNodeIds(page)).toEqual([stageId, project, mentor].sort());

  // 对话的上下文跟着当前所在的这一层走 —— 这是"画布上下文"那件事的用户可见形态。
  await expect(page.getByRole('textbox', { name: '给 AI 的消息' }))
    .toHaveAttribute('placeholder', '关于「科研能力」，告诉 AI 你的想法……');

  // --- 时间线:同一件事画在时间上 -----------------------------------------------
  await page.getByRole('tab', { name: '时间线', exact: true }).click();
  const canvas = page.getByTestId('timeline-canvas');
  await expect(canvas).toHaveAttribute('data-ready', 'true');
  const card = page.locator(`[data-timeline-item="${project}"] [data-timeline-card]`);
  await expect(card).toBeVisible();
  const before = await getPlan(page, token, workspaceId);
  const startDate = await page.locator(`[data-timeline-item="${project}"]`).getAttribute('data-start-date');
  expect(startDate).toBe(before.nodes.find(item => item.id === project)!.deadline);

  /*
   * 把卡片拖走 —— **它不该动,也不该写出任何东西**。
   *
   * 拖多远是不重要的(所以这里不是"45 天"那种有含义的距离):现在这条路上根本没有
   * 写入。上一步的 `beginItem` 只做一件事 —— 选中。要做的是"改安排请走排期",
   * 而后端没有"把开始日期推几天"这个字段,唯一能被悄悄改掉的只有 `deadline`。
   */
  const box = await card.boundingBox();
  if (!box) throw new Error('时间线上没有这张卡片');
  await page.mouse.move(box.x + 12, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 12 + 120, box.y + box.height / 2, { steps: 12 });
  await page.mouse.up();
  await expect(page.locator(`[data-timeline-item="${project}"]`)).toHaveAttribute('data-start-date', startDate!);
  const afterDrag = await getPlan(page, token, workspaceId);
  expect(afterDrag.revisionVersion, '拖动不该产生一笔计划改动').toBe(before.revisionVersion);
  expect(afterDrag.nodes.find(item => item.id === project)!.deadline).toBe(startDate);

  // --- 任务视图:勾掉一件事,三个视图都得认 --------------------------------------
  await page.getByRole('tab', { name: '任务', exact: true }).click();
  await expect(page.locator('.task-detail').filter({ hasText: '科研项目' })).toBeVisible();
  await page.getByRole('button', { name: '完成科研项目', exact: true }).click();
  // 真值先落到后端 —— 界面这一层是乐观更新,它变绿不代表写成功了。
  await expect
    .poll(async () => (await getPlan(page, token, workspaceId)).nodes.find(item => item.id === project)?.status)
    .toBe('completed');

  await page.getByRole('tab', { name: '路径', exact: true }).click();
  await expect(page.locator(`.react-flow__node[data-id="${project}"] .growth-node`)).toHaveClass(/is-complete/);

  // --- 在画布上说话 ---------------------------------------------------------------
  await page.getByRole('textbox', { name: '给 AI 的消息' }).fill('我想先整理导师资料');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  // 断言的是**用户自己发的那条**:没有模型 key 时回复是降级的,拿回复当判据等于
  // 把"模型这次答得怎么样"混进"消息存没存下来"里 —— 那是两件事。
  await expect(page.locator('.floating-conversation .message').filter({ hasText: '我想先整理导师资料' })).toBeVisible();

  // --- 跨页走一圈再回来 -----------------------------------------------------------
  //
  // 显式带上 `?workspace=`:那六个导航链接指向的是裸地址,靠一个本地键找回上下文。
  // 这一条验的是"页面之间共享同一份状态",不是导航链接的拼写。
  for (const name of ['today', 'journal', 'conversations', 'me']) {
    await openSpacePage(page, `/${name}`, workspaceId);
    await expect(page.locator('.editorial-page, .conversation-hub')).toBeVisible();
  }

  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  // 整页重载之后回到的是根那一层(子空间是组件内部的状态,不在地址里),
  // 所以再进一次阶段 —— 而里面的完成状态是从后端读回来的。
  await enterSpace(page, stageId);
  await expect(page.locator(`.react-flow__node[data-id="${project}"] .growth-node`)).toHaveClass(/is-complete/);
  await expect(page.locator('.floating-conversation .message').filter({ hasText: '我想先整理导师资料' })).toBeVisible();

  // 刚才那两页也得还在(它们和这一页共享同一个空间)。
  await openSpacePage(page, '/conversations', workspaceId);
  await expect(page.locator('.hub-thread .message').filter({ hasText: '我想先整理导师资料' })).toBeVisible();

  expect(errors).toEqual([]);
});
