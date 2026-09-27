import { expect, test } from '@playwright/test';
import {
  API_BASE,
  addDependency,
  addRelation,
  api,
  assertBackendRunning,
  createNode,
  createWorkspace,
  getPlan,
  openSpacePage,
  registerAccount,
  renderedNodeIds,
  waitForRealPlan,
} from './support/session';

/**
 * 收走一个节点,它下面那一整支跟着走 —— **在界面上,也在库里**;而且**能拿回来**。
 *
 * ## 这个文件改过两次,每一次都是因为产品真的变了
 *
 * 第一版的第一条是纯单元测试:拿 `initialGrowth` 那份示例数据调
 * `growthReducer(state, { type: 'DELETE_NODE' })`,断言后代和相连的边都没了。那个
 * reducer 连同示例空间一起删掉了 —— 删子树从"浏览器里改一棵内存树"变成了后端的一次
 * 软删除。单元测试的**对象**没有了,它想钉的两件事还在:删的是**一整支**、边不能悬空。
 *
 * 第二版钉住了那两件事,但"删除"当时是不可逆的:点一下,一整支连同挂在上面的边一起
 * 从库里消失,没有任何地方能拿回来。现在(步骤 3C)默认动作变成了**归档** ——
 * 点垃圾桶之前先告诉你这一下会带走什么,点完之后在「归档」里能把整支原样恢复,
 * 而边**一行都不动**。所以第三条测试的断言跟着反了过来(见它自己的注释),并且
 * 多了一条"归档 → 恢复"的完整闭环。
 *
 * ## 一条重复过的坑
 *
 * 垃圾桶按钮默认 `opacity: 0`,悬停才到 1(`dark-theme.css:252`)。所以断言"默认看不见"
 * 之前**必须先把鼠标挪开** —— 上一条测试或上一次交互把指针留在节点上的话,读到的是
 * 1,而失败信息会指向"按钮的样式坏了"。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('归档前先告诉你这一下会带走什么，之后能把整支恢复回来', async ({ page }) => {
  const { token } = await registerAccount(page, 'archive');
  const workspaceId = await createWorkspace(page, token, '归档恢复验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  // 待归档的一支,**三层**:被点的是最上面那个,所以"只收走它自己"也画得出来。
  const doomed = await createNode(page, token, workspaceId, { parentId: root.id, title: '待收阶段', nodeType: 'stage' });
  const doomedChild = await createNode(page, token, workspaceId, { parentId: doomed, title: '待收子任务', nodeType: 'task' });
  const doomedGrand = await createNode(page, token, workspaceId, { parentId: doomedChild, title: '待收孙任务', nodeType: 'task' });

  // 旁边一支**必须活下来**。少了它,一个"把整个空间清空"的实现也能让下面全绿。
  const kept = await createNode(page, token, workspaceId, { parentId: root.id, title: '留下的阶段', nodeType: 'stage' });
  const keptChild = await createNode(page, token, workspaceId, { parentId: kept, title: '留下的子任务', nodeType: 'task' });

  // 一条前置、一条普通关联,都挂在待收那一支上。确认框里的两个数字就该分别是 1 和 1 ——
  // 断言成不同的数才分得出"关系"和"前置"这两行没有被写反。
  await addDependency(page, token, workspaceId, kept, doomed);
  await addRelation(page, token, workspaceId, kept, doomed);

  const before = await getPlan(page, token, workspaceId);
  expect(before.nodes, '六条:根 + 待收三支 + 留下两支').toHaveLength(6);
  expect(before.dependencies).toHaveLength(1);
  expect(before.relations, '前置那条也在 relations 里,所以是两条').toHaveLength(2);

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  // 顶层这一层画的是根 + 两个阶段;子任务要**进入**各自的阶段才画得出来
  //(这是有意的层级设计,不是丢数据)。所以这里比的是同层集合。
  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id, doomed, kept].sort());

  // --- 点垃圾桶:**先出数字,再出按钮** ----------------------------------------
  await page.mouse.move(2, 2);
  const node = page.locator(`.react-flow__node[data-id="${doomed}"]`);
  const trash = node.locator('.node-delete');
  await expect(trash).toHaveCSS('opacity', '0');
  await node.hover();
  await expect(trash).toHaveCSS('opacity', '1');
  await trash.click();

  const confirm = page.getByRole('dialog');
  await expect(confirm.getByRole('heading')).toHaveText('归档「待收阶段」？');
  // 数字来自 `GET /archive-impact`,由后端现算 —— 界面上不许拿本地那份计划凑。
  const impact = confirm.locator('.archive-impact');
  await expect(impact).toContainText('后代 2 个');
  await expect(impact).toContainText('关系 1 条、前置 1 条');
  // 这一批的全部意义就在这一句上:按下去之前得知道**能拿回来**。
  await expect(confirm).toContainText('可以在「归档」里恢复');
  await expect(confirm).toContainText('原样');

  await confirm.getByRole('button', { name: '归档' }).click();

  // --- 界面:待收那一支整个没了,留下的那一支还在 ------------------------------
  await expect
    .poll(() => renderedNodeIds(page), { message: '归档掉的节点还在画布上' })
    .toEqual([root.id, kept].sort());

  // --- 后端:整棵子树,连同两个后代 --------------------------------------------
  //
  // 只断言画布的话,一个"界面上藏起来、库里还留着"的实现照样全绿。这一条断言的是
  // **画布和后端说的是同一件事**(见 `support/session.ts` 头那段)。
  const after = await getPlan(page, token, workspaceId);
  expect(after.nodes.map((item) => item.id).sort()).toEqual([root.id, kept, keptChild].sort());
  expect(after.nodes.find((item) => item.id === keptChild)!.title).toBe('留下的子任务');
  // 归档是改过计划:版本要往前走,否则"计划在提案生成后被改过"就查不出来。
  expect(after.revisionVersion).toBeGreaterThan(before.revisionVersion);

  // --- 归档之后的那个"还在"的入口 ----------------------------------------------
  //
  // 工具栏那个按钮上的数字是这一支能不能被找回来的唯一线索。数字不对(比如一直是 0),
  // 用户会以为东西没了 —— 而东西还在库里。
  const toolbar = page.locator('.space-floating-tools > button', { hasText: '归档' });
  await expect(toolbar).toContainText('1');
  await toolbar.click();

  const list = page.getByRole('dialog');
  await expect(list.getByRole('heading')).toHaveText('归档恢复验收空间 · 归档');
  // 归档那一下的结果要在**这里**看得到(见 `PathView.tsx` 里那个按钮上的注释)。
  await expect(list.getByRole('status')).toContainText('已归档「待收阶段」及其下面的 2 项');
  const row = list.locator('.archive-list li').filter({ hasText: '待收阶段' });
  await expect(row).toHaveCount(1);
  await expect(row.locator('.archive-row-meta')).toContainText('后代 2 个');

  // --- 恢复:一支、连着的线一起回来 --------------------------------------------
  await row.getByRole('button', { name: '恢复' }).click();
  await expect(list.getByRole('status'), '恢复的结果要如实报出来').toContainText('已恢复「待收阶段」及其下面的 2 项');
  await expect(list.getByRole('status')).toContainText('条线跟着回来了');
  // 列表空了 —— 而且空状态那句话是"还没有归档过东西",不是一片空白。
  await expect(list.locator('.archive-list li')).toHaveCount(0);
  await expect(list.locator('.archive-hint')).toContainText('还没有归档过东西');
  await list.getByRole('button', { name: '关闭弹窗' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);

  // --- 库里那一行真的留着:线回来了,节点也在 ----------------------------------
  //
  // 归档**不动边**(见下面最后一条测试)。所以这里读回来的边就是"库里那行一直在"的
  // 界面证据 —— 恢复不是重新连了一次线,而是把它重新露出来。
  const restored = await getPlan(page, token, workspaceId);
  expect(restored.nodes.map((item) => item.id).sort()).toEqual(
    [root.id, doomed, doomedChild, doomedGrand, kept, keptChild].sort(),
  );
  expect(restored.dependencies, '前置那条回来了').toHaveLength(1);
  expect(restored.relations, '关联那条也回来了').toHaveLength(2);
  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id, doomed, kept].sort());

  // --- 刷新之后还是这个样子:真相在后端,不在浏览器内存里 ----------------------
  await page.reload();
  await waitForRealPlan(page);
  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id, doomed, kept].sort());
});

test('根目标没有归档入口，两种模式下都收不走', async ({ page }) => {
  const { token } = await registerAccount(page, 'archive-root');
  const workspaceId = await createWorkspace(page, token, '根目标保护空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  await createNode(page, token, workspaceId, { parentId: root.id, title: '一个普通节点', nodeType: 'task' });

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 界面上**根本不提供**这个入口,而不是"点了报错"。
  const rootNode = page.locator(`.react-flow__node[data-id="${root.id}"]`);
  await rootNode.hover();
  await expect(rootNode.locator('.node-delete')).toHaveCount(0);
  // 子节点上是有的 —— 少了这句,一个"按钮整个没渲染出来"的实现也照样全绿。
  expect(await page.locator('.node-delete').count()).toBeGreaterThan(0);

  // 接口这一层是最后一道闸:界面藏起来只是界面的事,绕过界面仍然删不掉才算数。
  // 两种模式都试:归档可恢复、彻底删除不可恢复,但"根目标不能没有"与模式无关。
  for (const mode of ['archive', 'delete']) {
    const response = await page.request.delete(
      `${API_BASE}/api/workspaces/${workspaceId}/nodes/${root.id}?mode=${mode}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect(response.status(), `根目标收不走应该是 409,不是 200(mode=${mode})`).toBe(409);
    // 对错误码本身,不只是状态码:409 在这个接口上还可能是别的原因,而"删不掉"和
    // "删不了因为它是个根目标"是两件事。
    expect((await response.json()).error.code).toBe('CANNOT_DELETE_ROOT');
  }

  const plan = await getPlan(page, token, workspaceId);
  expect(plan.nodes.map((item) => item.id)).toContain(root.id);
});

test('上层还在归档里时，那一行明说先恢复上层', async ({ page }) => {
  const { token } = await registerAccount(page, 'archive-blocked');
  const workspaceId = await createWorkspace(page, token, '归档挡住验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  const parent = await createNode(page, token, workspaceId, { parentId: root.id, title: '父阶段', nodeType: 'stage' });
  const child = await createNode(page, token, workspaceId, { parentId: parent, title: '子任务', nodeType: 'task' });

  /*
   * 两次归档走接口 —— **这里验的不是"点垃圾桶会发生什么"**(那是上一条测试的事),
   * 而是"归档列表在一部分东西恢复不了的时候长什么样"。要造出那种状态必须先归档
   * 子任务、再归档父阶段,走界面的话得先进一趟子画布点一次垃圾桶、再退回来点一次,
   * 而这两下验的东西完全相同。
   */
  await api(page, token, `/api/workspaces/${workspaceId}/nodes/${child}?mode=archive`, { method: 'DELETE' });
  await api(page, token, `/api/workspaces/${workspaceId}/nodes/${parent}?mode=archive`, { method: 'DELETE' });

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id]);

  await page.locator('.space-floating-tools > button', { hasText: '归档' }).click();
  const list = page.getByRole('dialog');

  // 两次归档 = 两行(各自那一批的根)。**子任务那一行照样列出来** ——
  // 藏起来会让用户以为它被删了,而他其实还能先恢复上层。
  const rows = list.locator('.archive-list li');
  await expect(rows).toHaveCount(2);
  const parentRow = rows.filter({ hasText: '父阶段' });
  const childRow = rows.filter({ hasText: '子任务' });
  await expect(childRow.locator('.archive-blocked')).toContainText('它的上层还在归档里');
  await expect(childRow.getByRole('button', { name: '恢复' }), '按不动,而不是点了报错').toHaveCount(0);
  await expect(parentRow.locator('.archive-blocked')).toHaveCount(0);

  // 先恢复上层:子任务那一行的按钮**当场**变得可用 —— 挡住它的原因没了。
  await parentRow.getByRole('button', { name: '恢复' }).click();
  await expect(rows).toHaveCount(1);
  await expect(childRow.locator('.archive-blocked')).toHaveCount(0);
  await childRow.getByRole('button', { name: '恢复' }).click();

  await expect(rows).toHaveCount(0);
  const plan = await getPlan(page, token, workspaceId);
  expect(plan.nodes.map((item) => item.id).sort()).toEqual([root.id, parent, child].sort());
  // 恢复**不重建**父子关系,它本来就没断过(`parent_id` 一直是那一列)。
  expect(plan.nodes.find((item) => item.id === child)!.parentId).toBe(parent);
});

