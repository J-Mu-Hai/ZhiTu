/**
 * 画布上的**边**怎么被精确点到。
 *
 * ## 为什么需要它:包围盒的中心不在笔画上
 *
 * 这条边是曲线(或平滑折线),而 `boundingBox()` 给的是**包围**这条曲线的那个矩形 ——
 * 它的中心往往落在曲线的外侧,也就是**画布空白**上。拿它当"点在线上"用,双击打中的
 * 是 `.react-flow__pane`,于是"双击边不该建节点"这条断言会以"双击边建出了节点"的
 * 形式红,而**产品其实是对的**。这个坑真实发生过一次(见 `canvas-create.spec.ts` 的
 * 验收 2),失败信息还指向产品。
 *
 * 所以判据换成**浏览器自己的命中测试**:沿路径取样,取第一个命中元素属于这条边的点。
 * 一个都找不到就报"前提不成立",**不退回包围盒中心** —— 退回去就是把上面那个坑
 * 重新挖一遍。
 *
 * 两个文件要用(验收 2 与"双击边什么都不开"),所以它成模块。
 */

import type { Page } from '@playwright/test';

/** 找一个真的落在边上的点(屏幕坐标)。找不到就抛 —— 那是测试的前提塌了,不是产品坏了。 */
export async function pointOnEdge(page: Page): Promise<{ x: number; y: number }> {
  const point = await page.evaluate(() => {
    const path = document.querySelector('.react-flow__edge path.react-flow__edge-path') as SVGPathElement | null;
    const svg = path?.ownerSVGElement;
    const matrix = path?.getScreenCTM();
    if (!path || !svg || !matrix) return null;
    const length = path.getTotalLength();
    for (let fraction = 0.15; fraction <= 0.86; fraction += 0.05) {
      const local = path.getPointAtLength(length * fraction);
      const cursor = svg.createSVGPoint();
      cursor.x = local.x;
      cursor.y = local.y;
      const screen = cursor.matrixTransform(matrix);
      // `elementFromPoint` 是浏览器真正的命中测试。线上命中的是
      // `.react-flow__edge-interaction`(那条 20 像素宽的透明带),不是画布。
      if (document.elementFromPoint(screen.x, screen.y)?.closest('.react-flow__edge')) {
        return { x: screen.x, y: screen.y };
      }
    }
    return null;
  });
  if (!point) throw new Error('这条边上找不到一个真的命中它的点 —— 这个断言的前提不成立');
  return point;
}
