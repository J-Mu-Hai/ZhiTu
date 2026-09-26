import { expect, test, type Page } from '@playwright/test';
import {
  API_BASE,
  api,
  assertBackendRunning,
  clickUntilVisible,
  createNode,
  createWorkspace,
  getPlan,
  registerAccount,
  renderedNodeIds,
  waitForRealPlan,
  type PlanRelation,
} from './support/session';

/**
 * 画布上手动连关系 —— 三种边、两个入口、看得懂的方向、连完还在。
 *
 * ## 这个文件在验哪一件事
 *
 * 步骤 2 只把关系**存进了库**(`node_relations` 表、四个接口),界面上一根线都画不出来。
 * 所以这一批的要害不是"接口通不通"(后端 396 条测试已经在看着它),而是:
 *
 * 1. **用户自己连得上** —— 拖线和表单两条路,不依赖模型;
 * 2. **三种边在图上分得开** —— 尤其"前置"和"相关":前者会让后续任务排不早于前置,
 *    后者不会。线画得一样等于把这个区别藏起来了;
 * 3. **方向没有被谁偷偷翻过来** —— `source → target` 就是前置 → 后续,全仓一个约定;
 * 4. **连完还在** —— 刷新之后从后端读回来,画布上还是那条线。
 *
 * ## 为什么断言"线长什么样"也要真的读浏览器算出来的样式
 *
 * "虚线、无箭头"是**要求**(任务书 §3.2 与 `docs/09-...HANDOFF.md` 的视觉约定),
 * 不是装饰。断言组件里传了哪个 `type` 只是在断言自己写的代码,所以这里读的是
 * `getComputedStyle` 上的 `stroke-dasharray` 与 `marker-end` —— 浏览器最终画出来
 * 的那两个值。三种类型都画成同一种线的实现过不了。
 *
 * ## 这个文件**不**重复后端已经验过的那些
 *
 * 跨账号、跨空间的越权(`RELATION_NOT_FOUND` / `NODE_NOT_FOUND`)、幂等的库内效果、
 * 布局不写版本账……都在 `backend/tests/test_relations_and_layout.py` 里,各有各的
 * 反向断言。下面只在最后一个用例里确认**界面用的那个接口对越权引用仍然是 404** ——
 * 因为界面根本构造不出这种请求(表单里只列当前这一层的节点),不确认的话,
 * "界面上没有这个入口"很容易被读成"这件事没人验"。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

const dialog = (page: Page) => page.getByRole('dialog');
/** 工具栏上那个按钮。**作用域限定在工具栏里** —— 弹窗里的提交按钮同名。 */
const relationButton = (page: Page) => page.locator('.space-floating-tools button', { hasText: '建立关系' });

async function openRelationForm(page: Page): Promise<void> {
  // 弹窗里的提交按钮也叫「建立关系」,所以触发器必须限定作用域(见上面那个函数)。
  // `clickUntilVisible` 会在效果没出现时再点一次 —— 见它在 `support/session.ts` 里的理由。
  await clickUntilVisible(page, relationButton(page), dialog(page));
}

/**
 * 拖一条线:从 `from` 右侧的圆点拖到 `to` 左侧的圆点。
 *
 * 中间那几帧移动不是装饰。React Flow 的连接状态建立在**指针移动**上,一步跳到终点
 * 的话,它可能只收到最后一次 `pointerup`,于是"拖了但什么都没发生" —— 而这个失败
 * 长得像"手柄选择器写错了"。
 */
async function dragConnect(page: Page, from: string, to: string): Promise<void> {
  const source = page.locator(`.react-flow__node[data-id="${from}"] .react-flow__handle.source`);
  const target = page.locator(`.react-flow__node[data-id="${to}"] .react-flow__handle.target`);
  const fromBox = await source.boundingBox();
  const toBox = await target.boundingBox();
  if (!fromBox || !toBox) throw new Error('拖线失败:找不到某个节点上的连接圆点');
  const start = { x: fromBox.x + fromBox.width / 2, y: fromBox.y + fromBox.height / 2 };
  const end = { x: toBox.x + toBox.width / 2, y: toBox.y + toBox.height / 2 };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  for (const ratio of [0.3, 0.6, 0.85]) {
    await page.mouse.move(start.x + (end.x - start.x) * ratio, start.y + (end.y - start.y) * ratio);
  }
  await page.mouse.move(end.x, end.y);
  await page.mouse.up();
}

