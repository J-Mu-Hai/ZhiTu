import type { V01TimelineItemView } from '@/lib/backend';
import type { GrowthNode, GrowthState } from '@/types/growth';
import { isInSpace } from './selectors';

export type ZoomLevel = 'year' | 'quarter' | 'month' | 'week' | 'day';
export const zoomPresets: Record<ZoomLevel, number> = { year: .55, quarter: 1.5, month: 4, week: 20, day: 90 };
export const zoomLabels: Record<ZoomLevel, string> = { year: '年', quarter: '季度', month: '月', week: '周', day: '天' };
export const dayNumber = (date: string) => Date.parse(`${date}T00:00:00Z`) / 86400000;
export const dateString = (day: number) => new Date(Math.round(day) * 86400000).toISOString().slice(0, 10);
export const dateToX = (day: number, start: number, pixelsPerDay: number) => (day - start) * pixelsPerDay;
export const xToDate = (x: number, start: number, pixelsPerDay: number) => start + x / pixelsPerDay;
export const zoomLevelFor = (density: number): ZoomLevel => density < .9 ? 'year' : density < 2.5 ? 'quarter' : density < 12 ? 'month' : density < 55 ? 'week' : 'day';
export function anchoredZoom(start: number, oldDensity: number, newDensity: number, anchorX: number) {
  return xToDate(anchorX, start, oldDensity) - anchorX / newDensity;
}
/**
 * 今天的日期字符串,**按浏览器所在时区算**。
 *
 * 这里以前是一个写死的常量 `DEMO_TODAY = '2026-09-16'`,它同时充当时间线的"今天"
 * 竖线、"今天"按钮的目标、以及 Home 键的落点。它带来的问题不是"日期不准",而是
 * **整条时间线以一个固定的过去时刻为锚** —— 用户打开工作时,那条"今天"的线画在
 * 三个月前,按"今天"也是跳到那里。而界面上没有任何东西提示这个日期是不真实的。
 *
 * 不能用 `toISOString().slice(0,10)`:那是 UTC 的"今天"。东八区早上 8 点之前,
 * UTC 还停在前一天 —— 用户会在"今天"这条线上看到昨天。
 */
export function todayInTimeZone(now = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(now);
  const value = (type: string) => parts.find(part => part.type === type)?.value ?? '';
  return `${value('year')}-${value('month')}-${value('day')}`;
}

/**
 * `date` 所在那一周的**周一和周日**(含两端),同样是本地日期。
 *
 * 任务视图的"本周"以前是写死的两个字符串 `'2026-09-14'` / `'2026-09-20'` —— 那
 * 是一周,而且是**某一周**。过了那一周之后,"本周"这个筛选项永远返回空列表,
 * 而按钮本身看起来一切正常:用户会以为这周真的没任务。
 *
 * 一律按周一开头算,与中文界面里"本周"的默认理解一致(周日开头的 `getDay()` 会
 * 把周日算成下一周的末尾)。
 */
export function weekBounds(date: string): { start: string; end: string } {
  const day = dayNumber(date);
  const mondayOffset = (new Date(day * 86400000).getUTCDay() + 6) % 7;
  return { start: dateString(day - mondayOffset), end: dateString(day - mondayOffset + 6) };
}

export interface TimelineItem {
  node: GrowthNode;
  start: number;
  end: number;
  kind: 'event' | 'duration' | 'milestone' | 'goal';
  derived: boolean;
  /**
   * 这条投影属于哪条轨道。
   *
   * - `phase`:粗时间架构的阶段条(月/季/年尺度主表现);
   * - `week`:已确认的“本周计划 / 下周预览”周条(周尺度);
   * - `day` :已确认的日工作块(日尺度)。
   *
   * 轨道只决定画在哪一行,不影响数据真值。`undefined` 按 `phase` 处理。
   */
  track?: 'phase' | 'week' | 'day';
  /**
   * 同一条投影在 React 列表里的稳定 id。
   *
   * 一个节点的**多场日工作块**必须各自一条(不能都拿 `node.id` 当 key),所以投影
   * 自带一个;`undefined` 时回退到 `node.id`。
   */
  itemId?: string;
}

