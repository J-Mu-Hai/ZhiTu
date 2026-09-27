/**
 * 画布布局的撤销与重做。**这一版只管"位置",别的什么都不管。**
 *
 * ## 为什么先只做位置
 *
 * 画布上能改的东西分成两类,而它们的撤销**不是同一件事**:
 *
 * - **布局**:节点在哪儿。改的是 `node_positions`,`PUT /layout` 不产生计划版本
 *   (见 `backend.ts` 的 `PutLayoutRequest`)。撤销它 = 把一行位置写回去,没有别的后果。
 * - **业务**:建节点、改标题、建关系、归档。这些是真改动 —— 每一次都已经写过
 *   `PlanRevision` 和事件,撤回去必须是一次**新的、受版本校验的补偿操作**(任务书
 *   §4.8 要求走 `contentVersion` / `revisionVersion`),而不是把画面擦掉。
 *
 * 把这两类混进同一个 `Ctrl+Z`,用户按下去之前没法知道会发生哪一种 —— 而"擦掉画面
 * 但库里没变"正是最难发现的一种坏。所以这一版**只收位置**,业务撤销另行设计。
 *
 * ## 业务数据**不可能**从这里被恢复
 *
 * 这个文件里的代码只读写 `Record<key, {x, y}>`,它连节点 id 之外的东西都拿不到。
 * 撤销一个节点的位置**不会**让它从归档里回来,也不会重建一行 —— 它只决定"这个东西
 * 在画布上画在哪"。一个已经归档的节点没有画面可摆,它的位置只会被**跳过并说明**
 * (见 `applyPatch` 的 `keep`)。
 *
 * ## 一条拖拽 = 一条历史
 *
 * 逐帧的位置是**预览**,不是操作(`PathView` 把它们放在 `dragging` 里,只在这一次
 * 渲染里有效)。一次拖拽中途会经过几十个坐标,都记进历史的话,用户按一次撤销只会把
 * 节点往回挪一个像素,而且永远撤不完。
 */

/** 画布上的位置表。键是 `${看到的那一层}:${节点 id}`,与 `provider` 里的 `positions` 同形。 */
export type PositionMap = Record<string, { x: number; y: number }>;

/**
 * 一次布局改动。
 *
 * 两份都是**只含这次动过的键**的小补丁,不是整份位置表:整份的话,一次撤销会把
 * 别处(另一个层级、另一个节点)在之后发生的改动一起抹掉 —— 那些改动不属于这一步,
 * 用户也没打算撤它们。
 */
export interface LayoutEdit {
  before: PositionMap;
  after: PositionMap;
}

/** 撤销/重做两条栈。`past` 的末尾是"上一步",`future` 的末尾是"下一步"。 */
export interface LayoutHistory {
  past: LayoutEdit[];
  future: LayoutEdit[];
  /**
   * 这份历史属于谁:`${账号 id}:${空间 id}`。
   *
   * Provider 的 `key` 已经是空间 id(换空间就是换一份状态),退出登录会把整个
   * Provider 卸掉 —— 所以隔离开销在结构上已经成立。这里**照样记一份**所有者,
   * 是因为那两条都靠"外面的人用对" :有一天有人给 Provider 换 key,或者把历史挪到
   * 一个不随登录状态卸载的地方,这里的判断就是唯一还在拦着"上一个人的撤销把我摆的
   * 位置挪走"的东西。
   */
  owner: string;
}

/**
 * 最多记多少步。
 *
 * 有上限不是省内存,是**别让撤销变成一个没有底的操作**:位置补丁很小,几百步也占不了
 * 多少,但用户连着按三十次撤销想回到"刚才那一版"时,一个深不见底的栈只会让他
 * 一路退回今天打开页面时的样子。50 步足够覆盖"我刚才摆了几次"这件事。
 */
export const LAYOUT_HISTORY_LIMIT = 50;

export function emptyHistory(owner: string): LayoutHistory {
  return { past: [], future: [], owner };
}

/** 复制一份键值对,**连坐标对象一起换新的**。 */
function copyMap(map: PositionMap): PositionMap {
  const copy: PositionMap = {};
  for (const [key, value] of Object.entries(map)) copy[key] = { x: value.x, y: value.y };
  return copy;
}

/**
 * 记一步。
 *
 * - **所有者和记着的那份不一样就从头开始** —— 不是"接着往上叠"。见 `owner`。
 * - **新操作发生 → 重做分支作废。** 这是撤销/重做的通用语义:用户撤销两步、又拖了
 *   一个节点,他就不再站在"可以重做那两步"的线上了。留着那条分支,下一次"重做"会把
 *   他带到一个他从没走过的状态上(那个状态里,他刚拖的那个节点回到了原点)。
 * - 超出上限时**丢最旧的**。
 */
export function recordEdit(history: LayoutHistory, owner: string, edit: LayoutEdit): LayoutHistory {
  const base = history.owner === owner ? history : emptyHistory(owner);
  const past = [...base.past, { before: copyMap(edit.before), after: copyMap(edit.after) }];
  return { past: past.slice(-LAYOUT_HISTORY_LIMIT), future: [], owner };
}

/** 撤销一步:返回新的历史、以及**要写回去的那份位置**。没得撤时返回 `null`。 */
export function undoEdit(history: LayoutHistory): { history: LayoutHistory; patch: PositionMap } | null {
  const edit = history.past[history.past.length - 1];
  if (!edit) return null;
  return {
    history: { past: history.past.slice(0, -1), future: [...history.future, edit], owner: history.owner },
    patch: copyMap(edit.before),
  };
}

/** 重做一步。同上,方向相反。 */
export function redoEdit(history: LayoutHistory): { history: LayoutHistory; patch: PositionMap } | null {
  const edit = history.future[history.future.length - 1];
  if (!edit) return null;
  return {
    history: { past: [...history.past, edit], future: history.future.slice(0, -1), owner: history.owner },
    patch: copyMap(edit.after),
  };
}

/**
 * 把一份位置补丁合进当前位置表。
 *
 * `keep` 决定一个键**还能不能落** —— 调用方拿它去问"这个节点还在这个空间里吗"。
 * 落不了的键**跳过去,并且把键名报回去**:调用方要能对用户说"这一步里有 2 个东西
 * 已经不在了,只恢复了剩下的那些"。**跳过不等于替代** —— 这里绝不会为了摆一个位置
 * 而新建一行、或者把归档的东西捞回来(见文件头)。
 *
 * 值没变的键不算改动,凭空造出一个"看着像改了"的新表只会让下游多一次无意义的保存。
 */
export function applyPatch(
  current: PositionMap,
  patch: PositionMap,
  keep: (key: string) => boolean,
): { positions: PositionMap; skipped: string[] } {
  const skipped: string[] = [];
  const next = { ...current };
  let changed = false;
  for (const [key, value] of Object.entries(patch)) {
    if (!keep(key)) { skipped.push(key); continue; }
    const old = next[key];
    if (old && old.x === value.x && old.y === value.y) continue;
    next[key] = { x: value.x, y: value.y };
    changed = true;
  }
  return { positions: changed ? next : current, skipped };
}
