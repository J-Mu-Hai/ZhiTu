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
