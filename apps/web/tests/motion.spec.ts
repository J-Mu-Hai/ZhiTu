import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  addRelation,
  api,
  assertBackendRunning,
  createNode,
  createWorkspace,
  getPlan,
  getToday,
  openSpacePage,
  registerAccount,
  scheduleEverything,
  waitForRealPlan,
} from './support/session';
import { artifactPath } from './support/artifacts';

/**
 * 生命感与 reduced-motion 的验收。
 *
 * 规范:`docs/12-MOTION-AND-LIVENESS-SYSTEM.md`;实现:`src/app/motion.css` 与
 * `AmbientGlow` / `PathView` / `RealToday` / `Journal` 里那几处状态。
 *
 * ## 这个文件验的是**触发条件**,不是"好不好看"
 *
 * 动效最容易骗人的地方是"它看起来在表示某件事"。所以这里每一条都盯住**什么真实状态
 * 让它播出来**,而不是它长什么样:
 *
 * | 动画 | 唯一的触发条件 | 谁给的 |
 * | --- | --- | --- |
 * | 新建节点淡入 | `addNode` **返回了**那个节点 | 后端真的建了一行 |
 * | 连线绘制 | `addRelation` **返回了**那条边 | 后端真的写了一条关系 |
 * | 勾选完成 | 写入响应 `saved` 为真 | 库里真的多了那一条 |
 * | hover 变清晰 | 指针在那个节点上 | 真实的 id 比较 |
 * | 拖动抬升 | ReactFlow 自己加的 `.dragging` | 手指真的按着 |
 *
 * 对应地,这里有两条**反向**的用例:写失败时不许有任何"完成"的样子(第 7 条),
 * AI 没在跑的时候不许出现「正在思考」(第 8 条)。它们比正向那几条更要紧 ——
 * 一个假的"成功"反馈,用户没有任何办法看出来。
 *
 * ## 证据怎么留
 *
 * 十几条断言里有一半靠"类名在不在"是测不出来的:220 毫秒的动画在轮询里可能一次都
 * 抓不到。所以这个文件用 `animationstart` 事件把**这一份文档里跑过的每一个动画**
 * (名字、挂在哪个元素上、离它最近的那个真实 `data-id`)记在 `window.__motion` 上,
 * 断言读的是这份流水。它同时能答两个问题:"播了吗"和"播了几次" ——
 * 后者正是规范 4.1 最后那句"不许因为一次重渲染就把所有节点重播一遍"。
 *
 * ## 它不验的
 *
 * 不验美观(那要看截图)、不验时长具体是多少毫秒(令牌表在 `motion.css` 里,
 * 这里只验"面板用的是面板令牌"这一层)、不验 reduced motion 之外的浏览器差异。
 * 也不重复后端已经验过的东西 —— 关系和布局的整批拒绝、幂等、越权都在
 * `backend/tests/` 里。
 *
 * 所有截图落在这一轮的现场目录里(`tests/support/artifacts.ts`)。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

// ---------------------------------------------------------------------------------
// 现场记录:这一份文档里跑过的每一个动画
// ---------------------------------------------------------------------------------

interface MotionEntry {
  /** `@keyframes` 的名字。过渡不会进来(过渡不是"动画")。 */
  name: string;
  /** 动画挂着的那个元素自己的 class。 */
  cls: string;
  /** 它所在的"有意义的那一层":节点、今天那一行、Dock、菜单…… */
  host: string;
  /** 最近的 `[data-id]` 祖先(节点 id / 边 id)。**动效必须能对上一个真实 id。** */
  ref: string | null;
  /** 关键帧里出现的位移声明。空数组 = 这个动画只改透明度或颜色。 */
  frames: string[];
}

interface TransitionEntry {
  property: string;
  cls: string;
  /** 这一刻这条过渡**实际**要跑多久。reduced motion 下它必须是瞬时的。 */
  duration: string;
}

interface MotionLog {
  animations: MotionEntry[];
  /** `frames` 非空的那几条 —— reduced motion 下这个必须是空的。 */
  displacements: MotionEntry[];
  /** 任何一次 `transform` / `translate` / `scale` 的过渡。 */
  transformTransitions: TransitionEntry[];
}

/**
 * 装上记录器。**必须在 `page.goto` 之前调**(走 `addInitScript`)——
 * 加载期就开始的动画(比如 Dock 进场)不算的话,"普通节点有没有自己动起来"
 * 这个问题就漏了一半。
 *
 * ## 为什么要顺手写一份到 `sessionStorage`
 *
 * `addInitScript` 在**每一次跳转**上都会重跑一遍,而 `window` 是新的 —— 走一趟
 * `/today` → `/journal` 之后,在 `/today` 上录下来的那些就没了。这个文件里正好有
 * 那样一条(勾选完了再去随笔页)。所以记录同时写进 `sessionStorage`:它在一次
 * 会话里跨页面活着,而 Playwright 每个测试给一个全新的上下文,不会串到别的测试。
 */
