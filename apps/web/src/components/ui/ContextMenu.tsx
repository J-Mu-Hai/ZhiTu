'use client';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

/**
 * 右键菜单。**这一版全仓第一个**,没有可复用的旧件,所以把几个"为什么是这样"写在这里。
 *
 * ## 为什么不用 `Dialog.tsx`
 *
 * 那个组件调的是 `showModal()`,于是它自带四条我们**一条都不要**的性质:它是模态、
 * 它锁焦点、它挡住外部指针、它走 top layer。菜单要的正好相反 —— 点别处就关、不锁焦点、
 * 不接管页面。硬套那个组件的话,这四个性质得逐个关掉,而关不干净的表现是"菜单开着的时候
 * 画布点不动",且原因看起来和菜单毫无关系。
 *
 * ## 为什么挂在 `document.body` 上
 *
 * `.react-flow__viewport` 带着一个 CSS transform(平移与缩放),而画布本身有裁剪。
 * 就地渲染的菜单会**跟着画布一起缩放**(放大到 2 倍时菜单里的字也大两倍),
 * 挪到节点附近时还会被画布的边缘切掉一半。挂到 body 上,这两件事都不成立 ——
 * 代价是定位要用**屏幕坐标**,由调用方把指针的 `clientX/clientY` 传进来。
 *
 * ## 为什么键盘事件要 `stopPropagation`
 *
 * React 的事件是沿**组件树**冒泡的,不是沿 DOM 树 —— 所以 portal 到 body 里的菜单,
 * 它的事件仍然会冒到 ReactFlow 那一层。而 ReactFlow 在节点被选中时会接管方向键
 * (用来移动选中的节点),不拦的话,用户在菜单里按上下键,**画布会跟着平移**。
 *
 * 拦在菜单自己身上,而不是用 ReactFlow 的 `disableKeyboardA11y` 图省事:那个开关是
 * 全局的,关掉它连 ReactFlow 自己的节点键盘支持(用键盘选中节点、移动节点)一起没了 ——
 * 为了修菜单而弄坏画布的键盘可用性,是把一个问题换成两个。
 */

export interface ContextMenuItem {
  key: string;
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  /**
   * 不可选。
   *
   * **`disabledReason` 必须一起给。** 只禁用不说明,用户看到的是一个灰掉的选项,
   * 而他没有任何办法知道为什么 —— 那和"这个功能不存在"在他眼里是一样的。
   * 根目标的归档就是这个情况:它本来就不该被删,而这句话要说出来。
   */
  disabled?: boolean;
  disabledReason?: string;
}

export interface ContextMenuState {
  x: number;
  y: number;
  items: ContextMenuItem[];
  /**
   * 关掉菜单之后焦点回到哪。
   *
   * 键盘用户按 `Shift+F10` 打开菜单,`Esc` 之后如果焦点掉到 `body`,他下一次按键
   * 就落在了空处 —— 要重新 Tab 一长串才能回到刚才那个按钮。这里存的是**开菜单时**
   * 的那个元素,关的时候还给他。
   */
  restoreFocusTo?: HTMLElement | null;
}

/** 菜单边缘与边界之间留的余量,免得贴着边看起来像被切掉。 */
const EDGE_GAP = 4;

interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/**
 * 可用范围 = **窗口 ∩ 画布容器**。
 *
 * 两个都要:只按窗口夹紧,菜单会跑到工具栏或侧栏上面去;只按画布夹紧,画布比窗口大时
 * (它确实经常比窗口大)菜单会被放进窗口之外。
 */
function usableBox(boundary?: HTMLElement | null): Box {
  const win: Box = {
    left: 0,
    top: 0,
    right: window.innerWidth,
    bottom: window.innerHeight,
  };
  if (!boundary) return win;
  const rect = boundary.getBoundingClientRect();
  return {
    left: Math.max(win.left, rect.left),
    top: Math.max(win.top, rect.top),
    right: Math.min(win.right, rect.right),
    bottom: Math.min(win.bottom, rect.bottom),
  };
}

