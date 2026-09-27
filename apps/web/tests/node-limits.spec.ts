/**
 * 字数上限与长正文(第二批 §2.1 / §2.2)。
 *
 * ## 这一组验的是"界面上的数与服务端的数是同一个数"
 *
 * 上限本身由 `backend/tests/test_description_limit.py` 与 `test_node_notes.py` 钉着。
 * 这里要验的是**另一半**,而那一半只有在浏览器里才看得见:同一段文字,输入框旁边的
 * 计数说多少、服务端最后收到多少 —— 两者必须是同一个数。
 *
 * 差一个就会走到两个方向上都错的地方:
 *
 * - 前端数 UTF-16 码元的话,一个 emoji 被数成 2。用户打了 300 个 emoji,计数器说
 *   "600/300"、保存按钮是灰的,而服务端本来会照收 —— **一个用户看不见的假上限**。
 * - 前端不数、只在服务端拒的话,用户点了按钮才吃到 400,而那条错误在界面上长得
 *   就是"点了没反应"。
 *
 * 所以这里**必须**用 `insertText` 打那几个星平面字符:`.fill()` 与键盘输入在
 * Playwright 里对代理对的处理是一样的,但 `insertText` 是唯一一个"确定性地一次
 * 插入整段"的入口 —— 逐字符敲的话,某些字符会被输入法/键盘布局吃掉。
 *
 * ## 长正文为什么不能用 `description` 顶替
 *
 * 最要紧的一条断言在这里:**长正文写进去之后,`/plan` 里的 `description` 一个字
 * 都没变**。它俩是两套账(见 `backend/db/models/note.py` 与
 * `provider.tsx::saveNodeNote`)。如果哪天有人图省事把长正文塞回 `plan_nodes`,
 * 那句断言会红 —— 而那句红起来的理由不是"两条路都写不进去",是"两万字的正文
 * 开始出现在每一次读计划里,并且被快照进每一版 `plan_revisions`"。
 */

import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  api,
  assertBackendRunning,
  createNode,
  createWorkspace,
  getPlan,
  registerAccount,
  waitForRealPlan,
} from './support/session';

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
});

/**
 * 一段**没有任何首尾空白**的混排文字:`_clean` 会去掉两端空白,带上会让比对差一个。
 *
 * 后半段那个 `if` 不能省:长度正好是 4 的倍数时,`unit` 重复完刚好以 `\n` 结尾,
 * 存下来会变成 `length - 1`。那是**归一化**,不是上限 —— 混进"正好 300 能存"这条
 * 断言里,将来它红了会让人以为上限错了。所以整除时少重复一次、改用不带换行的字补齐。
 */
function mixed(length: number): string {
  const unit = '学🛫z\n';          // 4 个码点,UTF-16 里是 5 个码元
  let repeats = Math.floor(length / 4);
  let rest = length - repeats * 4;
  if (rest === 0) { repeats -= 1; rest = 4; }
  return unit.repeat(repeats) + '好'.repeat(rest);
}

async function scene(page: Page, prefix: string) {
  const account = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, account.token, '上限验收空间', '验字数上限与长正文');
  const rootId = (await getPlan(page, account.token, workspaceId)).nodes[0].id;
  return { account, workspaceId, rootId };
}

function card(page: Page, nodeId: string): Locator {
  return page.locator(`.react-flow__node[data-id="${nodeId}"]`);
}

/** 打开新建节点表单,等它真的能用(计划到达之前那个按钮是禁用的)。 */
async function openCreate(page: Page, workspaceId: string): Promise<Locator> {
  await page.goto(`/workbench?workspace=${workspaceId}`);
  const trigger = page.getByRole('button', { name: '新建节点' });
  await expect(trigger).toBeEnabled();
  await trigger.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('节点名称')).toBeVisible();
  return dialog;
}

/** 打开某个节点的详情弹窗。**一次单击** —— 双击是"建节点",见 `node-body.spec.ts`。 */
async function openEditor(page: Page, workspaceId: string, nodeId: string): Promise<Locator> {
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  await card(page, nodeId).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('heading')).toContainText('编辑节点');
  return dialog;
}