async function instrumentAnimations(page: Page): Promise<void> {
  await page.addInitScript(() => {
    interface Entry { name: string; cls: string; host: string; ref: string | null; frames: string[] }
    interface Moved { property: string; cls: string; duration: string }
    type Store = { animations: Entry[]; displacements: Entry[]; transformTransitions: Moved[] };
    const empty: Store = { animations: [], displacements: [], transformTransitions: [] };
    let store: Store = empty;
    try {
      store = JSON.parse(sessionStorage.getItem('__motion') ?? 'null') ?? empty;
    } catch {
      store = empty; // 上一次写到一半被打断(跳转正好落在写的过程中)—— 从头记。
    }
    (window as unknown as { __motion: Store }).__motion = store;
    const save = () => {
      try { sessionStorage.setItem('__motion', JSON.stringify(store)); } catch { /* 存不下就只留内存里那一份 */ }
    };

    // "停在原地"的几种写法。`translate: 0 -50%` 那种静态居中不在其中 ——
    // 这里看的是**关键帧里有没有位移**,不是元素当前在哪。
    const atRest = (value: string) =>
      value === 'none' || value === '0' || value === '0px' ||
      /^matrix\(1, 0, 0, 1, 0, 0\)$/.test(value) ||
      /^translate(3d)?\(0px(, 0px){1,2}\)$/.test(value) ||
      /^scale\(1(, 1)?\)$/.test(value);

    const moving = (target: Element): string[] => {
      const found: string[] = [];
      for (const animation of target.getAnimations()) {
        const effect = animation.effect as KeyframeEffect | null;
        for (const frame of effect?.getKeyframes?.() ?? []) {
          for (const property of ['transform', 'translate', 'scale']) {
            const value = (frame as unknown as Record<string, unknown>)[property];
            if (typeof value === 'string' && !atRest(value)) found.push(`${property}: ${value}`);
          }
        }
      }
      return found;
    };

    const hostOf = (target: Element) =>
      target.closest('.today-item, .growth-node, .conversation-panel, .floating-title, .context-menu, .canvas-tools-popover, .journal-composer, .journal-reader, .journal-list-item, .react-flow__edge')?.getAttribute('class') ?? '';

    const record = (target: Element) => ({
      cls: target.getAttribute('class') ?? target.tagName.toLowerCase(),
      host: hostOf(target),
      ref: target.closest('[data-id]')?.getAttribute('data-id') ?? null,
    });

    document.addEventListener('animationstart', (event) => {
      if (!(event.target instanceof Element)) return;
      const entry: Entry = { name: event.animationName, frames: moving(event.target), ...record(event.target) };
      store.animations.push(entry);
      if (entry.frames.length) store.displacements.push(entry);
      save();
    }, true);

    /*
     * 过渡也要看:reduced motion 下唯一可能"动起来"而不经过关键帧的就是它。
     * `transitionstart` 不冒泡,所以只能挂在捕获阶段。
     *
     * **连时长一起记。** 光记"有一次 transform 过渡"是不够的 —— ReactFlow 自己在
     * fitView 和平移时会给 viewport 与节点写内联的 `transition: transform …`
     * (`@xyflow/react/dist/style.css` 里一条都没有,全是内联的),那不是这一层的东西。
     * 而 reduced motion 下全局那条 `transition-duration: 1ms !important` 会把它压成
     * 瞬时(`!important` 的作者声明压得过内联声明)—— 于是"有没有位移"要看时长,
     * 不看有没有过。
     */
    document.addEventListener('transitionstart', (event) => {
      if (!['transform', 'translate', 'scale'].includes(event.propertyName)) return;
      const target = event.target instanceof Element ? event.target : null;
      store.transformTransitions.push({
        property: event.propertyName,
        cls: target ? record(target).cls : 'unknown',
        duration: target ? getComputedStyle(target).transitionDuration : 'unknown',
      });
      save();
    }, true);
  });
}

async function motionLog(page: Page): Promise<MotionLog> {
  return page.evaluate(() => (window as unknown as { __motion: MotionLog }).__motion);
}

/**
 * 真的会把元素挪一段距离的那些过渡 —— 时长不是瞬时的。
 *
 * reduced motion 下这个必须是空的。允许一条 1ms 的 transform 过渡存在:那是
 * "瞬间到达终点",没有过程;不允许的是任何一条能让人**看见**它在移动的过渡。
 * 时长写成逗号分隔的一串时,只要有一个分量超过 1ms 就算。
 */
function slidingTransitions(log: MotionLog): string[] {
  return log.transformTransitions
    .filter(entry => entry.duration.split(',').some(value => Number.parseFloat(value) > 0.001))
    .map(entry => `${entry.property} @ ${entry.cls} (${entry.duration})`);
}

/** 此刻**还在跑**、而且永远跑不完的动画。环境光算一个,别的都不许有。 */
async function runningInfinite(page: Page): Promise<string[]> {
  return page.evaluate(() => document.getAnimations()
    .filter(animation => animation.playState === 'running')
    .filter(animation => (animation.effect?.getTiming().iterations ?? 1) === Infinity)
    .map(animation => (animation instanceof CSSAnimation ? animation.animationName : '(transition)')));
}

/**
 * 某个容器**里面**此刻还在跑的**关键帧动画**。
 *
 * 只看 `CSSAnimation`,不看过渡:`document.getAnimations()` 把 CSS 过渡也算成
 * `Animation`。而 hover / 按下的过渡是每次点一下都会有的东西(点发送那个按钮的时候
 * 指针正停在它上面,140 毫秒的底色过渡就在跑) —— 把它算进来,这条断言就变成
 * "点完按钮之后什么都没在动",那不是它的意思,而且它会随机地红。
 */
async function runningInside(page: Page, selector: string): Promise<string[] | null> {
  return page.evaluate((sel) => {
    const root = document.querySelector(sel);
    if (!root) return null;
    return document.getAnimations()
      .filter((animation): animation is CSSAnimation => animation instanceof CSSAnimation)
      .filter(animation => animation.playState === 'running')
      .filter(animation => {
        const target = (animation.effect as KeyframeEffect | null)?.target;
        return target instanceof Node && root.contains(target);
      })
      .map(animation => animation.animationName);
  }, selector);
}

// ---------------------------------------------------------------------------------
// 画布:位置、连线、拖动 —— 与 `layout.spec.ts` 同一套读法
// ---------------------------------------------------------------------------------

/** 一个真实空间:根目标下几个同级节点。`ids` 按标题找回真实 UUID。 */
async function scene(page: Page, prefix: string, titles: string[]) {
  const account = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, account.token, `${prefix} 空间`);
  const root = (await getPlan(page, account.token, workspaceId)).nodes[0];
  const ids: Record<string, string> = {};
  for (const title of titles) {
    ids[title] = await createNode(page, account.token, workspaceId, { parentId: root.id, title });
  }
  return { ...account, workspaceId, root: root.id, ids };
}

/** 画布上那个节点**此刻**在哪儿 —— 读的是 ReactFlow 写在 DOM 上的值。 */
async function nodeAt(page: Page, nodeId: string): Promise<{ x: number; y: number }> {
  const style = (await page.locator(`.react-flow__node[data-id="${nodeId}"]`).getAttribute('style')) ?? '';
  const found = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/.exec(style);
  if (!found) throw new Error(`读不出这个节点的位置(style="${style}")`);
  return { x: Number(found[1]), y: Number(found[2]) };
}

