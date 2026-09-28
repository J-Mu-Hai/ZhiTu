'use client';
import { useCallback, useSyncExternalStore } from 'react';

/**
 * 视口断点。
 *
 * ## 为什么不能直接用 `window.matchMedia(...)` 取值
 *
 * 服务端渲染时 `window` 不存在,`useState(() => innerWidth < 480)` 这类写法在服务端
 * 拿到的是另一个值 —— 水合(把服务端画的 HTML 接到客户端的 React 上)时两边对不上,
 * React 会报水合不一致。
 *
 * `useSyncExternalStore` 就是为"浏览器里才有、但渲染要用"的值准备的:它先拿
 * `getServerSnapshot`(在服务端和**水合那一次**渲染都用它,所以和 HTML 一致),
 * 水合完成后立刻按 `getSnapshot` 的真实值再渲染一次。于是没有水合警告,
 * 也不会留下一个"服务端说是、客户端说不是"的中间态。
 *
 * 第三个参数的 `false` 是刻意的:断点全部写成 `max-width`,"服务端那一档应该是宽屏
 * 还是窄屏"没有答案,但**宽屏是更保守的那个默认** —— 窄屏那一档做的事是"把东西收起来"
 * (收起面板、只留图标),先按没收的样子画、再收,比先收起来再展开少一次跳动。
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback((notify: () => void) => {
    const list = window.matchMedia(query);
    list.addEventListener('change', notify);
    return () => list.removeEventListener('change', notify);
  }, [query]);
  const getSnapshot = useCallback(() => window.matchMedia(query).matches, [query]);
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}

/**
 * 手机工作台那一档。
 *
 * 取 480 而不是复用已有的 720:720 那一档里,顶部浮动组件是**竖排两层**
 * (面包屑一行、视图切换一行),那是给"还有余量的窄屏"留的;480 以下才是真正
 * 要按手机重做的那一档 —— 一行浮动组件、画布优先、AI 面板改成底部抽屉。
 * 两档的分工写在这里,免得下次有人把断点合并。
 */
const MOBILE_QUERY = '(max-width:480px)';

export function useMobileLayout(): boolean {
  return useMediaQuery(MOBILE_QUERY);
}