export function timelineItems(growth: GrowthState, spaceId: string): TimelineItem[] {
  return Object.values(growth.nodes).filter(n => isInSpace(growth, n.id, spaceId) && n.type !== 'capability').flatMap(node => {
    let start = node.startDate ?? node.scheduledDate;
    let end = node.endDate ?? start;
    const derived = !start;
    if (!start && ['goal', 'stage'].includes(node.type)) {
      const children = Object.values(growth.nodes).filter(n => n.id !== node.id && isInSpace(growth, n.id, node.id));
      const dates = children.flatMap(n => [n.startDate, n.endDate, n.scheduledDate].filter((d): d is string => !!d)).sort();
      start = dates[0]; end = dates.at(-1);
    }
    if (!start || !end || !Number.isFinite(dayNumber(start)) || !Number.isFinite(dayNumber(end))) return [];
    return [{ node, start: dayNumber(start), end: Math.max(dayNumber(start), dayNumber(end)), derived,
      kind: node.type === 'goal' ? 'goal' : node.type === 'milestone' ? 'milestone' : start === end ? 'event' : 'duration' } as TimelineItem];
  });
}
/**
 * 一段时间线上**画不出来**的节点。
 *
 * `timelineItems` 只返回有日期可画的节点:既没有 `startDate`/`scheduledDate`,也不是
 * "能从子节点推出一个范围"的目标或阶段 —— 剩下的直接被丢掉。真实空间里这很常见:
 * 模型建了一批任务,只给其中几个填了截止日,其余的在时间线上**凭空消失**。
 *
 * 这和"画错"是同一件事的两面。用户在任务视图里看到 11 项、时间线上只有 3 项,除了
 * 自己数一遍没有任何办法知道少了什么 —— 而数不出来的时候,他会以为计划就是那 3 项。
 * 所以这里把它们找出来,让界面能如实说一句"这 8 项还没有排期"。
 *
 * 判据直接取 `timelineItems` 的**结果差集**,而不是重写一遍日期推导规则:后者会在
 * `timelineItems` 变化时悄悄分叉,于是"画不出来的节点"和"这里列出来的节点"又对不上。
 */
export function unscheduledNodes(growth: GrowthState, spaceId: string): GrowthNode[] {
  const drawable = new Set(timelineItems(growth, spaceId).map(item => item.node.id));
  return Object.values(growth.nodes)
    .filter(node => isInSpace(growth, node.id, spaceId) && node.type !== 'capability' && !drawable.has(node.id))
    .sort((a, b) => a.title.localeCompare(b.title, 'zh'));
}

export function getVisibleItems(items: TimelineItem[], level: ZoomLevel) {
  return items.filter(({ node }) => {
    const detail = node.timelineLevel ?? 'task';
    if (level === 'year') return node.type === 'goal' || (node.type === 'milestone' && detail === 'major');
    if (level === 'quarter') return node.type === 'stage' || node.type === 'milestone' || detail === 'major';
    if (level === 'month') return node.type === 'milestone' || (node.type === 'task' && detail !== 'action');
    if (level === 'week') return node.type === 'task';
    return node.type === 'task' && detail === 'action';
  });
}
export type TimelineAxisMode = 'dated' | 'relative';

export interface DraftTimeline {
  items: TimelineItem[];
  mode: TimelineAxisMode;
}

/**
 * 把 V1/V0.1 的 `v01Timeline` 草案适配进**既有主轴**的 `TimelineItem[]`。
 *
 * 相对周不会伪造真实日期:第 N 周映射到合成日轴 `anchorDay + (N-1)*7`,刻度由
 * `weekTicks` 显示“第 N 周”。有日期时才用真实日期。草案阶段卡片用合成 `GrowthNode`
 * 承载标题/摘要/状态,布局仍走 `layoutItems`(碰撞避让、上下分轨)。
 */
export function draftTimelineItems(
  draft: V01TimelineItemView[],
  anchorDay: number,
): DraftTimeline {
  const dated = draft.some(item => Boolean(item.startDate && item.endDate));
  const items: TimelineItem[] = [];
  for (const item of draft) {
    let start: number;
    let end: number;
    if (dated) {
      if (!item.startDate || !item.endDate) continue;
      start = dayNumber(item.startDate);
      end = Math.max(start, dayNumber(item.endDate));
    } else {
      const startWeek = item.startWeek ?? 1;
      const endWeek = item.endWeek ?? startWeek;
      start = anchorDay + (startWeek - 1) * 7;
      end = anchorDay + endWeek * 7; // 含端点
    }
    if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
    const node: GrowthNode = {
      id: item.id,
      title: item.title,
      type: 'stage',
      status: item.status === 'planned' ? 'doing' : 'pending',
      priority: 'medium',
      planningLevel: 'phase',
      timelineLevel: 'major',
      startDate: dateString(Math.floor(start)),
      endDate: dateString(Math.round(end)),
      description: item.deliverable || item.goal,
    };
    items.push({ node, start, end, kind: 'duration', derived: false, track: 'phase', itemId: item.id });
  }
  return { items, mode: dated ? 'dated' : 'relative' };
}