test('归档不动库里的边，彻底删除才动', async ({ page }) => {
  /*
   * 为什么这条只能在**接口**这一层验。
   *
   * `GET /plan` 的 `dependencies` 是投影出来的,两端有一端不在的边会被过滤掉
   * (`load_all_dependencies` 只取两端都活着的)。但这也意味着"按 `/plan` 断言边没了"
   * 分不出下面两种情况:
   *
   *   a. 那一行**真的删掉了**;
   *   b. 那一行还在库里,只是被投影挡掉了。
   *
   * 这两种现在**都是对的**,区别只在按的是哪个模式 —— 归档要的正是 b(不然拿什么
   * 恢复),彻底删除要的是 a。所以这里读的是 **DELETE 的响应体**:`removedDependencies`
   * 是执行删除的同一段代码数出来的,它不经过任何投影;而 `restorable` 是这一下之后
   * 还能不能拿回来的**承诺**,界面照着它说话(见 `lib/backend.ts::NodeEditResult`)。
   */
  const { token } = await registerAccount(page, 'archive-edges');
  const workspaceId = await createWorkspace(page, token, '归档边验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  const a = await createNode(page, token, workspaceId, { parentId: root.id, title: '先做的', nodeType: 'task' });
  const b = await createNode(page, token, workspaceId, { parentId: root.id, title: '后做的', nodeType: 'task' });
  await addDependency(page, token, workspaceId, a, b);

  // --- 归档:**一行边都不动**,并且如实说"可以恢复" ----------------------------
  const archived = await api<{ restorable: boolean; removedDependencies: number; deletedCount: number }>(
    page,
    token,
    `/api/workspaces/${workspaceId}/nodes/${b}`,
    { method: 'DELETE' }, // 不传 mode —— 默认就应该是归档
  );
  expect(archived.removedDependencies, '归档不动边:那一行要留着,恢复时它还得回来').toBe(0);
  expect(archived.restorable, '归档完要能拿回来;这一位说 false 的话界面会许一个假承诺').toBe(true);
  expect((await getPlan(page, token, workspaceId)).dependencies, '线从画布上消失').toEqual([]);

  // 恢复回来 —— 那条线**原样**在,因为它从来没被删过。
  await api(page, token, `/api/workspaces/${workspaceId}/nodes/${b}/restore`, { method: 'POST' });
  expect((await getPlan(page, token, workspaceId)).dependencies, '库里的那一行真的留着').toHaveLength(1);

  // --- 彻底删除:边跟着走,而且**不再接受恢复** --------------------------------
  const purged = await api<{ restorable: boolean; removedDependencies: number; deletedCount: number }>(
    page,
    token,
    `/api/workspaces/${workspaceId}/nodes/${b}?mode=delete`,
    { method: 'DELETE' },
  );
  expect(purged.removedDependencies, '彻底删除要真的把边的行删掉').toBe(1);
  expect(purged.restorable).toBe(false);
  expect(purged.deletedCount).toBe(1);

  const refused = await page.request.post(
    `${API_BASE}/api/workspaces/${workspaceId}/nodes/${b}/restore`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  expect(refused.status(), '彻底删除过的东西不该还能恢复').toBe(409);
  expect((await refused.json()).error.code).toBe('NODE_PURGED');
  // 不在归档列表里:那里给的是"能恢复"的东西,而它不能。
  const archiveList = await api<{ node: { id: string } }[]>(
    page, token, `/api/workspaces/${workspaceId}/archive`,
  );
  expect(archiveList.map((entry) => entry.node.id)).not.toContain(b);

  // --- 边的**前驱**那一端 ------------------------------------------------------
  //
  // 后端那句 SQL 是 `predecessor IN (...) OR successor IN (...)`,只验一半的话,
  // 一个只清了一侧的实现在上面那条仍然全绿。
  const c = await createNode(page, token, workspaceId, { parentId: root.id, title: '再先做的', nodeType: 'task' });
  const d = await createNode(page, token, workspaceId, { parentId: root.id, title: '再后做的', nodeType: 'task' });
  await addDependency(page, token, workspaceId, c, d);

  const removedPredecessor = await api<{ removedDependencies: number; deletedCount: number }>(
    page,
    token,
    `/api/workspaces/${workspaceId}/nodes/${c}?mode=delete`,
    { method: 'DELETE' },
  );
  expect(removedPredecessor.removedDependencies, '删掉边的前驱端,那条边也要一起走').toBe(1);
  expect((await getPlan(page, token, workspaceId)).dependencies).toEqual([]);

  // `deletedCount` 说的是"我收走了几条",用户需要知道这一点才不会被吓一跳:
  // 这里收的是叶子,所以是 1,不是一整支。
  expect(removedPredecessor.deletedCount).toBe(1);
});

/**
 * 被归档的节点,不能继续挂在对话的上下文上。
 *
 * ## 这一条钉的是"用户在别处没了这个东西"之后的那一句话
 *
 * 归档不只发生在垃圾桶上:AI 的提案里能提 `delete_node`,用户点确认之后那个节点就
 * 不在计划里了;别的标签页也会归档。而那两种情况下,**本地的选中不会跟着清** ——
 * `select(null)` 只在用户自己点垃圾桶时走(`provider.confirmArchive`)。
 *
 * 于是用户下一次发消息,带上去的是一个已经不存在的节点 id。服务端会拒绝它
 * (404 `NODE_NOT_FOUND`,见 `backend/tests/test_message_context_node.py`),
 * 而用户看到一句和他刚说的话毫无关系的错误 —— 真正的问题在他的画面之外。
 *
 * **判据是"没有错误行",不是"上下文标签不见了"。** 标签在两处都已经会自己消失:
 * 它渲染的是 `growth.nodes[selectedId]`,节点不在计划里它就空了。而发出去的
 * `contextNodeId` 读的是**另一个**东西 —— 那个原始的 `selectedId`。两者分家正是
 * 这个缺陷的形状,所以只断言标签等于什么都没验。
 */
const DOOMED_TITLE = '查文献';
const SURVIVOR_TITLE = '活着的任务';
test('正在讨论的节点在别处被归档之后，下一条消息不带它的 id 上去', async ({ page }) => {
  const { token } = await registerAccount(page, 'archive-context');
  const workspaceId = await createWorkspace(page, token, '归档后续验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  // 被选中、然后从别处归档的那一个。
  const doomed = await createNode(page, token, workspaceId, {
    parentId: root.id, title: DOOMED_TITLE, nodeType: 'task',
  });
  // 旁边活下来一个,用来触发那次计划重拉(见下)。少了它,唯一能点的"完成"
  // 就落在根目标上,而"根目标能不能被标成完成"是另一件事,不该混进这条测试。
  await createNode(page, token, workspaceId, {
    parentId: root.id, title: SURVIVOR_TITLE, nodeType: 'task',
  });

  // 先在**路径**视图上等计划到达:`waitForRealPlan` 读的是画布上真画出来的节点,
  // 而任务视图里没有画布,直接落在那里会一直读到空列表、超时在一句与真正原因无关的
  // 失败信息上。计划到了再切到任务视图。
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await page.getByRole('tab', { name: '任务', exact: true }).click();

  // 点任务行 = 选中它(和画布上点节点是同一个状态)。对话卡片上多出"正在讨论"。
  await page.locator('.task-detail').filter({ hasText: DOOMED_TITLE }).click();
  await expect(page.locator('.floating-conversation .context-chip')).toContainText(DOOMED_TITLE);

  // **走接口归档,不走界面上那个垃圾桶。** 界面上的删除会顺手把选中清掉 ——
  // 那样就正好绕过了这条测试要验的那件事。
  await api(page, token, `/api/workspaces/${workspaceId}/nodes/${doomed}`, { method: 'DELETE' });

  // 触发一次**同一会话内**的计划重拉。任何一次成功的计划写入都会重拉
  // (`mutatePlan` -> `refreshPlan`),而任务行左边那个复选框就是最省事的一次。
  // 复选框的 `aria-label` 是"完成" + **节点标题**(见 TaskView)。
  await page.locator(`.task-check[aria-label="完成${SURVIVOR_TITLE}"]`).click();
  // 等到新计划真的到了才继续:上下文标签消失就是它的可见证据 ——
  // 标签渲染的是 `growth.nodes[selectedId]`,而那一份在新计划里已经没有这个节点了。
  await expect(page.locator('.floating-conversation .context-chip')).toHaveCount(0);

  await page.getByLabel('给 AI 的消息').fill('那先做别的');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();

  const sent = page.locator('.floating-conversation .message').filter({ hasText: '那先做别的' });
  await expect(sent).toBeVisible();
  // `turn-error` 是这条消息失败时**唯一**的落点。404 那条路上它会写着
  // "这个节点不在当前空间里。" —— 而这句话对用户刚说的那句话毫无解释力。
  await expect(page.locator('.floating-conversation .turn-error')).toHaveCount(0);
  // 反过来确认这一轮真跑完了:助手回了一条,而不是停在"发送中"。
  // 新空间一条历史都没有,所以这一轮跑完就该正好两条。
  await expect(page.locator('.floating-conversation .message')).toHaveCount(2);
});