export function ContextMenu({
  state,
  onClose,
  boundary,
}: {
  state: ContextMenuState;
  onClose: () => void;
  /** 夹紧用的容器。通常传画布那一层。不传就只按窗口夹。 */
  boundary?: HTMLElement | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const { x, y, items, restoreFocusTo } = state;
  /** 第一个可选项的下标。全禁用时是 -1(那时焦点给菜单容器本身)。 */
  const firstEnabled = items.findIndex(item => !item.disabled);
  const [active, setActive] = useState(firstEnabled);

  /*
   * 定位 → 显形 → 聚焦。**三件事必须在同一个 layout effect 里按这个顺序做完。**
   *
   * ## 为什么不是一个 state 分两步走
   *
   * 这里原来是 `setPlacement({ ready: true })` 配一条 `style={{ visibility: ready ?
   * 'visible' : 'hidden' }}`,另有一个 `useEffect` 负责聚焦。**那套是坏的,而且坏得很安静:**
   * 聚焦那个 effect 在 `visibility: hidden` 的那一帧就跑了,而**隐藏元素是不可聚焦的**
   * —— `.focus()` 是一次空操作,不报错、不抛异常。
   *
   * 后果是整套键盘支持一次都没生效过:打开菜单后焦点要么留在触发器上、要么掉到
   * `body`(点空白处右键那条路),于是 `ArrowDown` / `Home` / `End` 全部没反应,
   * **`Esc` 也关不掉菜单** —— 因为 keydown 根本没冒到菜单这一层。三条路(按钮、
   * 节点右键、空白右键)都中,只是"焦点恰好留在上一份菜单里"会让其中一条看起来是好的。
   *
   * 直接写 DOM 而不是走 state,是因为走 state 就必然多一次渲染,而"元素已经显形"
   * 与"该聚焦了"之间就又出现一个窗口。`visibility: hidden` 的元素**有布局**
   * (量得到宽高),所以在隐藏状态下量、定好位、再显形,一帧之内可以全部做完,
   * 浏览器不会画出一个没定位的菜单。
   *
   * 因此这个组件**不传 `style` 属性**:left/top/visibility 只由下面这段写,
   * React 重渲染(比如方向键改了 `active`)不会把它们抹回去。
   */
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const box = usableBox(boundary);
    let left = x;
    let top = y;
    // 越界就翻到指针另一侧。翻转比夹紧好:夹紧会让菜单盖住指针,而用户刚点的东西
    // 就在指针底下 —— 菜单盖住它,他反而看不到自己点的是哪儿。
    if (left + rect.width > box.right) left = x - rect.width;
    if (top + rect.height > box.bottom) top = y - rect.height;
    // 翻完还越界(菜单比可用范围还大),再夹紧 —— 这时至少保证左上角可见。
    left = Math.min(Math.max(left, box.left + EDGE_GAP), Math.max(box.left + EDGE_GAP, box.right - rect.width - EDGE_GAP));
    top = Math.min(Math.max(top, box.top + EDGE_GAP), Math.max(box.top + EDGE_GAP, box.bottom - rect.height - EDGE_GAP));
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    el.style.visibility = 'visible';
    // **显形之后才聚焦。** 顺序反了的话上面那一整段注释说的就是这个 bug。
    const buttons = el.querySelectorAll<HTMLElement>('[role="menuitem"]');
    (firstEnabled >= 0 ? buttons[firstEnabled] : el)?.focus();
  }, [x, y, boundary, items.length, firstEnabled]);

  /*
   * 菜单项被禁用时**也让它的原因能被键盘读到**。
   *
   * 焦点在菜单里时按 `Tab` 会走出菜单(菜单不锁焦点,这是有意的 —— 见文件头),
   * 所以原因必须挂在每一项自己的 `title` 上,而不能指望用户先聚焦到它。
   * (这一条不写代码,只是提醒:`title` 已经在下面每一项上设好了。)
   */

  const close = useCallback(() => {
    onClose();
    // 还焦点。放在 `onClose` 之后:关闭会卸载菜单,而卸载之后再去 focus 一个已经
    // 不在文档里的元素是空操作 —— 顺序反了的话,键盘用户按完 Esc 焦点就没了。
    restoreFocusTo?.focus?.();
  }, [onClose, restoreFocusTo]);

  /*
   * 触发器上的 `aria-expanded`。
   *
   * `aria-haspopup="menu"` 只说了"这个按钮会开出一个菜单",**没说它现在开着**。
   * 屏幕阅读器用户按完那个按钮之后,再读到的还是同一句话 —— 他没法知道菜单到底开没开。
   * 而这个状态只有菜单自己知道,所以由它来写,和"关掉之后把焦点还回去"是同一件事的两面。
   *
   * 直接写 DOM 而不是让调用方传一个 prop:`.node-more` 住在 `PathView` 那份**整张图的
   * memo** 里(见那里的说明:为菜单开合重算整张图不值当),而 React 不会动它没有
   * 声明过的属性,所以这次写入不会被下一次渲染抹掉。
   *
   * 右键那条路没有触发器(`restoreFocusTo` 是 null),也就没有东西需要标成展开的。
   */
  useEffect(() => {
    if (!restoreFocusTo) return;
    restoreFocusTo.setAttribute('aria-expanded', 'true');
    return () => restoreFocusTo.removeAttribute('aria-expanded');
  }, [restoreFocusTo]);

  // 点别处关掉。用 `pointerdown` 而不是 `click`:后者要等指针抬起,而拖动会把它吃掉。
  useEffect(() => {
    function onPointerDown(event: PointerEvent) {
      if (ref.current?.contains(event.target as Node)) return;
      onClose();
    }
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [onClose]);

  function onKeyDown(event: React.KeyboardEvent) {
    // 见文件头:不拦的话方向键会去平移画布。
    event.stopPropagation();
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    const buttons = Array.from(ref.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    if (buttons.length === 0) return;
    const current = buttons.findIndex(button => button === document.activeElement);
    // 禁用项**跳过**,但不禁用就不给选——`Tab` 到它上面是允许的,因为 `disabledReason`
    // 要靠 title 读出来。所以这里只挑能选的那些来移动。
    const enabled = buttons.map((button, index) => (button.getAttribute('aria-disabled') === 'true' ? -1 : index)).filter(index => index >= 0);
    if (enabled.length === 0) return;
    const here = enabled.indexOf(current);
    let next: number | null = null;
    if (event.key === 'ArrowDown') next = enabled[(here + 1 + enabled.length) % enabled.length];
    else if (event.key === 'ArrowUp') next = enabled[(here - 1 + enabled.length) % enabled.length];
    else if (event.key === 'Home') next = enabled[0];
    else if (event.key === 'End') next = enabled[enabled.length - 1];
    if (next === null) return;
    event.preventDefault();
    buttons[next]?.focus();
    setActive(next);
  }

  return createPortal(
    <div
      ref={ref}
      className="context-menu"
      role="menu"
      tabIndex={-1}
      aria-label="节点操作"
      onKeyDown={onKeyDown}
      // 菜单上的右键不该再冒到画布去开第二个菜单。
      onContextMenu={event => event.preventDefault()}
      /*
       * **这里不传 `style`,而且不能传。**
       *
       * left/top/visibility 三样都由上面那个 layout effect 直接写 DOM。一旦这里也
       * 给一个 style,React 就重新成为这三样的"主人",而我们自己那次
       * `el.style.visibility = 'visible'` 就会在下一次重渲染(方向键改 `active`)
       * 时被它按旧值抹回去 —— 菜单会在用户按第一次方向键时**整个消失**。
       *
       * 初始的 `visibility: hidden` 因此写在 `.context-menu` 的 CSS 里
       * (`dark-theme.css`),和左边那两条来自同一个地方。
       */
    >
      {items.map((item, index) => (
        <button
          key={item.key}
          type="button"
          role="menuitem"
          className={`context-menu-item${item.disabled ? ' is-disabled' : ''}`}
          // roving tabIndex:菜单里只有一个可 Tab 到的项,其余靠方向键走。
          tabIndex={index === active ? 0 : -1}
          aria-disabled={item.disabled ? true : undefined}
          title={item.disabled ? item.disabledReason : undefined}
          onClick={() => {
            if (item.disabled) return;
            onClose();
            item.onSelect();
          }}
        >
          {item.icon ? <span className="context-menu-icon" aria-hidden="true">{item.icon}</span> : null}
          <span className="context-menu-label">{item.label}</span>
          {/*
            禁用原因**印出来**,不只在 title 里。title 要悬停几百毫秒才出现,而且触屏
            根本没有悬停 —— 而这个产品明确要保留触屏入口。
          */}
          {item.disabled && item.disabledReason ? (
            <span className="context-menu-why">{item.disabledReason}</span>
          ) : null}
        </button>
      ))}
    </div>,
    document.body,
  );
}