/** 这条线在浏览器里**最终画成什么样**。断言的是这几项,不是我们自己传的 props。 */
async function edgeLook(page: Page, relationId: string) {
  return page.locator(`[data-testid="rf__edge-${relationId}"] .react-flow__edge-path`).evaluate((element) => {
    const style = getComputedStyle(element);
    return { dash: style.strokeDasharray, stroke: style.stroke, markerEnd: style.markerEnd };
  });
}

/** 读回后端那几行 —— 画布上"有没有"的真值在这里,不在 DOM 里。 */
async function relations(page: Page, token: string, workspaceId: string): Promise<PlanRelation[]> {
  return (await getPlan(page, token, workspaceId)).relations;
}

/**
 * 点提交,并等到弹窗关掉。
 *
 * "后端收下了"在这一屏上**只有这一个可见信号** —— 弹窗里没有别的成功提示。
 * 少了这一步,后面那句"读回来的关系是一条"就会在请求还在路上时读到空数组,
 * 而失败会指向"关系没存上"(一个假的产品缺陷)。
 */
async function submitRelation(page: Page, label = '建立关系'): Promise<void> {
  await dialog(page).getByRole('button', { name: label }).click();
  await expect(dialog(page)).toHaveCount(0);
}

/** 一根线的中点,换算成屏幕坐标 —— 拿去真点一下它。 */
async function edgeMidpoint(page: Page, relationId: string): Promise<{ x: number; y: number }> {
  return page.locator(`[data-testid="rf__edge-${relationId}"] .react-flow__edge-path`).evaluate((element) => {
    const path = element as SVGPathElement;
    const middle = path.getPointAtLength(path.getTotalLength() / 2);
    const matrix = path.getScreenCTM();
    if (!matrix) throw new Error('这条线不在一个可换算的坐标系里');
    return {
      x: middle.x * matrix.a + middle.y * matrix.c + matrix.e,
      y: middle.x * matrix.b + middle.y * matrix.d + matrix.f,
    };
  });
}

/** 每个用例都要的那三步:一个空间、根目标下面两个节点、打开工作台。 */
async function twoNodes(page: Page, prefix: string, first: string, second: string) {
  const { token } = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, token, '关系验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  const a = await createNode(page, token, workspaceId, { parentId: root.id, title: first });
  const b = await createNode(page, token, workspaceId, { parentId: root.id, title: second });
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  return { token, workspaceId, root: root.id, a, b };
}

test('表单建一条「相关」：线上画得出来，刷新后还在', async ({ page }) => {
  const { token, workspaceId, a, b } = await twoNodes(page, 'relation-form', '文献综述', '开题报告');

  await openRelationForm(page);
  // 默认就是「相关」。**这个默认值是要求的一部分**:把两个东西连起来不等于它们有先后,
  // 而"前置"会真的改变排期结果 —— 系统不能替用户做这个解释。
  await expect(dialog(page).getByLabel('关系类型')).toHaveValue('related_to');
  await dialog(page).getByLabel('起点').selectOption(a);
  await dialog(page).getByLabel('终点').selectOption(b);
  // 建成了才关弹窗 —— 失败了要留着让用户看见原因(下面"成环被拒绝"那条验的就是那一边)。
  await submitRelation(page);

  const rows = await relations(page, token, workspaceId);
  expect(rows).toHaveLength(1);
  expect(rows[0].relationType).toBe('related_to');
  /*
   * 「相关」是**无向**的:库里存哪一头是 `source` 由 UUID 排序决定,不由用户先点谁决定
   * (见 `node_service._endpoints`)。所以这里比对的是**这一对**,不是方向 ——
   * 断言 `sourceId === a` 会是一条看运气红/绿的测试,而且它验的性质根本不是产品要的。
   */
  expect([rows[0].sourceId, rows[0].targetId].sort()).toEqual([a, b].sort());
  expect(rows[0].note).toBeNull();

  const edge = page.locator(`[data-testid="rf__edge-${rows[0].id}"]`);
  await expect(edge, '库里有了,画布上也必须画出来').toHaveCount(1);
  const look = await edgeLook(page, rows[0].id);
  expect(look.dash, '「相关」画成虚线').not.toBe('none');
  expect(look.markerEnd, '「相关」没有箭头 —— 它没有方向可说').toBe('none');

  // 刷新之后还在。这一条才是"关系真的存下来了",内存里那份画得再对也不算数。
  await page.reload();
  await waitForRealPlan(page);
  await expect(page.locator(`[data-testid="rf__edge-${rows[0].id}"]`)).toHaveCount(1);
});

