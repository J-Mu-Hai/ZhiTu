/**
 * 节点正文与单击语义(步骤 4)。
 *
 * ## 这一组在防什么
 *
 * 三件事以前都不成立,而每一件都是**静默**的:
 *
 * 1. **单击要等 240 毫秒才开编辑器。** 因为"单击看详情"和"双击进子空间"挤在同一个
 *    手势上,只能靠一个计时器分开(`PathView.tsx` 里删掉的那一段 `pendingOpen`)。
 *    代价是每一次查看正文都要干等,而双击会不会被认出来取决于手速。
 * 2. **正文只能靠那个"保存节点"按钮存。** 它曾和标题、优先级、工时挤在同一条 PATCH 里,
 *    关掉弹窗会把没提交的正文连同草稿一起丢掉,而屏幕上没有任何东西说过这件事。
 * 3. **两个人同写一段正文,后保存的整段吃掉先保存的,两边都显示"保存成功"。**
 *    `contentVersion` 这一列和 `CONCURRENCY_CONFLICT` 这个错误码一直都在,
 *    但**写侧从来没用过**(见 `backend/tests/test_node_content_version.py` 的文件头)。
 *
 * ## 断言分三层,缺一层就会自欺
 *
 * - **界面**:保存状态那一行(`.body-save-note`)写的是"已保存"还是"没保存上"。
 * - **接口**:`GET /plan` 里那条节点此刻的正文与版本号 —— 界面说存上了,库里就得真有。
 * - **另一个浏览器上下文**:空 localStorage 里重新读一遍。只看同一个页面会把自己骗
 *   过去:屏幕上那行字可能只是本地草稿,库里一个字都没变。
 *
 * 反过来说,**"存不上"也要验接口**。只断言界面显示了失败,不验库里的旧值还在,
 * 就分不清"写失败了"和"写成功了但界面显示失败"。
 *
 * ## 两条用例是**故意造出来的坏情况**
 *
 * 存不上、别人先改了 —— 都不是正常路径,只能靠拦截请求或绕过界面直接改库造出来。
 * 它们是这一组里最重要的两条:正常路径绿了,不代表这两种情况下界面对用户说的是实话。
 */

import { expect, test, type Browser, type Locator, type Page } from '@playwright/test';
import {
  API_BASE,
  assertBackendRunning,
  createNode,
  createWorkspace,
  enterSpace,
  getPlan,
  openSpacePage,
  registerAccount,
  renderedNodeIds,
  TOKEN_KEY,
  waitForRealPlan,
  type TestAccount,
} from './support/session';

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
});

/**
 * 一个最小场景:根 → 一个阶段 → 一片叶子。
 *
 * 三层是必要的:要验"单击不会顺带进子空间",画布上就得有一个**能进去的**节点;
 * 要验"改了子节点再回上层",就得先能进去。
 *
 * 叶子带一个预计工时 —— 正文保存**只能动正文那两列**,它要是顺手把别的字段抹了,
 * "没有重复工时"这句就是空话,而那种破坏在界面上要等到排期那天才看得出来。
 */
async function scene(page: Page, prefix: string) {
  const account = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, account.token, '正文验收空间', '验收正文的自动保存与冲突处理');
  const rootId = (await getPlan(page, account.token, workspaceId)).nodes[0].id;
  const stageId = await createNode(page, account.token, workspaceId, {
    parentId: rootId,
    title: '一个阶段',
    nodeType: 'stage',
  });
  const leafId = await createNode(page, account.token, workspaceId, {
    parentId: stageId,
    title: '要写正文的叶子',
    nodeType: 'task',
    estimateMinutes: 90,
  });
  return { account, workspaceId, rootId, stageId, leafId };
}

/** 画布上某个节点的卡片。 */
function card(page: Page, nodeId: string) {
  return page.locator(`.react-flow__node[data-id="${nodeId}"]`);
}

/**
 * 打开节点的正文编辑器。
 *
 * **一次单击,不双击。** 这正是步骤 4 改掉的那件事 —— 双击在这个产品里已经不是入口了,
 * 测试里留着它等于测一条用户走不了的路(见 `support/session.ts::enterSpace`)。
 */