// ---------------------------------------------------------------------------------
// 1. 300 个码点能存,301 个存不进去 —— 而且计数的口径和服务端是同一个
// ---------------------------------------------------------------------------------
test('说明的 300 个码点按码点算：混排加 emoji 正好 300 能存，301 被拦住', async ({ page }) => {
  const { account, workspaceId } = await scene(page, 'limit-300');
  const dialog = await openCreate(page, workspaceId);

  const counter = dialog.locator('.body-count');
  await expect(counter).toHaveText('0/300');

  const name = '正好三百个码点';
  await dialog.getByLabel('节点名称').fill(name);
  const box = dialog.getByLabel(/节点说明|树叶说明/);

  // ---- 正好 300:因为按**码点**数,这一串里那 75 个 emoji 各算一个。
  //      按 UTF-16 码元数的话这里是 375,计数器会说"超了",而服务端照收。
  const exact = mixed(300);
  await box.click();
  await page.keyboard.insertText(exact);
  await expect(counter, '计数按 UTF-16 码元数了 —— emoji 被算成两个').toHaveText('300/300');
  await expect(dialog.getByRole('button', { name: '新建节点' })).toBeEnabled();
  await dialog.getByRole('button', { name: '新建节点' }).click();
  await expect(dialog).toBeHidden();

  const plan = await getPlan(page, account.token, workspaceId);
  const created = plan.nodes.find(node => node.title === name);
  expect(created, '正好 300 码点的说明没有建出节点来').toBeTruthy();
  // **逐字相同**:不只是"存进去了",是"一个字都没被改动"。
  expect(created!.description, '存下来的说明与打进去的那一份不是同一段文字').toBe(exact);

  // ---- 301:界面先说清楚,而且**按钮按不下去**(不是发出去等一个 400)
  const second = await openCreate(page, workspaceId);
  await second.getByLabel('节点名称').fill('三百零一个码点');
  const secondBox = second.getByLabel(/节点说明|树叶说明/);
  await secondBox.click();
  await page.keyboard.insertText(mixed(301));
  await expect(second.locator('.body-count')).toContainText('这一份有 301 个');
  await expect(second.locator('.body-count')).toHaveClass(/is-over/);
  await expect(second.getByRole('button', { name: '新建节点' })).toBeDisabled();

  const after = await getPlan(page, account.token, workspaceId);
  expect(after.nodes.some(node => node.title === '三百零一个码点'), '超限的说明还是建出节点了').toBe(false);
});

// ---------------------------------------------------------------------------------
// 2. 判"受不受限"用的是**库里那一份**,不是输入框里那个数
// ---------------------------------------------------------------------------------
/**
 * 这条本该在浏览器里验,而**它在隔离栈里造不出来**:豁免的判据是"库里那一份已经超过
 * 300",而任何一条能写进库的路径都不会让一份不豁免的说明变成逾限的 —— 上限就是写
 * 入路径上执行的。唯一的造法是绕过路径直接改一行,那是 pytest 那边
 * (`test_description_limit.py::_force_long`)在做的事,浏览器这边没有等价的手。
 *
 * 所以这里只验**判据本身**,把那个纯函数按它的语义钉住。真正在浏览器里能看到
 * 豁免文案的,只有上限出现之前就存在的数据 —— 那种状态在老库里有,在新库里没有。
 */
test('豁免的判据是存量，不是输入框里那个数', async () => {
  const { codePointLength, isDescriptionExempt } = await import('../src/lib/codepoints');
  // 数的是**码点**:同一段文字,按码点算与按 UTF-16 码元算差一个 emoji 的量。
  expect(codePointLength('🛫')).toBe(1);
  expect('🛫'.length).toBe(2);

  // 存量 301 → 豁免;存量 300 → 不豁免(边界是"**超过** 300",不是"达到")。
  expect(isDescriptionExempt('学'.repeat(301))).toBe(true);
  expect(isDescriptionExempt('学'.repeat(300))).toBe(false);
  // 没写过(`null`)与空串都不豁免 —— 那不是"存量很长",那是"还没有正文"。
  expect(isDescriptionExempt(null)).toBe(false);
  expect(isDescriptionExempt('')).toBe(false);
});