test('拖线默认建「相关」，改成「影响」并写说明，线上看得见那句话', async ({ page }) => {
  const { token, workspaceId, a, b } = await twoNodes(page, 'relation-drag', '文献综述', '开题报告');

  await dragConnect(page, a, b);

  // 拖完立刻开编辑器:拖出来的是哪一种,得让用户当场确认,不能默默落成一种。
  await expect(dialog(page), '拖完线之后关系编辑器应当自己打开').toBeVisible();
  await expect(dialog(page).getByText('无向关联'), '默认这一档是「无向关联」').toBeVisible();

  /*
   * **拖的方向被记住了。** 这一条是这次改出来的:`connectNodes` 原来先 POST 一条
   * `related_to` 再拿返回的行灌编辑器,而「相关」是无向的、后端按 UUID 排序规范化两端,
   * 于是"从 A 拖到 B"这件事在返回的行里可能变成 B → A。用户再改成有向的「影响」,
   * 箭头就反了 —— 一半的运行里看不出来(这条用例第一次红正是撞上那一半)。
   * 现在拖线不写库,只把两个端点预填进表单,所以这里可以**确定地**断言。
   */
  await expect(dialog(page).getByLabel('起点')).toHaveValue(a);
  await expect(dialog(page).getByLabel('终点')).toHaveValue(b);
  // 拖动这一下**还没写库** —— 写入发生在用户点确认的时候(理由见上面那段)。
  expect(await relations(page, token, workspaceId), '拖动本身不该落库').toHaveLength(0);

  await dialog(page).getByLabel('关系类型').selectOption('influences');
  await expect(dialog(page).getByText('影响方向')).toBeVisible();
  await dialog(page).getByLabel('说明').fill('文献结论会影响开题的写法');
  await submitRelation(page);

  const rows = await relations(page, token, workspaceId);
  expect(rows).toHaveLength(1);
  // 「影响」是**有向**的,所以这里方向要断言:拖的方向就是它影响的方向。
  expect(rows[0]).toMatchObject({ relationType: 'influences', sourceId: a, targetId: b, note: '文献结论会影响开题的写法' });

  const look = await edgeLook(page, rows[0].id);
  expect(look.dash, '「影响」也是虚线').not.toBe('none');
  expect(look.markerEnd, '「影响」有箭头 —— 它是有向的').not.toBe('none');
  // 说明直接印在线上:写了"为什么连",图上一眼要看得见,而不是只在编辑器里。
  await expect(page.locator(`[data-testid="rf__edge-${rows[0].id}"]`)).toContainText('文献结论会影响开题的写法');

  await page.reload();
  await waitForRealPlan(page);
  await expect(page.locator(`[data-testid="rf__edge-${rows[0].id}"]`)).toContainText('文献结论会影响开题的写法');
});