const near = (a: number, b: number, tolerance = 1.5) => Math.abs(a - b) < tolerance;

function edgePath(page: Page, edgeId: string): Locator {
  return page.locator(`.react-flow__edge[data-id="${edgeId}"] .react-flow__edge-path`);
}

/** 一条线此刻的粗细与不透明度 —— 它有没有"变清晰"的判据。 */
async function edgeStroke(page: Page, edgeId: string): Promise<{ width: number; opacity: number }> {
  return edgePath(page, edgeId).evaluate(element => {
    const style = getComputedStyle(element);
    return { width: Number.parseFloat(style.strokeWidth), opacity: Number.parseFloat(style.opacity) };
  });
}

interface StoredLayout {
  positions: { nodeId: string; x: number; y: number }[];
  viewports: { scopeNodeId: string; zoom: number; panX: number; panY: number }[];
}

async function storedLayout(page: Page, token: string, workspaceId: string): Promise<StoredLayout> {
  return api<StoredLayout>(page, token, `/api/workspaces/${workspaceId}/layout`);
}

/**
 * 拖动一个节点。
 *
 * 中间那几帧移动不是装饰:一步跳到终点的话,ReactFlow 可能只收到最后一次
 * `pointermove` 而**不认为这是一次拖动** —— 表现是"拖了,但位置没变",
 * 而这个失败长得像"选择器写错了"(见 `layout.spec.ts` 里同一段说明)。
 *
 * `onMidway` 在**指针还没松开**的时候被调一次 —— 那是唯一能看清"拿起来了"
 * 那 2px 的时刻。
 */
async function dragNode(page: Page, nodeId: string, dx: number, dy: number, onMidway?: () => Promise<void>): Promise<void> {
  const node = page.locator(`.react-flow__node[data-id="${nodeId}"]`);
  const box = await node.boundingBox();
  if (!box) throw new Error('拖不动:这个节点不在画布上');
  const start = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  for (const ratio of [0.3, 0.6, 0.85]) {
    await page.mouse.move(start.x + dx * ratio, start.y + dy * ratio);
  }
  if (onMidway) await onMidway();
  await page.mouse.move(start.x + dx, start.y + dy);
  await page.mouse.up();
  await expect(page.getByRole('dialog'), '拖一下节点把详情弹窗也打开了').toHaveCount(0);
}

/**
 * `matrix(a, b, c, d, tx, ty)` 里的 `ty`。拖动抬升那 2px 就靠它读。
 *
 * `none` 是"没有位移",记 0 —— 静止的节点本来就没有 transform,那不是读不出来。
 */
async function liftOf(page: Page, selector: string): Promise<number> {
  const transform = await page.locator(selector).evaluate(element => getComputedStyle(element).transform);
  if (transform === 'none') return 0;
  const found = /matrix\(([^)]+)\)/.exec(transform);
  if (!found) throw new Error(`读不出版换矩阵(transform="${transform}")`);
  return Number(found[1].split(',')[5]);
}

/** 「今天」这一页:一个有工时、排进日程、还没记录的任务。 */
async function todayScene(page: Page, prefix: string) {
  const account = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, account.token, `${prefix} 空间`);
  const root = (await getPlan(page, account.token, workspaceId)).nodes[0];
  await createNode(page, account.token, workspaceId, {
    parentId: root.id,
    title: '整理这周的笔记',
    nodeType: 'task',
    estimateMinutes: 60,
  });
  await scheduleEverything(page, account.token, workspaceId);
  await openSpacePage(page, '/today', workspaceId);
  await expect(page.locator('.focus-card .task-check'), '这一页没有排上可勾选的事 —— 这条测试的前提没成立').toBeVisible();
  return { ...account, workspaceId };
}

// ---------------------------------------------------------------------------------
// 1. reduced motion
// ---------------------------------------------------------------------------------

