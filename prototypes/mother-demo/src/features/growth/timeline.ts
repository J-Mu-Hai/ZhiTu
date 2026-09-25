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
export interface TimelineItem { node: GrowthNode; start: number; end: number; kind: 'event' | 'duration' | 'milestone' | 'goal'; derived: boolean }
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