test('前置关系写的是「前置 → 后续」，成环当场被拒绝、且不写进去', async ({ page }) => {
  const { token, workspaceId, a, b } = await twoNodes(page, 'relation-cycle', '文献综述', '开题报告');

  await openRelationForm(page);
  await dialog(page).getByLabel('关系类型').selectOption('depends_on');
  // 选了"前置"之后,两个下拉从"起点/终点"变成"前置(先做)/后续(后做)" ——
  // 这一档的两个端点名字不一样,因为它的方向是有语义的。
  await dialog(page).getByLabel('前置（先做）').selectOption(a);
  await dialog(page).getByLabel('后续（后做）').selectOption(b);
  await expect(dialog(page).getByText('前置 → 后续：先做「文献综述」，才轮得到「开题报告」。')).toBeVisible();
  await submitRelation(page);

  const rows = await relations(page, token, workspaceId);
  expect(rows).toHaveLength(1);
  // **方向没有被反转。** 后端存的就是 `source → target` = 前置 → 后续,投影层不再翻一次
  // (见 `planProjection.ts` 里那段注释)。这条断言在钉住那个全仓唯一的约定。
  expect(rows[0]).toMatchObject({ relationType: 'depends_on', sourceId: a, targetId: b });

  // 反着再连一条 → 成环。后端 409,界面必须**把它说出来**,而不是"点了没反应"。
  await openRelationForm(page);
  await dialog(page).getByLabel('关系类型').selectOption('depends_on');
  await dialog(page).getByLabel('前置（先做）').selectOption(b);
  await dialog(page).getByLabel('后续（后做）').selectOption(a);
  await dialog(page).getByRole('button', { name: '建立关系' }).click();

  await expect(dialog(page), '失败了就不该关弹窗').toBeVisible();
  await expect(dialog(page).getByRole('alert'), '环的原因必须写在这里').toContainText('环');
  // 库里还是那一条:被拒绝的请求什么都没写。
  expect(await relations(page, token, workspaceId)).toHaveLength(1);

  // 关掉之后重开,错误不该粘在下一次上 —— 否则用户会以为新的一次也失败了。
  await dialog(page).getByRole('button', { name: '关闭弹窗' }).click();
  await expect(dialog(page)).toHaveCount(0);
  await openRelationForm(page);
  await expect(dialog(page).getByRole('alert')).toHaveCount(0);
});

test('同一对节点连两次是幂等的：不报错，也不多出一条线', async ({ page }) => {
  const { token, workspaceId, a, b } = await twoNodes(page, 'relation-idempotent', '文献综述', '开题报告');

  // 第一次 A → B。
  await openRelationForm(page);
  await dialog(page).getByLabel('起点').selectOption(a);
  await dialog(page).getByLabel('终点').selectOption(b);
  await submitRelation(page);

  // 第二次**反着连**。无向关系里这和上一次是同一条边 —— 不报错,也不该多出一行。
  await openRelationForm(page);
  await dialog(page).getByLabel('起点').selectOption(b);
  await dialog(page).getByLabel('终点').selectOption(a);
  // 幂等意味着这一下**不报错**,所以弹窗照常关掉。
  await submitRelation(page);

  const rows = await relations(page, token, workspaceId);
  expect(rows, '反着连一次不该变成两条线').toHaveLength(1);
  // 画布上也只多出那一条:父子连线有两条(root -> A、root -> B),关系线一条。
  // 少数一次就说明"幂等"是真的幂等,不是"库里没多、图上多了一条"。
  await expect(page.locator(`[data-testid="rf__edge-${rows[0].id}"]`)).toHaveCount(1);
  await expect(page.locator('.react-flow__edge')).toHaveCount(3);
});

test('点线能打开编辑器，删掉关系之后两端节点一个不少', async ({ page }) => {
  const { token, workspaceId, root, a, b } = await twoNodes(page, 'relation-delete', '文献综述', '开题报告');

  await openRelationForm(page);
  await dialog(page).getByLabel('起点').selectOption(a);
  await dialog(page).getByLabel('终点').selectOption(b);
  await submitRelation(page);
  const [edge] = await relations(page, token, workspaceId);

  // 点线上**真正的那一点**。线只有一两像素宽,按包围盒中心点会点到空白处 ——
  // 于是"点不中"长得像"这个功能没做"。
  const point = await edgeMidpoint(page, edge.id);
  await page.mouse.click(point.x, point.y);

  await expect(dialog(page), '点线应当打开关系编辑器').toBeVisible();
  await expect(dialog(page).getByText('编辑关系')).toBeVisible();
  await expect(dialog(page).getByText('无向关联')).toBeVisible();
  await dialog(page).getByRole('button', { name: '删除这条关系' }).click();
  await expect(dialog(page)).toHaveCount(0);

  expect(await relations(page, token, workspaceId), '线要真的从库里删掉').toHaveLength(0);
  await expect(page.locator(`[data-testid="rf__edge-${edge.id}"]`)).toHaveCount(0);
  // **删边不删点**:三个节点一个都不能少,否则"删掉一条线"会顺手毁掉两个任务。
  await expect.poll(() => renderedNodeIds(page)).toEqual([root, a, b].sort());
});