// ---------------------------------------------------------------------------------
// 3. 长正文是**另一张表**:它写进去,`description` 一个字都不动
// ---------------------------------------------------------------------------------
test('长正文存得住、读得回来，而且不写进 plan_nodes.description', async ({ page }) => {
  const { account, workspaceId, rootId } = await scene(page, 'limit-note');
  const nodeId = await createNode(page, account.token, workspaceId, {
    parentId: rootId, title: '有笔记的节点', nodeType: 'task', description: '这段说明不该被动。',
  });

  const dialog = await openEditor(page, workspaceId, nodeId);
  const body = '第一段：先把材料收齐。\n\n第二段：\n  缩进也要原样留着。\n\n🛫';
  const box = dialog.getByLabel('长正文（笔记）');
  await box.fill(body);
  await expect(dialog.locator('.note-save-note')).toHaveClass(/is-saved/, { timeout: 15000 });

  // ---- 半条命:它在库里。
  // `GET` 返回的是**笔记本身**,不是 `{note: …}` —— 只有 `PUT` 包一层(它回的是
  // `NoteEditResponse`)。这两行读错了会让断言报"读不到",而原因看着像"没存上"。
  const note = await api<{ body: string; contentVersion: number }>(
    page, account.token, `/api/workspaces/${workspaceId}/nodes/${nodeId}/notes`,
  );
  // **逐字相同**,包括空行与缩进 —— 长正文是原样存的,不做 `_clean` 那种首尾修剪。
  expect(note.body).toBe(body);
  expect(note.contentVersion).toBe(1);

  // ---- 另半条命(这一条才是这个测试存在的理由):`description` 没有被顺手写进去
  const plan = await getPlan(page, account.token, workspaceId);
  expect(plan.nodes.find(node => node.id === nodeId)!.description, '长正文串到简述里去了').toBe('这段说明不该被动。');

  // ---- 关掉重开:正文要从**服务端**读回来,而不是留在编辑器里那一份
  await dialog.getByRole('button', { name: '关闭弹窗' }).click();
  const again = await openEditor(page, workspaceId, nodeId);
  await expect(again.getByLabel('长正文（笔记）')).toHaveValue(body);
});

// ---------------------------------------------------------------------------------
// 4. 超过两万码点:**不自动存**,而且库里那份一个字都没变
// ---------------------------------------------------------------------------------
test('长正文超过两万码点之后不再自动保存，库里那一份保持原样', async ({ page }) => {
  const { account, workspaceId, rootId } = await scene(page, 'limit-note-over');
  const nodeId = await createNode(page, account.token, workspaceId, {
    parentId: rootId, title: '笔记会写超的节点', nodeType: 'task',
  });

  const dialog = await openEditor(page, workspaceId, nodeId);
  const box = dialog.getByLabel('长正文（笔记）');
  await box.fill('这一份是能存上的。');
  await expect(dialog.locator('.note-save-note')).toHaveClass(/is-saved/, { timeout: 15000 });
  const before = await api<{ contentVersion: number }>(
    page, account.token, `/api/workspaces/${workspaceId}/nodes/${nodeId}/notes`,
  );

  // 写成超限的一份。停手之后**不会**有保存请求 —— 服务端会拒,而拒了之后每 700
  // 毫秒再撞一次同一堵墙,屏幕上只会来回闪"正在保存"。
  await box.fill('学'.repeat(20001));
  await expect(dialog.locator('.note-field .body-count')).toHaveText('20001/20000');
  await expect(dialog.locator('.note-field .body-count')).toHaveClass(/is-over/);
  await expect(dialog.locator('.note-save-note')).toContainText('这样存不进去');

  // 等一个足够长的窗口(防抖是 700 毫秒),然后问库里的真值。
  await page.waitForTimeout(2000);
  const after = await api<{ body: string; contentVersion: number }>(
    page, account.token, `/api/workspaces/${workspaceId}/nodes/${nodeId}/notes`,
  );
  expect(after.contentVersion, '超限那一份被存进去了 —— 服务端的上限被绕过了').toBe(before.contentVersion);
  expect(after.body).toBe('这一份是能存上的。');

  // 改回能存下的长度之后它**会**自己接着存 —— 上面那个"不存"是长度造成的,
  // 不是这条自动保存被卡死了。
  await box.fill('改短了，这一段应该自己存上去。');
  await expect(dialog.locator('.note-save-note')).toHaveClass(/is-saved/, { timeout: 15000 });
});
