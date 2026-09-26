import { expect, test } from '@playwright/test';
import {
  API_BASE,
  addDependency,
  api,
  assertBackendRunning,
  createNode,
  createWorkspace,
  getPlan,
  registerAccount,
  renderedNodeIds,
  waitForRealPlan,
} from './support/session';

/**
 * 删掉一个节点,它下面那一整支要跟着走 —— **在界面上,也在库里**。
 *
 * ## 这个文件原来是两条测试,现在只剩它们的意图
 *
 * 上一版的第一条是纯粹的单元测试:拿 `initialGrowth` 那份示例数据,调
 * `growthReducer(state, { type: 'DELETE_NODE' })`,断言后代和相连的边都没了。
 * 那个 reducer 连同示例空间一起删掉了 —— 删子树这件事从"浏览器里改一棵内存树"
 * 变成了**后端的一次软删除**(见 `services/node_service.py:delete_node`)。
 * 单元测试的**对象**没有了,但它想钉的两件事还在,而且现在更要紧:
 *
 * 1. 删的是**一整支**,不是被点的那一个;
 * 2. 挂在被删节点上的**边**不能留下悬空的。
 *
 * 第二条在下面单独立了一条,原因写在它自己的注释里。
 *
 * ## 一条重复过的坑
 *
 * 删除按钮默认 `opacity: 0`,悬停才到 1(`dark-theme.css:252`)。所以断言"默认看不见"
 * 之前**必须先把鼠标挪开** —— 上一条测试或上一次交互把指针留在节点上的话,读到的是
 * 1,而失败信息会指向"删除按钮的样式坏了"。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('删掉一个节点，它下面那一整支从界面和后端一起消失', async ({ page }) => {
  const { token } = await registerAccount(page, 'delete');
  const workspaceId = await createWorkspace(page, token, '删除验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  // 待删的一支,**三层**:被点的是最上面那个,所以"只删它自己"也画得出来。
  const doomed = await createNode(page, token, workspaceId, { parentId: root.id, title: '待删阶段', nodeType: 'stage' });
  const doomedChild = await createNode(page, token, workspaceId, { parentId: doomed, title: '待删子任务', nodeType: 'task' });
  const doomedGrand = await createNode(page, token, workspaceId, { parentId: doomedChild, title: '待删孙任务', nodeType: 'task' });

  // 旁边一支**必须活下来**。少了它,一个"把整个空间清空"的实现也能让下面全绿。
  const kept = await createNode(page, token, workspaceId, { parentId: root.id, title: '留下的阶段', nodeType: 'stage' });
  const keptChild = await createNode(page, token, workspaceId, { parentId: kept, title: '留下的子任务', nodeType: 'task' });

  // 一条挂在待删节点上的边:删完这里应该一条都不剩(见第三条测试)。
  await addDependency(page, token, workspaceId, kept, doomed);
  const before = await getPlan(page, token, workspaceId);
  expect(before.nodes, '六条:根 + 待删三支 + 留下两支').toHaveLength(6);
  expect(before.dependencies, '预先埋一条边').toHaveLength(1);

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 顶层这一层画的是根 + 两个阶段;子任务要**进入**各自的阶段才画得出来
  // (这是有意的层级设计,不是丢数据)。所以这里比的是同层集合。
  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id, doomed, kept].sort());

  // --- 删除入口:默认藏着,悬停才出来 ------------------------------------------
  await page.mouse.move(2, 2);
  const node = page.locator(`.react-flow__node[data-id="${doomed}"]`);
  const trash = node.locator('.node-delete');
  await expect(trash).toHaveCSS('opacity', '0');
  await node.hover();
  await expect(trash).toHaveCSS('opacity', '1');
  await trash.click();

  // --- 界面:待删那一支整个没了,留下的那一支还在 ------------------------------
  await expect
    .poll(() => renderedNodeIds(page), { message: '删掉的节点还在画布上' })
    .toEqual([root.id, kept].sort());

  // --- 后端:整棵子树,连同三个后代 --------------------------------------------
  //
  // 这一条才是重点。只断言画布的话,一个"界面上藏起来、库里还留着"的实现照样全绿 ——
  // 而那种节点用户永远找不回来,它会一直占着这个空间。
  const after = await getPlan(page, token, workspaceId);
  const survivors = after.nodes.map((item) => item.id).sort();
  expect(survivors).toEqual([root.id, kept, keptChild].sort());
  for (const id of [doomed, doomedChild, doomedGrand]) {
    expect(after.nodes.find((item) => item.id === id), '被删的那一支必须真的没了').toBeUndefined();
  }
  expect(after.nodes.find((item) => item.id === keptChild)!.title).toBe('留下的子任务');

  // 删了东西就是改过计划:版本要往前走,否则"计划在提案生成后被改过"就查不出来。
  expect(after.revisionVersion).toBeGreaterThan(before.revisionVersion);

  // --- 刷新之后还是这个样子:真相在后端,不在浏览器内存里 ----------------------
  await page.reload();
  await waitForRealPlan(page);
  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id, kept].sort());
});

test('根目标没有删除入口，接口上也删不掉', async ({ page }) => {
  const { token } = await registerAccount(page, 'delete-root');
  const workspaceId = await createWorkspace(page, token, '根目标保护空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  await createNode(page, token, workspaceId, { parentId: root.id, title: '一个普通节点', nodeType: 'task' });

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 界面上**根本不提供**这个入口,而不是"点了报错"。
  const rootNode = page.locator(`.react-flow__node[data-id="${root.id}"]`);
  await rootNode.hover();
  await expect(rootNode.locator('.node-delete')).toHaveCount(0);
  // 子节点上是有的 —— 少了这句,一个"删除按钮整个没渲染出来"的实现也照样全绿。
  expect(await page.locator('.node-delete').count()).toBeGreaterThan(0);

  // 接口这一层是最后一道闸:界面藏起来只是界面的事,绕过界面仍然删不掉才算数。
  const response = await page.request.delete(`${API_BASE}/api/workspaces/${workspaceId}/nodes/${root.id}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.status(), '根目标删不掉应该是 409,不是 200').toBe(409);
  // 错误体是 `{"error": {"code", "message", "details"}}`(见 `api/errors.py`)。
  // 对错误码本身,而不只是状态码:409 在这个接口上还可能是别的原因,
  // 而"删不掉"和"删不了因为它是个根目标"是两件事。
  expect((await response.json()).error.code).toBe('CANNOT_DELETE_ROOT');

  const plan = await getPlan(page, token, workspaceId);
  expect(plan.nodes.map((item) => item.id)).toContain(root.id);
});

test('删掉的节点不会留下悬空依赖', async ({ page }) => {
  /*
   * 为什么这条只能在**接口**这一层验。
   *
   * `GET /plan` 的 `dependencies` 是投影出来的,库里的死边(指向已软删除节点的那种)
   * 会被过滤掉 —— 这是有意的,免得界面上画出一条指向空气的线。但这也意味着
   * "按 `/plan` 断言边没了"分不出下面两种情况:
   *
   *   a. 那一行**真的删掉了**;
   *   b. 那一行还在库里,只是被投影挡掉了。
   *
   * b 是更糟的那种:它安静地躺着,下次有人写"按节点查边"的接口或写复盘统计时冒出来,
   * 而那时候已经没人记得它是怎么来的了。所以这里读的是 **DELETE 的响应体** ——
   * `removedDependencies` 是执行删除的同一段代码数出来的,它不经过任何投影。
   */
  const { token } = await registerAccount(page, 'delete-edges');
  const workspaceId = await createWorkspace(page, token, '悬空边验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  const a = await createNode(page, token, workspaceId, { parentId: root.id, title: '先做的', nodeType: 'task' });
  const b = await createNode(page, token, workspaceId, { parentId: root.id, title: '后做的', nodeType: 'task' });
  await addDependency(page, token, workspaceId, a, b);

  // 删**后继**那一端。
  const removedSuccessor = await api<{ removedDependencies: number }>(
    page,
    token,
    `/api/workspaces/${workspaceId}/nodes/${b}`,
    { method: 'DELETE' },
  );
  expect(removedSuccessor.removedDependencies, '删掉边的后继端,那条边要一起走').toBe(1);
  expect((await getPlan(page, token, workspaceId)).dependencies).toEqual([]);

  // 删**前驱**那一端。后端那句 SQL 是 `predecessor IN (...) OR successor IN (...)`,
  // 只验一半的话,一个只清了一侧的实现在上面那条仍然全绿。
  const c = await createNode(page, token, workspaceId, { parentId: root.id, title: '再先做的', nodeType: 'task' });
  const d = await createNode(page, token, workspaceId, { parentId: root.id, title: '再后做的', nodeType: 'task' });
  await addDependency(page, token, workspaceId, c, d);

  const removedPredecessor = await api<{ removedDependencies: number }>(
    page,
    token,
    `/api/workspaces/${workspaceId}/nodes/${c}`,
    { method: 'DELETE' },
  );
  expect(removedPredecessor.removedDependencies, '删掉边的前驱端,那条边也要一起走').toBe(1);
  expect((await getPlan(page, token, workspaceId)).dependencies).toEqual([]);

  // `deletedCount` 说的是"我删了几条",用户需要知道这一点才不会被吓一跳:
  // 这里删的是叶子,所以是 1,不是一整支。
  expect(removedPredecessor).toMatchObject({ deletedCount: 1 });
});