test('界面构造不出跨空间的引用，接口这一层也仍然是 404', async ({ page }) => {
  const { token, workspaceId, a } = await twoNodes(page, 'relation-tenant', '文献综述', '开题报告');

  // 界面上不可能选到别的空间里的节点:表单只列当前这一层的节点(`relationCandidates`)。
  await openRelationForm(page);
  const options = await dialog(page).getByLabel('起点').locator('option').allTextContents();
  // 列出来的**正好是这一层的三个节点 + 一个空的"请选择"**,一个不多一个不少。
  // 比的是集合不是顺序:顺序跟着画布布局走,钉住它只会让这条测试在挪一下节点之后
  // 因为一个和"越权"无关的理由变红。
  expect([...options].sort()).toEqual(['请选择…', '关系验收空间', '文献综述', '开题报告'].sort());
  await dialog(page).getByRole('button', { name: '关闭弹窗' }).click();

  // 绕过界面直接发一次:同一个用户的**另一个空间**里的节点,连不上。
  // 更深的那些(别人的账号、别的空间的边 id)在
  // `backend/tests/test_relations_and_layout.py` 里 —— 这里只确认界面背后的那道闸还在。
  const other = await createWorkspace(page, token, '另一个空间');
  const stranger = (await getPlan(page, token, other)).nodes[0];
  const response = await page.request.post(`${API_BASE}/api/workspaces/${workspaceId}/relations`, {
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: { sourceId: a, targetId: stranger.id, relationType: 'related_to' },
  });
  expect(response.status(), '别的空间里的节点不该连得上').toBe(404);
  expect((await response.json()).error.code).toBe('NODE_NOT_FOUND');
  expect(await relations(page, token, workspaceId)).toHaveLength(0);
});

test('关系不会因为切一次视图就消失', async ({ page }) => {
  const { token, workspaceId, a, b } = await twoNodes(page, 'relation-tabs', '文献综述', '开题报告');

  await openRelationForm(page);
  await dialog(page).getByLabel('起点').selectOption(a);
  await dialog(page).getByLabel('终点').selectOption(b);
  await submitRelation(page);
  const [edge] = await relations(page, token, workspaceId);

  // 工作台的四个视图是一个三元表达式:切页签会**整棵卸载重建**画布子树
  // (见 `docs/10-NEXT-BATCH-SCOPE.md` 第 4 节)。线是从 `/plan` 投影出来的,
  // 重建之后必须自己回来 —— 而"它没回来"看起来会像关系没存上。
  await page.getByRole('tab', { name: '时间线', exact: true }).click();
  await expect(page.locator('.react-flow')).toHaveCount(0);
  await page.getByRole('tab', { name: '路径', exact: true }).click();
  await waitForRealPlan(page);
  await expect(page.locator(`[data-testid="rf__edge-${edge.id}"]`)).toHaveCount(1);

  // 顺带一提,**新建关系的草稿**也跟着同一套规则:切视图不丢,只有明确关掉才丢。
  // 这里不开那个弹窗,是因为 `canvas-stability.spec.ts` 已经专门在验草稿那一组。
  expect(await relations(page, token, workspaceId)).toHaveLength(1);
});

test('接口能改说明、也能在「相关」与「影响」之间换类型，换成「前置」被拒绝', async ({ page }) => {
  const { token, workspaceId, a, b } = await twoNodes(page, 'relation-api', '文献综述', '开题报告');

  const created = await api<PlanRelation>(page, token, `/api/workspaces/${workspaceId}/relations`, {
    method: 'POST',
    data: { sourceId: a, targetId: b, relationType: 'related_to' },
  });
  const renamed = await api<PlanRelation>(page, token, `/api/workspaces/${workspaceId}/relations/${created.id}`, {
    method: 'PATCH',
    data: { relationType: 'influences', note: '一起推' },
  });
  expect(renamed).toMatchObject({ relationType: 'influences', note: '一起推' });

  // 换成 `depends_on` 是**跨表**的(`dependencies` 没有说明列,搬过去用户写的解释就没了),
  // 这一版拒绝并给出原因。界面上的对应表现是那个选项在编辑态被禁用 —— 这里验的是
  // 禁用背后的那道闸真的在,而不只是把界面画灰。
  const refused = await page.request.patch(
    `${API_BASE}/api/workspaces/${workspaceId}/relations/${created.id}`,
    {
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      data: { relationType: 'depends_on' },
    },
  );
  expect(refused.status()).toBe(400);
  expect((await refused.json()).error.code).toBe('RELATION_TYPE_CHANGE_UNSUPPORTED');
  // 被拒绝之后那条边原样还在:类型还是「影响」,说明也没被清掉。
  expect((await relations(page, token, workspaceId))[0]).toMatchObject({ relationType: 'influences', note: '一起推' });
});