async function openEditor(page: Page, nodeId: string) {
  await card(page, nodeId).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('heading')).toContainText('编辑节点');
  return dialog;
}

/** 弹窗里那个正文字段。用它的 label 认,不用"第几个 textarea" —— 顺序变了就该红。 */
const bodyField = (dialog: Locator) => dialog.getByLabel('详细说明');

/** 保存状态那一行。 */
const saveNote = (page: Page) => page.locator('.body-save-note');

/** 等"已保存"真的出现。它只在**服务端确认之后**才会写上去(见 `PathView.tsx::flushBody`)。 */
async function expectSaved(page: Page): Promise<void> {
  await expect(saveNote(page)).toHaveClass(/is-saved/, { timeout: 15000 });
  await expect(saveNote(page)).toContainText('正文已保存');
}

/** 库里那条节点此刻的样子。 */
async function storedBody(page: Page, token: string, workspaceId: string, nodeId: string) {
  const node = (await getPlan(page, token, workspaceId)).nodes.find((item) => item.id === nodeId);
  if (!node) throw new Error(`节点 ${nodeId} 不在计划里 —— 它被删了或者压根没建成`);
  return node;
}

/**
 * 在**另一个浏览器上下文**里用同一个账户打开同一层。
 *
 * 空 localStorage:没有草稿、没有缓存的计划、没有"当前空间"那个本地键。这是"刷新
 * 之后还在"最干净的一种验法 —— 同一页刷新还可能被草稿存储糊过去。
 */
async function openInFreshContext(
  browser: Browser,
  account: TestAccount,
  workspaceId: string,
): Promise<{ close: () => Promise<void>; page: Page }> {
  const context = await browser.newContext();
  const other = await context.newPage();
  // 和 `registerAccount` 写令牌是同一件事,只是这边用的是**已经存在**的账户。
  await other.goto('/login');
  await other.evaluate(([key, value]) => localStorage.setItem(key, value), [TOKEN_KEY, account.token] as const);
  await openSpacePage(other, '/workbench', workspaceId);
  await waitForRealPlan(other);
  return { close: () => context.close(), page: other };
}

// ---------------------------------------------------------------------------------
// 1. 单击语义
// ---------------------------------------------------------------------------------
test('单击节点打开正文与详情，但不会顺带进入子空间', async ({ page }) => {
  const { workspaceId, rootId, stageId } = await scene(page, 'body-click');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await expect.poll(() => renderedNodeIds(page)).toEqual([rootId, stageId].sort());

  const dialog = await openEditor(page, stageId);
  // 正文**就在这个弹窗里**,不用再按第二个按钮才看得到。
  await expect(bodyField(dialog)).toBeVisible();
  // 详情只承担内容编辑。排期属性由时间线 / 任务视图负责，不在这里重复摆三套控件。
  await expect(dialog.getByLabel('优先级')).toHaveCount(0);
  await expect(dialog.getByLabel('截止时间')).toHaveCount(0);
  await expect(dialog.getByLabel('预计工时（分钟）')).toHaveCount(0);

  // 而"进去"这件事没有发生:画布上还是根那一层,阶段也还是一张普通卡片,
  // 不是当前这一层的根(那个是 `.growth-node.goal`,见 `PathView.tsx` 的 `root`)。
  await expect.poll(() => renderedNodeIds(page)).toEqual([rootId, stageId].sort());
  await expect(card(page, stageId).locator('.growth-node.goal'), '单击把子空间也进去了').toHaveCount(0);
});