test('reduced motion:环境光完全静止,全程没有任何位移', async ({ page }) => {
  test.slow();
  // **必须在加载之前设**:加载期就播的那几个(Dock 进场)也要算进来。
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await instrumentAnimations(page);
  const account = await scene(page, 'motion-reduced', ['接受动画的节点']);
  await openSpacePage(page, '/workbench', account.workspaceId);
  await waitForRealPlan(page);

  // (1a) 环境光:两层都不播,连位移值都不在。
  const glow = await page.evaluate(() => {
    const layer = document.querySelector('.ambient-glow');
    if (!layer) return null;
    const before = getComputedStyle(layer, '::before');
    const after = getComputedStyle(layer, '::after');
    return {
      beforeName: before.animationName,
      afterName: after.animationName,
      beforeTransform: before.transform,
      afterTransform: after.transform,
      pointerEvents: getComputedStyle(layer).pointerEvents,
    };
  });
  if (!glow) throw new Error('这一页没有环境光层 —— 下面前提没成立,别把它读成产品有问题');
  expect(glow.beforeName, 'reduced motion 下环境光还在播').toBe('none');
  expect(glow.afterName).toBe('none');
  expect(glow.beforeTransform).toBe('none');
  expect(glow.afterTransform).toBe('none');
  // 它不参与任何交互 —— 一层盖住整页的东西必须有这一条。
  expect(glow.pointerEvents).toBe('none');
  console.log('[motion] reduced-motion 环境光:', JSON.stringify(glow));

  // (1b) 全页:**没有任何一直在动的东西**。
  expect(await runningInfinite(page), 'reduced motion 下还有循环动画在跑').toEqual([]);

  // (1c) 会播动画的那几个动作照走一遍 —— 面板开合、新建节点、Dock 收起再展开。
  await page.locator('.canvas-tools-menu > summary').click();
  const popover = page.locator('.canvas-tools-popover');
  await expect(popover, 'reduced motion 下菜单打不开了 —— 降级不许把功能弄坏').toBeVisible();
  const popoverStyle = await popover.evaluate(element => {
    const style = getComputedStyle(element);
    return { name: style.animationName, duration: style.animationDuration };
  });
  // 面板**仍然淡入**(这是规范 9.1 的要求),只是不再位移。
  expect(popoverStyle.name).toBe('menu-in');
  expect(popoverStyle.duration, '面板动画没有压到 1ms').toBe('0.001s');
  // 淡入仍然淡到看得见(不是"停在第一帧上,永远半透明")。1ms 之后再读,所以要轮询。
  await expect.poll(async () => popover.evaluate(element => getComputedStyle(element).opacity)).toBe('1');
  console.log('[motion] reduced-motion 菜单面板:', JSON.stringify(popoverStyle));

  await popover.getByRole('button', { name: '新建节点' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog, 'reduced motion 下新建表单没打开').toBeVisible();
  await dialog.getByPlaceholder('一个想法、一个行动，或新的方向').fill('reduced motion 下新建的节点');
  // 基线在这里现取,不写字面量:画布上本来就有几个节点,写死的数字会变成一个
  // **一开始就成立**的断言(它自己永远绿,而后面的查找会以"前提没成立"的样子红)。
  const nodesBefore = (await getPlan(page, account.token, account.workspaceId)).nodes.length;
  await dialog.getByRole('button', { name: '新建节点' }).click();
  await expect
    .poll(async () => (await getPlan(page, account.token, account.workspaceId)).nodes.length,
      { message: 'reduced motion 下新建的节点没有进后端' })
    .toBe(nodesBefore + 1);

  await page.getByRole('button', { name: '让对话内容消失' }).click();
  await expect(page.locator('.conversation-overlay')).toBeHidden();
  await page.getByRole('button', { name: '展开对话' }).click();
  await expect(page.locator('.conversation-overlay')).toBeVisible();

  // 画布平移(Dock 的阴影会变、环境动画本来就停了,这里只是把这条路走一遍)。
  const pane = await page.locator('.react-flow__pane').boundingBox();
  if (!pane) throw new Error('画布没渲染出来');
  await page.mouse.move(pane.x + pane.width / 2, pane.y + pane.height - 30);
  await page.mouse.down();
  await page.mouse.move(pane.x + pane.width / 2 - 140, pane.y + pane.height - 110, { steps: 10 });
  await page.mouse.up();

  const log = await motionLog(page);
  const created = log.animations.filter(entry => entry.name === 'node-create');
  expect(created, 'reduced motion 下新建的节点没有淡入').toHaveLength(1);
  expect(created[0].frames, 'reduced motion 下新建节点还做了位移').toEqual([]);
  console.log('[motion] reduced-motion 新建节点:', JSON.stringify(created[0]));

  expect(
    log.displacements.map(entry => `${entry.name} @ ${entry.cls} → ${entry.frames.join(', ')}`),
    'reduced motion 下有一处动画在做位移',
  ).toEqual([]);
  expect(slidingTransitions(log), 'reduced motion 下有一次看得见过程的 transform 过渡').toEqual([]);
  console.log('[motion] reduced-motion 期出现过的 transform 过渡:', JSON.stringify(log.transformTransitions));
  expect(await runningInfinite(page)).toEqual([]);

  // (1d) 被改写成"只改透明度"的那几条关键帧。**读的是 CSSOM**,不是截图 ——
  // 位移是从关键帧里拿掉的,这件事只有在规则里看得见。
  const redefined = await page.evaluate(() => {
    const found: { name: string; declarations: string[] }[] = [];
    const collect = (rules: CSSRuleList) => {
      for (const rule of Array.from(rules)) {
        if (!(rule instanceof CSSKeyframesRule)) continue;
        found.push({
          name: rule.name,
          declarations: Array.from(rule.cssRules).map(frame => (frame as CSSKeyframeRule).style.cssText),
        });
      }
    };
    for (const sheet of Array.from(document.styleSheets)) {
      let rules: CSSRuleList;
      try {
        rules = sheet.cssRules;
      } catch {
        continue; // 跨域的表读不到。这里没有跨域的表。
      }
      for (const rule of Array.from(rules)) {
        if (rule instanceof CSSMediaRule && rule.conditionText.includes('prefers-reduced-motion')) collect(rule.cssRules);
      }
    }
    return found;
  });
  expect(redefined.map(rule => rule.name).sort()).toEqual(['dock-in', 'menu-in', 'node-create', 'panel-drop-in', 'row-in']);
  for (const rule of redefined) {
    expect(rule.declarations.join('; '), `@keyframes ${rule.name} 在 reduced motion 下还带着位移`).not.toMatch(/(^|[;\s])(transform|translate|scale)\s*:/);
  }
  console.log('[motion] reduced-motion 改写过的关键帧:', redefined.map(rule => `${rule.name}(${rule.declarations.join(' ')})`).join(' | '));

  await page.screenshot({ path: artifactPath('motion-reduced-motion.png') });
});

test('reduced motion:勾选仍然换图标和颜色,面板仍然能用', async ({ page }) => {
  test.slow();
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await instrumentAnimations(page);
  const account = await todayScene(page, 'motion-reduced-today');

  // 勾选:图标与颜色照常变(规范 9.1 明确要求的"保留状态区分"),只是不做回弹。
  const check = page.locator('.focus-card .task-check');
  await check.click();
  await expect.poll(async () => (await getToday(page, account.token)).recordedCount, { message: '勾了但库里没有这一条' }).toBe(1);
  await expect(page.locator('.today-item.recorded .task-check svg'), 'reduced motion 下勾选框没有换上图标').toHaveCount(1);
  await expect(page.locator('.today-item.recorded .today-item-result'), 'reduced motion 下完成那一行没有落下来').toContainText('完成了');

  // 随笔输入区:仍然能展开、能输入、草稿仍然在。
  await openSpacePage(page, '/journal', account.workspaceId);
  await page.locator('.journal-compose-trigger').click();
  const composer = page.locator('.journal-composer');
  await expect(composer).toBeVisible();
  expect(await composer.evaluate(element => getComputedStyle(element).animationDuration)).toBe('0.001s');
  await page.getByLabel('此刻的想法').fill('reduced motion 下写的一段');
  await page.getByRole('button', { name: '收起随笔输入框' }).click();
  await page.locator('.journal-compose-trigger').click();
  await expect(page.getByLabel('此刻的想法')).toHaveValue('reduced motion 下写的一段');

  const log = await motionLog(page);
  expect(
    log.displacements.map(entry => `${entry.name} @ ${entry.cls} → ${entry.frames.join(', ')}`),
    'reduced motion 下有一处动画在做位移',
  ).toEqual([]);
  expect(slidingTransitions(log)).toEqual([]);
  expect(await runningInfinite(page)).toEqual([]);
});

// ---------------------------------------------------------------------------------
// 2 / 3. 节点
// ---------------------------------------------------------------------------------

test('普通节点一动不动,新建的那一个只播一次创建动画', async ({ page }) => {
  test.slow();
  await instrumentAnimations(page);
  const account = await scene(page, 'motion-node', ['甲', '乙']);
  await openSpacePage(page, '/workbench', account.workspaceId);
  await waitForRealPlan(page);
  await expect(page.locator('.growth-node')).toHaveCount(3);
  await page.screenshot({ path: artifactPath('motion-workbench-idle.png') });

  // (2) 普通节点上没有任何动画 —— 不是"跑完了",是从来没跑过。
  const running = await page.locator('.growth-node').evaluateAll(nodes =>
    nodes.map(node => getComputedStyle(node).animationName));
  expect(running, '普通节点上挂着动画').toEqual(['none', 'none', 'none']);

  // 先把会让整张图重算的几件事走一遍(hover、选中、取消选中)——
  // 这几步之后画布上**一个**创建动画都不许出现过。
  await page.locator(`.react-flow__node[data-id="${account.ids['甲']}"]`).hover();
  await page.mouse.move(4, 4);
  await page.locator(`.react-flow__node[data-id="${account.ids['乙']}"]`).click();
  await page.keyboard.press('Escape');
  const before = await motionLog(page);
  expect(before.animations.filter(entry => entry.name === 'node-create'), '还没建东西,画布上就有节点在播创建动画').toEqual([]);

  // (3) 真的建一个。
  await page.locator('.canvas-tools-menu > summary').click();
  await page.locator('.canvas-tools-popover').getByRole('button', { name: '新建节点' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await dialog.getByPlaceholder('一个想法、一个行动，或新的方向').fill('刚建的那一个');
  const nodesBefore = (await getPlan(page, account.token, account.workspaceId)).nodes.length;
  await dialog.getByRole('button', { name: '新建节点' }).click();
  await expect
    .poll(async () => (await getPlan(page, account.token, account.workspaceId)).nodes.length,
      { message: '新建的节点没有进后端' })
    .toBe(nodesBefore + 1);

  const plan = await getPlan(page, account.token, account.workspaceId);
  const fresh = plan.nodes.find(node => node.id !== account.root && !Object.values(account.ids).includes(node.id));
  if (!fresh) throw new Error('库里没有多出那个节点 —— 这条测试的前提没成立');

  const after = await motionLog(page);
  const creates = after.animations.filter(entry => entry.name === 'node-create');
  expect(creates, '新建节点没有播创建动画').toHaveLength(1);
  // 动画必须挂在**那个真实节点**上,不是"某个看起来像新的东西"。
  expect(creates[0].ref, '创建动画挂的节点对不上库里那一行').toBe(fresh.id);
  expect(creates[0].frames.join(' '), '创建动画不是"淡入 + 上移 5px"').toContain('5px');
  console.log('[motion] 创建动画:', JSON.stringify(creates[0]));

  // 再重算几次 —— 规范 4.1 点名的就是这一条:一次重渲染不许让所有节点重播。
  await page.locator(`.react-flow__node[data-id="${account.ids['甲']}"]`).hover();
  await page.mouse.move(4, 4);
  await page.locator(`.react-flow__node[data-id="${account.ids['乙']}"]`).click();
  await page.keyboard.press('Escape');
  const again = await motionLog(page);
  expect(again.animations.filter(entry => entry.name === 'node-create'), '重算一次画布,创建动画又播了一遍').toHaveLength(1);
  await expect(page.locator('.growth-node.is-created'), '创建标记没有摘掉 —— 下一次重算它还会再播一次').toHaveCount(0);
});

// ---------------------------------------------------------------------------------
// 4. 连线:只有直接相连的变清晰
// ---------------------------------------------------------------------------------

test('hover 一个节点,只有与它直接相连的线变清晰', async ({ page }) => {
  test.slow();
  const account = await scene(page, 'motion-edge-focus', ['甲', '乙', '丙', '丁']);
  await addRelation(page, account.token, account.workspaceId, account.ids['甲'], account.ids['乙']);
  await addRelation(page, account.token, account.workspaceId, account.ids['丙'], account.ids['丁']);
  await openSpacePage(page, '/workbench', account.workspaceId);
  await waitForRealPlan(page);

  const plan = await getPlan(page, account.token, account.workspaceId);
  /*
   * 「相关」是无向的,后端把两端**按 UUID 排过序**存 —— 所以这里不能按
   * "source 是甲、target 是乙"去找:谁的 UUID 小谁就是 source,而那是随机生成的。
   * 按无序的那一对找,找出来的仍然是**同一条边**(它的 id 是从后端拿的)。
   */
  const isPair = (row: { sourceId: string; targetId: string }, a: string, b: string) =>
    (row.sourceId === a && row.targetId === b) || (row.sourceId === b && row.targetId === a);
  const related = plan.relations.find(row => isPair(row, account.ids['甲'], account.ids['乙']));
  const unrelated = plan.relations.find(row => isPair(row, account.ids['丙'], account.ids['丁']));
  if (!related || !unrelated) throw new Error('建立的两条关系没有从后端回来 —— 这条测试的前提没成立');
  // 父子连线也一起看:它同样是"与这个节点相关的线"。
  const branchOfFirst = `${account.root}-${account.ids['甲']}`;
  const branchOfThird = `${account.root}-${account.ids['丙']}`;
  await expect(edgePath(page, related.id), '这条关系没有画出来').toHaveCount(1);
  await expect(edgePath(page, branchOfFirst)).toHaveCount(1);

  const before = {
    related: await edgeStroke(page, related.id),
    unrelated: await edgeStroke(page, unrelated.id),
    branchOfFirst: await edgeStroke(page, branchOfFirst),
    branchOfThird: await edgeStroke(page, branchOfThird),
  };
  expect(before.related.width).toBeCloseTo(1.6, 1);
  expect(before.branchOfThird.width).toBeCloseTo(1.35, 1);

  const node = page.locator(`.react-flow__node[data-id="${account.ids['甲']}"]`);
  await node.hover();
  await expect
    .poll(async () => (await edgeStroke(page, related.id)).width,
      { message: 'hover 了节点,与它直接相连的那条线没有变清晰' })
    .toBeGreaterThan(before.related.width);

  const hovered = {
    related: await edgeStroke(page, related.id),
    unrelated: await edgeStroke(page, unrelated.id),
    branchOfFirst: await edgeStroke(page, branchOfFirst),
    branchOfThird: await edgeStroke(page, branchOfThird),
  };
  // **不相关的一条一个像素都不许变。** 判断必须走真实 id —— 按标题认的话,
  // 同名的节点会亮错,而界面上看不出来。
  expect(hovered.unrelated, '不相干的那条关系线也变亮了').toEqual(before.unrelated);
  expect(hovered.branchOfThird, '不相干的父子连线也变亮了').toEqual(before.branchOfThird);
  expect(hovered.branchOfFirst.width).toBeGreaterThan(before.branchOfFirst.width);
  // 节点本体在 hover 时一动不动。
  expect(await liftOf(page, `.react-flow__node[data-id="${account.ids['甲']}"] .growth-node`), 'hover 时节点本体动了').toBe(0);
  console.log('[motion] hover 前/后:', JSON.stringify({ before, hovered }));
  await page.screenshot({ path: artifactPath('motion-workbench-node-hover.png') });

  // 指针离开之后回到原样 —— 变清晰是一次状态,不是永久加重。
  await page.mouse.move(4, 4);
  await expect.poll(async () => (await edgeStroke(page, related.id)).width).toBeCloseTo(before.related.width, 1);
});

// ---------------------------------------------------------------------------------
// 5. 拖动:抬升只是抬升,数据一个字不动
// ---------------------------------------------------------------------------------

test('拖动节点:抬升 2px 不渗进坐标,关系一条不多一条不少', async ({ page }) => {
  test.slow();
  const account = await scene(page, 'motion-drag', ['要拖的节点', '不动的那一个']);
  await addRelation(page, account.token, account.workspaceId, account.ids['要拖的节点'], account.ids['不动的那一个']);
  await openSpacePage(page, '/workbench', account.workspaceId);
  await waitForRealPlan(page);

  const relationsBefore = (await getPlan(page, account.token, account.workspaceId)).relations
    .map(row => `${row.relationType}:${row.sourceId}->${row.targetId}`);
  const before = await nodeAt(page, account.ids['要拖的节点']);

  await dragNode(page, account.ids['要拖的节点'], 170, 200, async () => {
    // 指针还按着 —— 这是唯一能看清"拿起来了"那 2px 的时刻。
    await expect(page.locator('.react-flow__node.dragging'), '拖到一半了,ReactFlow 却没有把它标成拖动中').toHaveCount(1);
    expect(await liftOf(page, '.react-flow__node.dragging .growth-node'), '拖动中节点没有抬起来那 2px').toBeCloseTo(-2, 1);
    await page.screenshot({ path: artifactPath('motion-workbench-dragging.png') });
  });

  const dropped = await nodeAt(page, account.ids['要拖的节点']);
  expect(dropped, '拖完位置没变 —— 这条测试的前提没成立,别把它读成产品有问题').not.toEqual(before);

  // 后端那一行要**等于放手的位置**:抬升那 2px 若是画在外层包裹节点上,
  // 这里读到的坐标就会差 2 —— 那是"看起来抬了 2px,实际存下去也偏了 2px"。
  await expect
    .poll(async () => {
      const mine = (await storedLayout(page, account.token, account.workspaceId)).positions
        .find(row => row.nodeId === account.ids['要拖的节点']);
      return mine ? near(mine.x, dropped.x) && near(mine.y, dropped.y) : false;
    }, { message: '拖完之后后端那一行和放手的位置对不上', timeout: 15000 })
    .toBe(true);
  const written = await storedLayout(page, account.token, account.workspaceId);
  expect(written.positions.map(row => row.nodeId), '只有用户真的摆过的那一个才该上传').toEqual([account.ids['要拖的节点']]);

  // 数据没被改写:关系还是那一条,方向没翻。
  const relationsAfter = (await getPlan(page, account.token, account.workspaceId)).relations
    .map(row => `${row.relationType}:${row.sourceId}->${row.targetId}`);
  expect(relationsAfter, '拖动把关系数据改掉了').toEqual(relationsBefore);
});

test('新建关系只画一次,而且用的是后端那条边的 id', async ({ page }) => {
  test.slow();
  await instrumentAnimations(page);
  const account = await scene(page, 'motion-edge-create', ['起点这一边', '终点这一边']);
  await openSpacePage(page, '/workbench', account.workspaceId);
  await waitForRealPlan(page);

  await page.locator('.canvas-tools-menu > summary').click();
  await page.locator('.canvas-tools-popover').getByRole('button', { name: '建立关系' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  // 两个下拉**按位置取**,不按标签文字取:这两个 `<label>` 各自包着整个 `<select>`,
  // 而选项文字里也写着"起点这一边/终点这一边" —— 按文字找会同时命中两个。
  const selects = dialog.getByRole('combobox');
  await selects.nth(0).selectOption({ label: '起点这一边' });
  await selects.nth(1).selectOption({ label: '终点这一边' });
  await dialog.getByRole('button', { name: '建立关系' }).click();

  // 真值在后端:边真的写进去了,那条线才谈得上"画一次"。
  await expect
    .poll(async () => (await getPlan(page, account.token, account.workspaceId)).relations.length,
      { message: '建立的关系没有进后端' })
    .toBe(1);
  const relation = (await getPlan(page, account.token, account.workspaceId)).relations[0];
  await expect(edgePath(page, relation.id), '新建的这条关系没有画出来').toHaveCount(1);

  const log = await motionLog(page);
  const drawn = log.animations.filter(entry => entry.name === 'edge-draw');
  expect(drawn, '新建的连线没有播绘制动画').toHaveLength(1);
  // **动效必须对得上真实 id。** 用标题匹配是规范 4.2 明令禁止的。
  expect(drawn[0].ref, '绘制的不是后端返回的那条边').toBe(relation.id);
  console.log('[motion] 连线绘制:', JSON.stringify(drawn[0]));

  // 画完回到稳定:绘制那个类摘掉,那条路径上不再有任何动画在跑,
  // 而且 `related_to` 自己的虚线样式回来了(`is-drawing` 期间它被让开过)。
  await expect(page.locator('.react-flow__edge-path.is-drawing')).toHaveCount(0);
  expect(await edgePath(page, relation.id).evaluate(element => element.getAnimations().length), '绘制动画结束了,那条线上还有东西在跑').toBe(0);
  // 新边建完就处于选中态(`submitRelationCreate` 会选中它),所以此刻它是最粗、最实的那一条 ——
  // 这不是绘制动画留下的样子,是"用户刚建的就是它"。
  const settled = await edgeStroke(page, relation.id);
  expect(settled.width).toBeCloseTo(2.6, 1);
  expect(settled.opacity).toBeCloseTo(1, 1);
  expect(await edgePath(page, relation.id).evaluate(element => getComputedStyle(element).strokeDasharray)).toContain('6px');
});

// ---------------------------------------------------------------------------------
// 6 / 7. 「今天」的勾选
// ---------------------------------------------------------------------------------

test('勾选完成:写进库之后才播完成反馈', async ({ page }) => {
  test.slow();
  await instrumentAnimations(page);
  const account = await todayScene(page, 'motion-today-ok');
  const check = page.locator('.focus-card .task-check');
  await expect(check).toHaveAttribute('aria-pressed', 'false');

  await check.click();
  // 顺序是**先写库,再播动画** —— 真值在库里那一条。
  await expect.poll(async () => (await getToday(page, account.token)).recordedCount,
    { message: '勾了但库里没有这一条' }).toBe(1);
  const today = await getToday(page, account.token);
  expect(today.items[0].result).toBe('completed');

  const log = await motionLog(page);
  const settles = log.animations.filter(entry => entry.name === 'check-settle');
  expect(settles, '写入成功之后没有播完成反馈').toHaveLength(1);
  expect(settles[0].cls, '完成反馈没有落在那个勾选框上').toContain('task-check');
  expect(log.animations.some(entry => entry.name === 'today-settle'), '文字没有落进完成色').toBe(true);
  console.log('[motion] 完成反馈:', JSON.stringify(log.animations.filter(entry => entry.name === 'check-settle' || entry.name === 'today-settle')));

  // 落定之后:图标在、文案在、"刚刚完成"那个标记已经摘掉。
  await expect(page.locator('.today-item.recorded .task-check svg')).toHaveCount(1);
  await expect(page.locator('.today-item.recorded .today-item-result')).toContainText('完成了');
  await expect(page.locator('.today-item.is-just-completed'), '完成标记没有摘掉,它会一直挂在那儿').toHaveCount(0);
  await page.screenshot({ path: artifactPath('motion-today-completed.png') });
});

test('写入失败:不播完成反馈,也不留下完成的样子', async ({ page }) => {
  test.slow();
  await instrumentAnimations(page);
  const account = await todayScene(page, 'motion-today-fail');

  // 只掐这一次写入。GET /api/today 是读,掐了它验的就变成另一件事了。
  const executions = /\/api\/sessions\/[0-9a-fA-F-]{36}\/executions$/;
  await page.route(executions, route => route.abort());

  const check = page.locator('.focus-card .task-check');
  await check.click();

  // 失败必须**说出来** —— 一个吞掉异常的界面和成功长得一模一样。
  const alert = page.locator('.today-page .turn-error');
  await expect(alert, '写失败了,界面上什么都没说').toBeVisible();
  await expect(alert).toContainText('重试');

  // 而且不许有任何"完成"的样子(包括抖动那一下 —— 这里一个动画都不许有)。
  await expect(check).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('.today-item.recorded')).toHaveCount(0);
  const log = await motionLog(page);
  // 页面上别的动画(列表淡入、Dock 进场)是有的,所以这里**按名字**筛 —— 不看名字的话,
  // 这条断言会变成"这一页什么都没动过",那不是它的意思。
  expect(
    log.animations.filter(entry => entry.name === 'check-settle' || entry.name === 'today-settle'),
    '写失败了却播了完成反馈',
  ).toEqual([]);
  expect((await getToday(page, account.token)).recordedCount, '这条测试要的是"没写进去",可它写进去了').toBe(0);

  await page.unroute(executions);
});

// ---------------------------------------------------------------------------------
// 8 / 9. AI Dock
// ---------------------------------------------------------------------------------

test('AI 空闲时没有「正在思考」;规则兜底不冒充真实模型', async ({ page }) => {
  test.slow();
  await instrumentAnimations(page);
  const account = await scene(page, 'motion-ai', ['讨论用的节点']);
  await openSpacePage(page, '/workbench', account.workspaceId);
  await waitForRealPlan(page);

  // (8) 空闲:Dock 里一个动画都没有,更不会有那个状态标识。
  await expect(page.locator('.conversation-overlay')).toBeVisible();
  await expect(page.locator('.ai-thinking'), 'AI 没在跑,却挂着「正在思考」').toHaveCount(0);
  await expect
    .poll(async () => runningInside(page, '.conversation-overlay'),
      { message: 'Dock 空闲时还有动画在跑' })
    .toEqual([]);

  /*
   * 把这一轮请求按住,才看得清"在飞的时候"长什么样。
   *
   * **这不是"等一会儿"**:等的是测试自己放行,没有任何固定时长 ——
   * 拿一个 `waitForTimeout` 去撞那 300 毫秒的窗口,是在赌。
   */
  let release = () => {};
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/workspaces/*/messages', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    await gate;
    await route.continue();
  });

  await page.getByLabel('给 AI 的消息').fill('我这周想先把笔记整理完。');
  await page.getByRole('button', { name: '发送消息' }).click();

  const thinking = page.locator('.floating-title .ai-thinking');
  await expect(thinking, '发送中却没有那个克制的状态标识').toBeVisible();
  await expect(thinking).toContainText('正在思考');
  // 顶部**只有一个**状态动画,而且不是旋转、不是跳点、不是整块面板发光。
  expect(await runningInside(page, '.conversation-overlay'), 'Dock 里同时跑着不止一个状态动画').toEqual(['ai-line']);
  await page.screenshot({ path: artifactPath('motion-ai-thinking.png') });

  release();

  // (9) 回了:标识收掉,来源徽标如实说明它不是模型想出来的。
  const badge = page.locator('.message.assistant .source-badge');
  await expect(badge).toBeVisible({ timeout: 20000 });
  await expect(page.locator('.ai-thinking'), '回复到了,标识还挂着').toHaveCount(0);
  const text = (await badge.innerText()).replace(/\s+/g, ' ');
  expect(text, '规则兜底的回复被说成了模型生成的').not.toContain('AI 规划');
  expect(text).toContain('本地规则');
  expect(text).toContain('模型不可用');
  expect(await badge.getAttribute('class'), '兜底没有标成降级').toContain('degraded');
  await expect.poll(async () => runningInside(page, '.conversation-overlay')).toEqual([]);
  console.log('[motion] 回复来源徽标:', text);
});

// ---------------------------------------------------------------------------------
// 10. 随笔草稿
// ---------------------------------------------------------------------------------

test('随笔:面板用统一令牌,收起一次不清草稿', async ({ page }) => {
  const account = await registerAccount(page, 'motion-journal');
  const workspaceId = await createWorkspace(page, account.token, '随笔动效空间');
  await openSpacePage(page, '/journal', workspaceId);
  await expect(page.getByLabel('此刻的想法')).toHaveCount(0);

  await page.locator('.journal-compose-trigger').click();
  const composer = page.locator('.journal-composer');
  await expect(composer).toBeVisible();
  const style = await composer.evaluate(element => {
    const computed = getComputedStyle(element);
    return { name: computed.animationName, duration: computed.animationDuration };
  });
  expect(style.name, '输入区展开没走统一的面板动画').toBe('panel-drop-in');
  expect(style.duration, '面板动画的时长不是 --motion-panel').toBe('0.26s');
  console.log('[motion] 随笔输入区:', JSON.stringify(style));

  const draft = '第一行\n第二行:这段还没有发布。';
  await page.getByLabel('此刻的想法').fill(draft);
  await page.screenshot({ path: artifactPath('motion-journal-expanded.png') });

  await page.getByRole('button', { name: '收起随笔输入框' }).click();
  await expect(page.getByLabel('此刻的想法')).toHaveCount(0);
  await expect(page.locator('.journal-compose-trigger')).toContainText('继续刚才未发布的文字');
  await page.locator('.journal-compose-trigger').click();
  // 随笔还没有持久化方案(那是另一件事),所以"收起不清空"是这里唯一能给的保证。
  await expect(page.getByLabel('此刻的想法'), '收起一次就把没发布的草稿清掉了').toHaveValue(draft);
});

// ---------------------------------------------------------------------------------
// 11. 菜单与焦点
// ---------------------------------------------------------------------------------

test('菜单动画不影响焦点:开落在菜单里,关回到触发器', async ({ page }) => {
  const account = await scene(page, 'motion-menu', ['打开菜单的节点']);
  await openSpacePage(page, '/workbench', account.workspaceId);
  await waitForRealPlan(page);

  const more = page.locator(`.react-flow__node[data-id="${account.ids['打开菜单的节点']}"] .node-more`);
  await more.click();
  const menu = page.locator('.context-menu');
  await expect(menu).toBeVisible();
  const style = await menu.evaluate(element => {
    const computed = getComputedStyle(element);
    return { name: computed.animationName, duration: computed.animationDuration };
  });
  expect(style.name, '菜单进场没走统一的面板动画').toBe('menu-in');
  expect(style.duration).toBe('0.26s');

  // 进场动画不许延误焦点:菜单一开,焦点就得在菜单里,否则方向键和 Esc 全是空的。
  expect(
    await page.evaluate(() => Boolean(document.activeElement && document.activeElement.closest('.context-menu'))),
    '菜单开了,焦点却没进去 —— 方向键和 Esc 都会失灵',
  ).toBe(true);
  await expect(more).toHaveAttribute('aria-expanded', 'true');

  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
  expect(
    await page.evaluate(() => (document.activeElement ? document.activeElement.getAttribute('aria-label') : null)),
    '关掉菜单之后焦点没有回到那个按钮',
  ).toBe('打开菜单的节点的更多操作');
  await expect(more).not.toHaveAttribute('aria-expanded', 'true');
});

// ---------------------------------------------------------------------------------
// 12. 横向溢出
// ---------------------------------------------------------------------------------

test('桌面与 390px 都没有横向溢出', async ({ page }) => {
  test.slow();
  const account = await scene(page, 'motion-overflow', ['一个节点']);
  const pages: [string, string][] = [['/workbench', '工作台'], ['/journal', '随笔'], ['/today', '首页']];
  for (const [path, label] of pages) {
    await openSpacePage(page, path, account.workspaceId);
    if (path === '/workbench') await waitForRealPlan(page);
    const desktop = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
    }));
    expect(desktop.scroll, `${label}在桌面宽度下横向溢出了`).toBeLessThanOrEqual(desktop.viewport);

    await page.setViewportSize({ width: 390, height: 844 });
    const mobile = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
    }));
    expect(mobile.scroll, `${label}在 390px 下横向溢出了`).toBeLessThanOrEqual(mobile.viewport);
    if (path === '/workbench') await page.screenshot({ path: artifactPath('motion-mobile-workbench.png') });
    await page.setViewportSize({ width: 1440, height: 960 });
  }
});