test('后端收不下这条线的时候:原因是中文的、弹窗不关、一个字都不写进去,重试能成', async ({ page }) => {
  const { token, workspaceId, a, b } = await twoNodes(page, 'relation-offline', '文献综述', '开题报告');

  /*
   * 只数**关系线**,不数全部的线。
   *
   * 画布上本来就有线:根目标到这两个节点各挂着一条父子线。所以"画布上没有线"是错的断言
   * ——这条用例头两次红都栽在这里:`[data-testid^="rf__edge-"]` 先数出 2(那 2 条是**正确**的
   * 父子线),改成"和之前一样多"之后又数出 0 —— 因为量的时候那两条父子线**还没画出来**,
   * 量具本身在动。React Flow 会给每条边加上 `react-flow__edge-<类型>` 这个类,而关系边的
   * 类型就是 `relation`(见 `edgeTypes`),按类型取就不受父子线什么时候渲染的影响。
   */
  const relationEdges = page.locator('.react-flow__edge-relation');
  await expect(relationEdges, '这条测试的前提:此刻还没有任何关系线').toHaveCount(0);

  await openRelationForm(page);
  await dialog(page).getByLabel('起点').selectOption(a);
  await dialog(page).getByLabel('终点').selectOption(b);
  await dialog(page).getByLabel('说明').fill('这条说明不能因为一次失败就没了');

  // **只掐断建关系那一条 POST**,别的一律放行:整页断网的话,连"计划读回来了"
  // 都不成立,失败会指向另一个原因(见 `support/session.ts` 里 `waitForRealPlan` 那段)。
  await page.route('**/api/workspaces/*/relations', (route) =>
    route.request().method() === 'POST' ? route.abort('connectionfailed') : route.continue(),
  );

  await dialog(page).getByRole('button', { name: '建立关系' }).click();

  // 1. 原因**看得见**,而且是句人话。`ApiError` 对连不上后端专门造了一条
  //    (`lib/api.ts`),不是把 `TypeError: Failed to fetch` 端给用户。
  const alert = dialog(page).getByRole('alert');
  await expect(alert, '保存失败必须在这块弹窗里说出来,不能只写进控制台').toBeVisible();
  await expect(alert).toContainText('连不上后端服务');

  // 2. 弹窗**还开着**,而且用户填过的东西原样在。关掉弹窗等于把一次失败变成一次
  //    "我填的那些去哪了" —— 重试要从头再填一遍,这是最容易被写成"看着也能用"的一处。
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page).getByLabel('说明')).toHaveValue('这条说明不能因为一次失败就没了');
  await expect(dialog(page).getByLabel('起点')).toHaveValue(a);

  // 3. 库里**一行都没有**,画布上也没多出一条线。界面上那句红字要是和库对不上
  //    (比如先乐观画上去再报错),下次刷新这条"没存上"的线就会自己冒出来。
  expect(await relations(page, token, workspaceId)).toHaveLength(0);
  await expect(relationEdges, '失败的那一次在画布上留下了一条线').toHaveCount(0);

  // 4. 网络回来之后**再点一次就能成** —— 失败之后这条路要留着,不然"重试"是句空话。
  await page.unroute('**/api/workspaces/*/relations');
  await submitRelation(page);
  const rows = await relations(page, token, workspaceId);
  expect(rows).toHaveLength(1);
  expect(rows[0].note).toBe('这条说明不能因为一次失败就没了');
  await expect(relationEdges, '重试成功之后那条线要出现').toHaveCount(1);
});