// ---------------------------------------------------------------------------------
// 1B. 双击语义(§9.1.1):这一批新加的"空白处双击建节点"不能从别的手势漏进来
// ---------------------------------------------------------------------------------
/**
 * 这一条防的是**新建节点从别的手势里漏出来**。
 *
 * 双击空白处建节点是这一批新加的,而它挂在一个很宽的容器上(整个画布)。判定写松一点
 * ——比如"只要不是点在工具栏上就算空白"—— 双击节点、在正文编辑框里双击
 * 选一个词,都会冒出新建表单。**而它长得很正常**:一个标题写着「新建节点」的弹窗,
 * 用户以为是自己点错了。
 *
 * 所以这里逐个手势验一遍,而且**判据是弹窗的标题**:详情弹窗的标题是「编辑节点」,
 * 新建表单的标题是「…中新建节点」。只数"有几个弹窗"是不够的 —— 一个把详情换成新建
 * 表单的实现,数量同样是 1。
 */
test('双击节点只开详情，在编辑框里双击也不建节点', async ({ page }) => {
  const { workspaceId, rootId, stageId } = await scene(page, 'body-dblclick');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await expect.poll(() => renderedNodeIds(page)).toEqual([rootId, stageId].sort());

  const dialogs = page.getByRole('dialog');
  const headings = () => dialogs.getByRole('heading').allInnerTexts();

  // --- 双击节点:只开**详情**,不是新建表单 ---------------------------------
  await card(page, stageId).dblclick();
  await expect(dialogs.getByRole('heading')).toContainText('编辑节点');
  expect((await headings()).filter((text) => text.includes('新建节点')), '双击节点弹出了新建表单').toEqual([]);

  // --- 双击正文编辑框:什么都不开(用户是在选一个词) -------------------------
  const text = '这段正文里双击一下,是在选词。';
  await bodyField(dialogs).fill(text);
  const box = await bodyField(dialogs).boundingBox();
  if (!box) throw new Error('正文编辑框不在');
  await page.mouse.dblclick(box.x + box.width / 2, box.y + box.height / 2);
  await expect(dialogs).toHaveCount(1);
  expect((await headings()).filter((item) => item.includes('新建节点')), '在编辑框里双击建了节点').toEqual([]);
  await expect(bodyField(dialogs)).toHaveValue(text);
  await page.keyboard.press('Escape');
  await expect(dialogs).toHaveCount(0);

  // 连线上的同一条手势边界由 `canvas-create.spec.ts` 用一条显式依赖关系覆盖。
  // 手工节点不再自动产生父子线，所以这里不伪造一条只为测试存在的结构线。
});

// ---------------------------------------------------------------------------------
// 2. 自动保存:不按任何按钮,刷新(换个上下文)之后还在
// ---------------------------------------------------------------------------------
test('正文停手自动保存，换个浏览器上下文重新打开仍在', async ({ page, browser }) => {
  test.slow();
  const { account, workspaceId, stageId, leafId } = await scene(page, 'body-autosave');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  // 叶子挂在阶段下面,**根那一层画不出它** —— 先按箭头进子空间,再点它。
  await enterSpace(page, stageId);

  const dialog = await openEditor(page, leafId);
  const text = '这周先跑一组对照实验,周末整理结果。';
  await bodyField(dialog).fill(text);

  // **一个保存按钮都没按。** "可靠保存"这句话的分量全在这一行:用户不需要记得存。
  await expectSaved(page);

  // 第二层:库里真的有了,而且版本号推进了(新建时是第 1 版)。
  const saved = await storedBody(page, account.token, workspaceId, leafId);
  expect(saved.description).toBe(text);
  expect(saved.contentVersion).toBe(2);

  // 第三层:空 localStorage 的另一个上下文里读回来的还是这一段。
  const other = await openInFreshContext(browser, account, workspaceId);
  try {
    // 这个上下文也要自己进一次子空间 —— 它是空的 localStorage,没有"我刚才在哪一层"。
    await enterSpace(other.page, stageId);
    const otherDialog = await openEditor(other.page, leafId);
    await expect(bodyField(otherDialog)).toHaveValue(text);
    // 打开时就该说"与库里一致",而不是"有改动待保存" —— 那说明读到的是本地那份。
    await expect(saveNote(other.page)).toContainText('正文与库里一致');
  } finally {
    await other.close();
  }
});