/**
 * 相对周刻度。**按缩放稀疏显示**,不把 24 个周标签压在同一行重叠。
 * 与 `draftTimelineItems` 共用 `anchorDay`:第 N 周落在 `anchorDay + (N-1)*7`。
 */
export function weekTicks(
  anchorDay: number,
  start: number,
  end: number,
  density: number,
): Tick[] {
  const ticks: Tick[] = [];
  const startWeek = Math.max(1, Math.floor((start - anchorDay) / 7) + 1);
  const endWeek = Math.max(startWeek, Math.ceil((end - anchorDay) / 7) + 1);
  const pixelsPerWeek = 7 * density;
  // 每 ~64px 才放一个标签;缩得越小,间隔越大。
  const step = Math.max(1, Math.ceil(64 / Math.max(1, pixelsPerWeek)));
  for (let week = startWeek; week <= endWeek; week += step) {
    ticks.push({
      day: anchorDay + (week - 1) * 7,
      label: `第 ${week} 周`,
      major: week === startWeek || week % 4 === 1,
      year: 0,
    });
  }
  return ticks;
}

export interface Tick { day: number; label: string; major: boolean; year: number }
export function timelineTicks(start: number, end: number, level: ZoomLevel): Tick[] {
  const ticks: Tick[] = [];
  for (let day = Math.floor(start); day <= Math.ceil(end); day++) {
    const date = new Date(day * 86400000), d = date.getUTCDate(), m = date.getUTCMonth(), weekday = date.getUTCDay();
    let major = false, minor = false, label = '';
    if (level === 'year') { major = d === 1 && m === 0; minor = d === 1 && m % 3 === 0; label = `${date.getUTCFullYear()}`; }
    if (level === 'quarter') { major = d === 1 && m % 3 === 0; minor = d === 1; label = `Q${Math.floor(m / 3) + 1}`; }
    if (level === 'month') { major = d === 1; minor = weekday === 1; label = `${m + 1}月`; }
    if (level === 'week') { major = weekday === 1; minor = true; label = `${m + 1}/${d}`; }
    if (level === 'day') { major = true; label = `${['日','一','二','三','四','五','六'][weekday]} ${m + 1}/${d}`; }
    if (major || minor) ticks.push({ day, label, major, year: date.getUTCFullYear() });
  }
  return ticks;
}
export interface PlacedItem { item: TimelineItem; x: number; left: number; lane: number; rangeLane: number }
/** Allocate bounded information layers, never a row per task. Unplaced items remain available in clusters. */
export function layoutItems(items: TimelineItem[], start: number, density: number, width: number, selectedId: string | null, cardWidth: number, layers: number) {
  const occupied: { left: number; right: number }[][] = Array.from({ length: layers * 2 }, () => []);
  const ranges: number[] = [];
  const placed: PlacedItem[] = [], hidden: TimelineItem[] = [];
  const priority = (item: TimelineItem) => item.node.id === selectedId ? 0 : item.kind === 'milestone' ? 1 : item.node.timelineLevel === 'major' ? 2 : item.node.status === 'completed' ? 4 : 3;
  const candidates = items.filter(i => i.end >= start && i.start <= start + width / density)
    .sort((a, b) => priority(a) - priority(b) || a.start - b.start || a.node.id.localeCompare(b.node.id));
  candidates.forEach((item, index) => {
    const x = Math.max(0, Math.min(width, dateToX(item.start, start, density)));
    let left = Math.max(8, Math.min(width - cardWidth - 8, x - cardWidth / 2));
    const preference = Array.from({ length: layers * 2 }, (_, i) => i % 2 === 0 ? i + index % 2 : i - index % 2);
    let lane: number | undefined;
    for (const shift of [0, 36, -36, 72, -72, 108, -108, 144, -144]) {
      const candidateLeft = Math.max(8, Math.min(width - cardWidth - 8, x - cardWidth / 2 + shift));
      lane = preference.find(l => occupied[l].every(r => candidateLeft + cardWidth + 12 <= r.left || candidateLeft >= r.right + 12));
      if (lane !== undefined) { left = candidateLeft; break; }
    }
    if (lane === undefined) { hidden.push(item); return; }
    occupied[lane].push({ left, right: left + cardWidth });
    let rangeLane = ranges.findIndex(end => end + 4 < dateToX(item.start, start, density));
    if (rangeLane < 0) rangeLane = ranges.length;
    ranges[rangeLane] = dateToX(item.end, start, density);
    placed.push({ item, x, left, lane, rangeLane: rangeLane % 3 });
  });
  return { placed, hidden };
}
