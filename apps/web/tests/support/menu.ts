import { expect, type Locator, type Page } from '@playwright/test';

/**
 * 打开一个节点的右键菜单。
 *
 * ## 为什么值得单开一个文件
 *
 * §9.1.1 把「归档」从**常驻垃圾桶**挪进了菜单。于是"收走一个节点"从一个动作
 * (点按钮)变成两个(开菜单 → 选项),而**三个 spec 都要走这两步**
 * (`node-delete`、`undo-redo`、`canvas-create`)。各抄一份的话,菜单的触发方式一变
 * (按钮类名、`Shift+F10`、还是直接右键)就要改三处 —— 而漏掉的那一处不会报错,
 * 它会停在"菜单没开",长得像被测功能坏了。
 *
 * ## 两条路,这里走的是按钮那条
 *
 * 菜单能通过点 `.node-more` 打开,也能通过右键节点打开。这里用**按钮**:它不依赖
 * 指针坐标,不受节点在画布上的位置影响。右键那条路是产品行为的一部分,由
 * `canvas-create.spec.ts` 单独验。
 *
 * `hover()` 在前面不是为了好看:`.node-more` 平时 `opacity: 0`,悬停才到 1
 * (触屏下恒显,见 `dark-theme.css` 的 `@media (hover: none)`)。不先移上去点的话,
 * 指针落在别处,点的是一个透明按钮 —— Playwright 仍然点得到它(它没有
 * `pointer-events: none`),所以失败不会出现在这里,而会出现在后面某一步。
 */
export async function openNodeMenu(page: Page, nodeId: string): Promise<Locator> {
  const node = page.locator(`.react-flow__node[data-id="${nodeId}"]`);
  await node.hover();
  await node.locator('.node-more').click();
  const menu = page.getByRole('menu');
  await expect(menu, `节点 ${nodeId} 的菜单没打开`).toBeVisible();
  return menu;
}

/**
 * 在已经开着的菜单里选一项。
 *
 * 按**可见文字**选,不按下标:`getByRole('menuitem', { name })` 是子串匹配,
 * 所以「归档」能选中「归档(可以恢复)」。菜单项的**顺序**是产品可以改的,
 * 而"用户看到的那一项"不是。
 */
export async function chooseMenuItem(page: Page, label: string): Promise<void> {
  await page.getByRole('menu').getByRole('menuitem', { name: label }).click();
}

/** 开菜单并选一项,合成一步。 */
export async function selectNodeMenuItem(page: Page, nodeId: string, label: string): Promise<void> {
  await openNodeMenu(page, nodeId);
  await chooseMenuItem(page, label);
}

/**
 * 打开画布工具栏上那个「更多空间操作」菜单。
 *
 * ## 为什么"新建节点"变成了两步
 *
 * 这些低频率的操作(新建节点 / 建立关系 / 归档)从**常驻按钮**搬进了这个菜单:
 * 常驻的那一排只留高频的。于是"建一个节点"从一个动作变成两个,而**直接在页面这一层
 * 找 `getByRole('button', { name: '新建节点' })` 的写法会找不到元素** ——
 * 菜单收起来的时候,`<details>` 里的内容不在无障碍树里,`getByRole` 看不见它。
 * 报错会写成 `element(s) not found`,读起来像按钮被删掉了,而不是"它收进菜单了"。
 *
 * `support/menu.ts` 里两个菜单各有一个入口函数,理由是一样的:菜单怎么开
 * (点哪个按钮、要不要悬停、`Shift+F10`)是产品可以改的,而抄在各 spec 里的那几份
 * 会静静地停在"菜单没开",长得像被测功能坏了。
 *
 * ## 幂等 —— 以及为什么要**重试着开**
 *
 * 已经开着就不再点一次 summary(再点是**关掉**它)。菜单项点了之后会自己收起,
 * 所以同一条用例里取第二次时,这里会重新打开。
 *
 * 但"再打开"不只是为了第二次:菜单的 `open` 是**直接写在 `<details>` 上的命令式
 * 属性**,而画布在"计划刚到"这一刻会重渲染 —— 那个 `<details>` 连同 `open` 一起被
 * 换掉,菜单会在用户眼皮底下自己合上。真人在那一刻会再点一次,这里做的也正是
 * 这件事:打开 → 等那一项**真的看得见**,没看见就再开一次。
 *
 * 这不是把抖动藏起来:藏起来是"放宽断言直到它过",而这里是"做到它真的开着"。
 * 拿掉重试就会变成一条时间赛跑 —— 在隔离栈里它已经红过一次
 * (`auth-profile` 注册那条、`live-loop` 建任务那条;trace 里菜单开过、随后
 * `<details>` 上就没有 `open` 了)。
 */
export async function openCanvasTools(page: Page): Promise<Locator> {
  const menu = page.locator('.canvas-tools-menu');
  const popover = page.locator('.canvas-tools-popover');
  await expect(menu, '画布工具栏不见了').toBeVisible();
  await expect(async () => {
    if (!(await menu.evaluate(element => (element as HTMLDetailsElement).open))) {
      await menu.locator('summary').click();
    }
    await expect(popover, '「更多空间操作」菜单没有打开').toBeVisible({ timeout: 1500 });
  }).toPass({ timeout: 15000 });
  return popover;
}

/**
 * 菜单里的一项(**不点**,把断言留给调用方)。
 *
 * 先开菜单是必须的:收着的时候它不在无障碍树里 —— 收着时找那一项得到的是
 * `element(s) not found`,读起来像按钮被删了。要检查"计划还没到的时候不能建"之类的
 * 禁用状态,拿它去 `toBeEnabled()`/`toBeDisabled()` 就好 —— 禁用态是产品行为,
 * 不因为多了一层菜单而改变。
 */
export async function canvasTool(page: Page, label: string): Promise<Locator> {
  const popover = page.locator('.canvas-tools-popover');
  const item = popover.getByRole('button', { name: label });
  await expect(page.locator('.canvas-tools-menu'), '画布工具栏不见了').toBeVisible();
  await expect(async () => {
    const menu = page.locator('.canvas-tools-menu');
    if (!(await menu.evaluate(element => (element as HTMLDetailsElement).open))) {
      await menu.locator('summary').click();
    }
    await expect(item, `「更多空间操作」菜单里没有「${label}」这一项`).toBeVisible({ timeout: 1500 });
  }).toPass({ timeout: 15000 });
  return item;
}