// ---------------------------------------------------------------------------------
// 3. 存不上:草稿留着、说清失败、**绝不显示已保存**、重试能成
// ---------------------------------------------------------------------------------
test('正文存不上时草稿还在、界面说没存上，重试之后才写进去', async ({ page }) => {
  test.slow();
  const { account, workspaceId, stageId, leafId } = await scene(page, 'body-fail');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await enterSpace(page, stageId);

  // 只让**第一次**正文写入失败 —— 恢复成真的请求,重试才有意义。
  // (不用"按次数算"的假失败:那样"重试成功了"可能只是第二次恰好轮到放行。)
  let failNext = true;
  await page.route('**/api/workspaces/*/nodes/*', async (route) => {
    if (route.request().method() === 'PATCH' && failNext) {
      failNext = false;
      await route.abort('failed');
      return;
    }
    await route.continue();
  });

  const dialog = await openEditor(page, leafId);
  const text = '这段字不该丢。万一没存上,它得还在编辑器里。';
  await bodyField(dialog).fill(text);

  const note = saveNote(page);
  await expect(note).toHaveClass(/is-failed/, { timeout: 15000 });
  await expect(note).toContainText('正文没有保存上');
  // 草稿还在编辑器里 —— 这就是"可恢复"的全部含义。
  await expect(bodyField(dialog)).toHaveValue(text);

  // **失败不许自己变成功。** debounce 是 700 毫秒,等过它再看一眼:
  // 界面要一直说的是"没存上"。这一句是这一条测试的真正内容 ——
  // "保存失败但显示已保存"是这个功能最容易犯、也最难被用户发现的错。
  await page.waitForTimeout(1500);
  await expect(note).toHaveClass(/is-failed/);
  await expect(note, '界面在没写进去的情况下说了"已保存"').not.toContainText('正文已保存');

  // 三层里的第二层:**库里确实没有**。只验界面的话,分不清"写失败了"和
  // "写成功了但界面显示失败" —— 后者更糟,用户会白白重写一遍。
  const beforeRetry = await storedBody(page, account.token, workspaceId, leafId);
  expect(beforeRetry.description).toBeNull();
  expect(beforeRetry.contentVersion).toBe(1);

  await note.getByRole('button', { name: '重试' }).click();
  await expectSaved(page);
  const afterRetry = await storedBody(page, account.token, workspaceId, leafId);
  expect(afterRetry.description).toBe(text);
  expect(afterRetry.contentVersion).toBe(2);
});

// ---------------------------------------------------------------------------------
// 4. 别人先改了:409 → 说清冲突 → 覆盖要用**服务端那一版**做前置条件
// ---------------------------------------------------------------------------------
test('正文被别人改过时显示冲突，覆盖仍走版本校验、放弃更是一个字都不写', async ({ page }) => {
  test.slow();
  const { account, workspaceId, stageId, leafId } = await scene(page, 'body-conflict');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await enterSpace(page, stageId);

  // 用 `page.request` 而不是界面 —— 这是"**另一个人**在另一个标签页里写的"那一下。
  // 走界面的第二个标签页也行,但那要造一整个浏览器上下文才换来同一件事。
  const patch = (body: string, contentVersion: number) =>
    page.request.fetch(`${API_BASE}/api/workspaces/${workspaceId}/nodes/${leafId}`, {
      method: 'PATCH',
      headers: { Authorization: `Bearer ${account.token}`, 'Content-Type': 'application/json' },
      data: { description: body, contentVersion },
    });

  // --- 一、"覆盖"这条路 -----------------------------------------------------------
  const dialog = await openEditor(page, leafId);
  // 编辑器此刻手上是第 1 版(刚建的节点)。抢在它保存之前,别人拿**同一个版本号**写进去。
  const held = (await storedBody(page, account.token, workspaceId, leafId)).contentVersion;
  expect(held).toBe(1);
  const rival = await patch('别人写的', held);
  expect(rival.ok(), `竞争的写入本身就失败了:${rival.status()} ${await rival.text()}`).toBe(true);

  await bodyField(dialog).fill('我写的');
  // 我这一份带的是第 1 版 → 库里已经是第 2 版 → 409,一个字都不写进去。
  const note = saveNote(page);
  await expect(note).toHaveClass(/is-conflict/, { timeout: 15000 });
  await expect(note).toContainText('这段正文在别处被改过了');
  // **必须把库里那一份读出来给用户看** —— 只说"冲突了"他没法决定留哪一份。
  await expect(note).toContainText('库里现在是:「别人写的」');
  await expect(bodyField(dialog), '冲突时用户自己那份草稿被改掉了').toHaveValue('我写的');

  // 冲突期间不许自作主张地反复重发(见 `flushBody` 那段注释):状态要稳得住。
  await page.waitForTimeout(1500);
  await expect(note).toHaveClass(/is-conflict/);
  const untouched = await storedBody(page, account.token, workspaceId, leafId);
  expect(untouched.description, '冲突的那次保存竟然写进去了').toBe('别人写的');
  expect(untouched.contentVersion).toBe(2);

  const actions = page.locator('.body-conflict-actions');
  await expect(actions.getByRole('button', { name: '用我的草稿覆盖' })).toBeVisible();
  await expect(actions.getByRole('button', { name: '放弃我的改动,载入库里那份' })).toBeVisible();

  // --- 一之续:**点了覆盖,但这一下不能是无条件写入** ------------------------------
  //
  // 用户按下去之前又有人写了一次。覆盖带的是**冲突那一刻读回来的**那一版(2),
  // 而库里已经是 3 —— 所以它必须**再冲突一次**,而不是把别人的字盖掉。
  // 这一条是"锁没有被绕开"的直接证据:如果覆盖走的是无条件 PATCH,它会绿着写进去,
  // 而屏幕上什么都不会说。
  const between = await patch('别人在我要点覆盖之前又写了一次', held + 1);
  expect(between.ok(), `第三次竞争的写入失败了:${between.status()} ${await between.text()}`).toBe(true);
  const afterBetween = await storedBody(page, account.token, workspaceId, leafId);
  expect(afterBetween.description).toBe('别人在我要点覆盖之前又写了一次');

  await actions.getByRole('button', { name: '用我的草稿覆盖' }).click();
  await expect(note, '点了覆盖之后又有人改,它却照样写进去了 —— 这一下没有被版本校验拦住').toHaveClass(
    /is-conflict/,
    { timeout: 15000 },
  );
  await expect(note).toContainText('库里现在是:「别人在我要点覆盖之前又写了一次」');
  const stillNotMine = await storedBody(page, account.token, workspaceId, leafId);
  expect(stillNotMine.description, '覆盖绕开了版本锁,把别人的字盖掉了').toBe(
    '别人在我要点覆盖之前又写了一次',
  );

  // 再点一次:这一回带的是刚读回来的那一版,才真的写进去。
  await actions.getByRole('button', { name: '用我的草稿覆盖' }).click();
  await expectSaved(page);

  const won = await storedBody(page, account.token, workspaceId, leafId);
  expect(won.description).toBe('我写的');
  // **比"别人最后写的那一版 +1"。** 覆盖这一下拿的必须是**刚从库里读回来的**那一版做
  // 前置条件:一直拿着手上那个旧号(1)重发,它会永远 409,而界面上看起来是"点了没反应"。
  expect(
    won.contentVersion,
    '覆盖之后版本号不对 —— 这一下带的不是服务端那一版',
  ).toBe(afterBetween.contentVersion + 1);

  // --- 二、"放弃"这条路 -----------------------------------------------------------
  // 再来一次冲突:这次选放弃,它必须**只把库里那份读进编辑器,不写任何东西**。
  const rival2 = await patch('别人第二次写的', won.contentVersion);
  expect(rival2.ok(), `第二次竞争的写入失败了:${rival2.status()} ${await rival2.text()}`).toBe(true);
  // 把"别人写完之后库里是第几版"记下来当基准 —— **不写死一个数字**。
  // 写死的话,这条断言验的就成了"我算对了吗",而不是"放弃有没有偷偷写一次"。
  const afterRival = await storedBody(page, account.token, workspaceId, leafId);
  expect(afterRival.description).toBe('别人第二次写的');

  await bodyField(dialog).fill('我又改了一版');
  await expect(note).toHaveClass(/is-conflict/, { timeout: 15000 });
  await expect(note).toContainText('库里现在是:「别人第二次写的」');

  await actions.getByRole('button', { name: '放弃我的改动,载入库里那份' }).click();
  await expect(bodyField(dialog)).toHaveValue('别人第二次写的');

  // 放弃之后那段字**和库里一致了**,所以既不该重发,也不该留下"没保存上"。
  await page.waitForTimeout(1500);
  await expect(note, '放弃之后界面还说有冲突').not.toHaveClass(/is-conflict/);
  await expect(note).not.toHaveClass(/is-failed/);
  const given = await storedBody(page, account.token, workspaceId, leafId);
  expect(given.description, '选了"放弃我的改动",库里却被改了').toBe('别人第二次写的');
  expect(
    given.contentVersion,
    '选了"放弃我的改动",却还是发出了一次写入 —— 版本号前进了',
  ).toBe(afterRival.contentVersion);
});

// ---------------------------------------------------------------------------------
// 5. 改子节点的正文,回到上层:变化在,而且没有多出节点、没有多出工时
// ---------------------------------------------------------------------------------
test('在子空间改正文后回到上层：正文在，节点没多、工时没变', async ({ page }) => {
  test.slow();
  const { account, workspaceId, rootId, stageId, leafId } = await scene(page, 'body-nested');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);

  const planBefore = await getPlan(page, account.token, workspaceId);
  expect(planBefore.nodes).toHaveLength(3);
  const leafBefore = planBefore.nodes.find((node) => node.id === leafId)!;

  // 进阶段那一层 —— 走箭头,不是单击(见 `support/session.ts::enterSpace`)。
  await enterSpace(page, stageId);
  await expect.poll(() => renderedNodeIds(page)).toEqual([stageId, leafId].sort());

  const text = '只能周末做:周中实验室排不开。';
  const dialog = await openEditor(page, leafId);
  await bodyField(dialog).fill(text);
  await expectSaved(page);
  // 关掉弹窗再去点面包屑 —— 弹窗是模态的,下面是点不动的。
  await page.getByRole('dialog').getByRole('button', { name: '关闭弹窗' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);

  // 回到根那一层。
  await page.getByRole('button', { name: '返回上级空间' }).click();
  await expect.poll(() => renderedNodeIds(page)).toEqual([rootId, stageId].sort());

  // 变化是从库里读回来的,不是"同一个组件里还留着那一份"。
  const after = await getPlan(page, account.token, workspaceId);
  expect(after.nodes).toHaveLength(3);
  expect(new Set(after.nodes.map((node) => node.id)).size, '正文保存弄出了重复的节点').toBe(3);
  expect(after.totalNodes).toBe(3);
  const child = after.nodes.find((node) => node.id === leafId)!;
  expect(child.description).toBe(text);
  // **正文保存只动正文。** 保存时随手把别的列抹掉的话,界面上要等到排期那一天
  // 才会有人发现,而那时候谁也说不清是哪一步弄丢的。
  // 逐项和**保存前那一份**比,而不是写死一个值 —— 写死的话,验的就成了"我记对枚举了吗"。
  expect(child.estimateMinutes, '保存正文时把预计工时弄丢了 —— 那条任务会突然排不进任何一天').toBe(90);
  expect(child.estimateMinutes).toBe(leafBefore.estimateMinutes);
  expect(child.title).toBe(leafBefore.title);
  expect(child.status).toBe(leafBefore.status);
  expect(child.deadline).toBe(leafBefore.deadline);
  expect(child.parentId).toBe(leafBefore.parentId);

  // 再进去一次:编辑器里是库里那一份。
  await enterSpace(page, stageId);
  const again = await openEditor(page, leafId);
  await expect(bodyField(again)).toHaveValue(text);
  await expect(saveNote(page)).toContainText('正文与库里一致');
});
