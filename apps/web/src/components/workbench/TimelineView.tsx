'use client';
import { useState, useRef, useEffect, useMemo, type CSSProperties, type PointerEvent } from 'react';
import { CalendarDays, CalendarClock, X, Flag, Circle, Target } from 'lucide-react';
import { useRouter } from 'next/navigation';
import type { GrowthNode } from '@/types/growth';
import { useDemo } from '@/features/growth/provider';
import { anchoredZoom, dateString, dateToX, dayNumber, draftTimelineItems, getVisibleItems, layoutItems, timelineItems, timelineTicks, unscheduledNodes, zoomLevelFor, zoomLabels, zoomPresets, todayInTimeZone, type TimelineItem, type ZoomLevel } from '@/features/growth/timeline';
import { StrategyArchitecturePreview } from './StrategyArchitecturePreview';
import { V01TimelineAxis } from './V01TimelineAxis';
import styles from './TimelineView.module.css';

const colors = { academic: '#749ce1', research: '#61ad9e', experience: '#c7a06e', personal: '#a294ce' };
// 阶段语义类别 -> 低饱和度色系。**不只靠颜色**:卡片上另有类别文字 + 阶段编号。
const categoryColors: Record<string, string> = {
  定位: '#7d9bb5',
  基础闭环: '#7aa892',
  深入建设: '#9a8fc0',
  产出: '#c19a6b',
  缓冲: '#9aa7b3',
};
const shortDate = (day: number) => { const d = new Date(day * 86400000); return `${d.getUTCMonth() + 1}.${d.getUTCDate()}`; };

/**
 * 这个点画的是不是**截止日**。
 *
 * 没有排期的节点,它的 `startDate`/`endDate` 是从 `deadline` 映出来的(见
 * planProjection),所以落在时间线上是一个点 —— 而这个点和"这件事安排在这一天"
 * 长得一模一样。判据是"渲染出来的那一天 **就是** 节点的 deadline",不是"节点有
 * deadline":一个排到了别处的节点不该被说成截止在那天。
 *
 * **`sessions` 非空时恒为假。** 排过期的节点,它的 start/end 来自真实场次,所以
 * 那个点是"这天有安排"。如果它恰好落在截止日那一天,不加这一句的话卡片上会写
 * "截止日 · 还没排出具体安排" —— 而它明明已经排好了。
 */
const isDeadlinePoint = (item: TimelineItem) =>
  !item.node.sessions?.length && !!item.node.deadline && item.start === item.end && dateString(item.start) === item.node.deadline;

/**
 * 已经排好的节点,卡片下面那行小字。
 *
 * 说"共 180 分钟,分 3 次"而不是重复一遍日期:日期已经在卡片标题上了,用户在这里
 * 想知道的是**这件事被拆成了什么**。一个 8 小时的任务被切成 8 场,如果界面上不写
 * 出来,他会以为系统把同一件事记了 8 遍(而这正是排期文档里反复强调不该发生的事)。
 */
function scheduledNote(node: GrowthNode): string {
  const sessions = node.sessions ?? [];
  const open = sessions.filter(session => session.status === 'planned' || session.status === 'in_progress');
  if (!open.length) return `已完成 ${sessions.length} 次安排`;
  const minutes = open.reduce((sum, session) => sum + session.plannedMinutes, 0);
  const locked = open.some(session => session.locked) ? ' · 已锁定' : '';
  return open.length === 1 ? `已排期 · ${minutes} 分钟${locked}` : `已排期 · 共 ${minutes} 分钟，分 ${open.length} 次${locked}`;
}

const rangeLabel = (item: TimelineItem) => {
  const range = `${shortDate(item.start)}${item.end !== item.start ? ` — ${dateString(item.start).slice(0, 4) !== dateString(item.end).slice(0, 4) ? dateString(item.end).replaceAll('-', '.') : shortDate(item.end)}` : ''}`;
  return isDeadlinePoint(item) ? `截止 ${range}` : range;
};

/**
 * 卡片下面那行小字。
 *
 * **顺序是有意义的**:推导出来的范围 > 已完成 > 截止日 > 描述 > 兜底。
 * 最前面原来还有一档"建议安排 · 等待接受",对应的是示例空间那份本地提案的
 * 预览态。它没有了:卡片现在画的永远是计划里真实的那一天。
 */
function cardNote(item: TimelineItem, viewportStart: number): string {
  if (item.derived) return '计划范围 · 根据子任务推导';
  if (item.node.status === 'completed') return '已完成';
  if (item.node.sessions?.length) return scheduledNote(item.node);
  if (isDeadlinePoint(item)) return '截止日 · 还没排出具体安排';
  if (item.start < viewportStart) return '此前开始 · 持续进行';
  return item.node.description ?? (item.kind === 'milestone' ? '重要节点' : item.kind === 'duration' ? '持续安排' : '当日行动');
}

/** 画布上唯一还在的手势:平移。拖动卡片改日期的那一支已经删掉了(见 `beginItem`)。 */
type Gesture = { x: number; start: number };

/**
 * 成长时间线(阶段 9:中央唯一时间轴)。
 *
 * ## 这一版的尺度控制
 *
 * 画布中央只有一条主轴,年/季/月/周/天刻度按 density 自动切换。**五档快捷尺度**
 * (年/季度/月/周/天)保留为一组轻量按钮 —— V1 粗时间线必须在同一条轴上自由缩放;
 * 但不再有右上导航/缩放控制组、顶部独立 ruler、底部 overview。
 *
 * ## 删控件 ≠ 删能力
 *
 * 鼠标/触控板拖动平移、Ctrl/⌘+滚轮缩放、方向键平移、`+`/`-` 缩放、`Home` 回到今天
 * **全部保留**(见下面的 wheel 监听与 `onKeyDown`)。当前尺度由 `density` 自动推断,
 * 日期刻度按密度自适应,不需要用户先选"月/周"。交互说明只在 `aria-describedby` 里
 * 说一次,不再用一条常驻说明占空间。
 */
export function TimelineView() {
  const { growth, selectedId, select, apply, updateNode, spaceId, workspaceId, isRealSpace, planError, timelineViewport: viewport, setTimelineViewport: setViewport, timelineAnchor, setTimelineAnchor, reasoning, confirmRemote, rejectRemote, deciding } = useDemo();
  const router = useRouter();
  // 每次渲染重新算一次。它只在跨过午夜时才会变,而这个组件本来就会因为别的原因
  // 重渲染很多次 —— 为它加一个定时器是没必要的复杂度。
  const today = dayNumber(todayInTimeZone());

  const [size, setSize] = useState({ width: 760, height: 570 });
  const [measured, setMeasured] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);
  const [clusterOpen, setClusterOpen] = useState(false);
  const [unscheduledOpen, setUnscheduledOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draftSelectedId, setDraftSelectedId] = useState<string | null>(null);
  const [editingDeadline, setEditingDeadline] = useState(false);
  const [deadlineInput, setDeadlineInput] = useState('');
  const [startInput, setStartInput] = useState(''), [endInput, setEndInput] = useState('');
  const canvas = useRef<HTMLDivElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const { start, density } = viewport;
  const level = zoomLevelFor(density), end = start + size.width / density;
  // **中央唯一主轴**:卡片从中轴上下错开,引线连到轴上。
  const axisY = Math.max(150, size.height * .5);
  // ---- V1 粗时间架构:走**同一条中央主轴**,不再另起一个小组件 ----
  // V0.1(`v1Stage` 为空)仍保留原来的 V01TimelineAxis,避免既有时间线回归。
  const v01Items = useMemo(() => reasoning?.v01Timeline ?? [], [reasoning?.v01Timeline]);
  const v1DraftMode = Boolean(reasoning?.v1Stage) && v01Items.length > 0;
  // 展示锚点:相对周草案的“预计开始日”。用户没给日期时用它推算预测日历,
  // **不**写进后端(后端仍是 startWeek/endWeek 真值)。
  const anchorDay = dayNumber(timelineAnchor);
  const draft = useMemo(
    () => (v1DraftMode ? draftTimelineItems(v01Items, anchorDay) : null),
    [v1DraftMode, v01Items, anchorDay],
  );
  // 阶段方框卡固定宽度(200–260),内容决定高度;非 V1 正式卡片仍用紧凑宽度。
  const cardWidth = draft
    ? Math.min(260, Math.max(200, size.width * .18))
    : Math.min(164, Math.max(126, size.width * .23));
  // 草案阶段全部铺开:按阶段数增加轨道,而不是把卡片藏进“另有 N 项”。
  const layers = draft
    ? Math.max(2, Math.ceil(v01Items.length / 2) + 1)
    : size.height >= 600 ? 2 : 1;
  const draftIsPending = v1DraftMode && v01Items.some(item => item.status === 'draft');
  const all = useMemo(
    () => (draft ? draft.items : timelineItems(growth, spaceId)),
    [draft, growth, spaceId],
  );
  // 这段时间线上画不出来的节点。它们没有消失,只是没日期 —— 见 `unscheduledNodes`。
  const unscheduled = useMemo(
    () => (draft ? [] : unscheduledNodes(growth, spaceId)),
    [draft, growth, spaceId],
  );
  // 草案阶段永远全部显示(3–6 个阶段,不受缩放层级过滤);正式计划仍按层级过滤。
  const displayItems = draft ? [...all] : getVisibleItems(all, level);
  const effectiveSelectedId = draft ? draftSelectedId : selectedId;
  const selectedSource = all.find(i => i.node.id === effectiveSelectedId);
  // Keep a deliberately selected object discoverable across semantic levels.
  if (selectedSource && !displayItems.some(i => i.node.id === effectiveSelectedId)) displayItems.push(selectedSource);
  const { placed, hidden } = layoutItems(displayItems, start, density, size.width, effectiveSelectedId, cardWidth, layers);
  const relativeAxis = Boolean(draft && draft.mode === 'relative');
  // 预测日历轴与正式时间线共用同一套刻度:年/季/月/周/天随 density 自动切换。
  // V1 周尺度把标签换成“某月 · 第 N 周” —— 用户看的是已确认的周计划,不是 10/6。
  const ticks = useMemo(() => {
    const base = timelineTicks(start, end, level);
    if (!draft || level !== 'week') return base;
    return base.map(tick => {
      const date = new Date(tick.day * 86400000);
      const month = date.getUTCMonth() + 1;
      const day = date.getUTCDate();
      return { ...tick, label: `${month}月 · 第${Math.floor((day - 1) / 7) + 1}周` };
    });
  }, [start, end, level, draft]);
  const firstYear = new Date(start * 86400000).getUTCFullYear();
  const years = Array.from({ length: new Date(end * 86400000).getUTCFullYear() - firstYear + 1 }, (_, i) => firstYear + i);
  const draftSelected = draft ? v01Items.find(item => item.id === draftSelectedId) ?? null : null;
  const selectedPlaced = draft ? placed.find(entry => entry.item.node.id === effectiveSelectedId) ?? null : null;
  const x = (day: number) => dateToX(day, start, density);

  /*
   * ---- 周 / 日轨道:只读**已确认**计划,缩放揭示更多 ----
   *
   * 周条来自已确认的「本周计划 / 下周预览」节点(`PlanNode`,stage),用父链回到正式阶段
   * 取它的相对周映射区间;日块来自已确认的排期场次(`sessions`)。proposal 草案和归档
   * 版本不进入这两条轨道 —— 这里只显示已经写进计划的东西。
   */
  const phaseRangeByTitle = new Map<string, { start: number; end: number }>();
  if (draft) draft.items.forEach(item => phaseRangeByTitle.set(item.node.title, { start: item.start, end: item.end }));
  const weekItems: TimelineItem[] = [];
  if (draft && (level === 'week' || level === 'day')) {
    for (const node of Object.values(growth.nodes)) {
      if (node.type !== 'stage' || !/^(本周计划|下周预览)[:：]/.test(node.title)) continue;
      const parent = node.parentId ? growth.nodes[node.parentId] : undefined;
      const range = parent ? phaseRangeByTitle.get(parent.title) : undefined;
      if (!range) continue;
      weekItems.push({ node, start: range.start, end: range.end, kind: 'duration', derived: false, track: 'week', itemId: `week:${node.id}` });
    }
  }
  const dayItems: TimelineItem[] = [];
  if (draft && level === 'day') {
    for (const node of Object.values(growth.nodes)) {
      if (node.type !== 'task' || node.purpose === 'information') continue;
      for (const session of node.sessions ?? []) {
        const day = dayNumber(session.date);
        if (!Number.isFinite(day)) continue;
        dayItems.push({ node, start: day, end: day, kind: 'event', derived: false, track: 'day', itemId: `session:${session.id}` });
      }
    }
  }
  const packLanes = (items: TimelineItem[], widthOf: (item: TimelineItem) => number) => {
    const lanes: { left: number; right: number }[][] = [];
    const result = new Map<string, number>();
    for (const item of items) {
      const id = item.itemId ?? item.node.id;
      const left = Math.max(0, x(item.start));
      const width = widthOf(item);
      let lane = lanes.findIndex(occupied => occupied.every(range => left + width + 8 <= range.left || left >= range.right + 8));
      if (lane < 0) { lane = lanes.length; lanes.push([]); }
      lanes[lane].push({ left, right: left + width });
      result.set(id, lane);
    }
    return result;
  };
  const weekLanes = packLanes(weekItems, item => Math.max(48, Math.min(size.width, x(item.end)) - Math.max(0, x(item.start))));
  const dayLanes = packLanes(dayItems, () => 96);

  const selected = selectedId ? growth.nodes[selectedId] : null;

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => { setSize({ width: entry.contentRect.width, height: entry.contentRect.height }); setMeasured(true); });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      if ((event.target as HTMLElement).closest('[data-cluster-panel],[data-unscheduled-panel],[data-testid="v1-phase-detail"]')) return;
      event.preventDefault();
      const rect = element.getBoundingClientRect();
      const anchor = event.clientX - rect.left;
      const horizontal = event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY);
      if (horizontal) {
        // 平移:触控板横向滚动 / Shift + 滚轮。
        setViewport(v => ({ ...v, start: v.start + (event.deltaX || event.deltaY) / v.density }));
        return;
      }
      // 缩放:**直接垂直滚轮**,围绕鼠标所在时间点 —— 不是固定屏幕中心。
      setViewport(v => {
        const next = Math.max(.25, Math.min(160, v.density * Math.exp(-event.deltaY * .0015)));
        return { start: anchoredZoom(v.start, v.density, next, anchor), density: next };
      });
    };
    element.addEventListener('wheel', wheel, { passive: false });
    return () => element.removeEventListener('wheel', wheel);
  }, [setViewport]);
  /*
   * Escape 关详情。
   *
   * **不是** document click 那种“点哪里都关”。它只在真的有选中（草案阶段或正式节点）
   * 时才动作，而且只监听 Escape —— 不会误伤右侧对话、日期控件、缩放按钮或任务卡。
   */
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (draftSelectedId !== null) { setDraftSelectedId(null); return; }
      if (selectedId !== null) select(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [draftSelectedId, selectedId, select]);
  // 进入粗时间线时**聚焦全部阶段一次**,但不锁死后续缩放/平移。
  const fittedDraftRef = useRef<TimelineItem[] | null>(null);
  useEffect(() => {
    if (!draft || !measured || size.width <= 0) return;
    if (fittedDraftRef.current === draft.items) return;
    fittedDraftRef.current = draft.items;
    const first = Math.min(...draft.items.map(item => item.start));
    const last = Math.max(...draft.items.map(item => item.end));
    const span = Math.max(7, last - first + 14);
    // 粗时间架构的首屏先停在“月”尺度：它的职责是看见阶段覆盖的时间长度，
    // 不是把 30 天方案一进来就放大成按周/按日的执行视图。用户仍可自由切换
    // 年 / 季度 / 月 / 周 / 日，且后续平移缩放不受这一初始值影响。
    const fittedDensity = (size.width - 140) / span;
    // 首屏停在“月”尺度(密度 < 12):职责是看见阶段覆盖的长度。但**不要**压到
    // 月预设的 4 —— 那样一周的阶段只有 28px,标题全截断。取 11 既仍属于月级,
    // 又让短阶段有可读宽度。用户仍可自由切年/季/月/周/日。
    const nextDensity = Math.max(.25, Math.min(11, fittedDensity));
    setViewport({ start: first - 20 / nextDensity, density: nextDensity });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, measured, size.width]);

  function zoomTo(nextDensity: number) {
    const next = Math.max(.25, Math.min(160, nextDensity));
    setViewport(v => {
      // 粗时间架构在月级首屏时常只占视口左半边。若仍机械地围绕屏幕正中
      // 缩放，切到“周 / 天”会把阶段甩到屏外，只剩一片空白。草案模式改为
      // 围绕阶段区间中心；普通时间线仍沿用用户当前视口中心的直觉。
      const focus = draft
        ? (Math.min(...draft.items.map(item => item.start)) + Math.max(...draft.items.map(item => item.end))) / 2
        : v.start + size.width / (2 * v.density);
      const anchor = (focus - v.start) * v.density;
      return { start: anchoredZoom(v.start, v.density, next, anchor), density: next };
    });
    setClusterOpen(false);
  }
  function choose(id: string) {
    if (draft) { setDraftSelectedId(id); return; }
    select(id); setEditing(false);
  }
  /**
   * 点在卡片上 —— **只选中,不开始拖动。**
   *
   * 这里原来还能把卡片横着拖几天,拖完把 `startDate`/`endDate` 推到别处。那条路
   * 现在整个删了:它改的是一场真实存在的安排,而写入路径是「排期」,不是把日期推
   * 几天 —— 后端根本没有这个字段。真正会写下去的只有 `deadline`,而那是**截止日**,
   * 悄悄拿它当排期用会改掉用户设的截止时间,界面上的说辞却是"调整了安排"。
   */
  function beginItem(event: PointerEvent<HTMLButtonElement>, item: TimelineItem) {
    event.stopPropagation(); choose(item.node.id);
    if (event.button !== 0 || item.derived || isRealSpace) return;
  }
  function move(event: PointerEvent) {
    const drag = gesture.current;
    if (drag) setViewport(v => ({ ...v, start: drag.start - (event.clientX - drag.x) / v.density }));
  }
  function finish() { gesture.current = null; }
  function reveal(item: TimelineItem) { choose(item.node.id); setClusterOpen(false); setViewport(v => ({ ...v, start: item.start - size.width / v.density * .35 })); }
  // 规划智能体 V0.1(`v1Stage` 为空)保留原来的小组件,避免既有时间线回归。
  // V1(`v1Stage` 非空)走下面同一条中央主轴 —— 见 `draftTimelineItems`。
  if (!reasoning?.v1Stage && v01Items.length > 0) {
    return <div className={styles.view} data-testid="timeline-view" data-v01="true">
      <V01TimelineAxis
        items={v01Items}
        proposalId={reasoning?.v01TimelineProposalId ?? null}
        deciding={deciding}
        onConfirm={id => { void confirmRemote(id); }}
        onReject={id => { void rejectRemote(id); }}
      />
    </div>;
  }
  return <div className={styles.view} data-testid="timeline-view" data-zoom={level}>
    {/* 交互说明只说一次,而且是给读屏的;**不再用常驻说明条占空间**。 */}
    <p id="timeline-help" className={styles.srOnly}>滚轮缩放；拖动空白平移；Shift 加滚轮或触控板横向滚动平移；方向键平移，加号减号缩放，Home 回到今天。改具体安排请用「排期」。</p>
    {/* 统一的中央时间轴:五档快捷尺度 + 滚轮/触控板缩放 + 拖拽平移。 */}
    <div className={styles.presets} data-testid="timeline-presets" role="group" aria-label="时间尺度">
      {(Object.keys(zoomPresets) as ZoomLevel[]).map(preset => (
        <button key={preset} type="button" className={level === preset ? styles.presetActive : ''} aria-pressed={level === preset} onClick={() => zoomTo(zoomPresets[preset])}>
          {zoomLabels[preset]}
        </button>
      ))}
    </div>
    <span className={styles.zoomHint}>滚轮缩放 · 拖动空白平移</span>
    {/* R2:V1 粗时间架构草案走主轴;顶部只留一条状态 + 操作,不再另摆一块预览。 */}
    {draft && (
      <div className={styles.draftBar} data-testid="v1-timeline-draft-bar" data-draft={draftIsPending ? 'true' : 'false'}>
        <span className={styles.draftStatus}>
          {draftIsPending
            ? relativeAxis
              ? `预测时间轴，以 ${timelineAnchor} 为起点，可调整${reasoning?.v1TimelineAlignment?.cadence ? ` · 估算节奏：${reasoning.v1TimelineAlignment.cadence}` : ''} · 相对周草案，等待你确认`
              : '已生成粗时间架构草案，等待你确认'
            : '时间架构已确认'}
        </span>
        {relativeAxis && (
          <label className={styles.draftAnchor}>
            起点
            <input type="date" value={timelineAnchor} aria-label="预测起点日期" onChange={event => setTimelineAnchor(event.target.value)} />
          </label>
        )}
        {draftIsPending && reasoning?.v01TimelineProposalId && (
          <div className={styles.draftActions}>
            <button type="button" className={styles.draftConfirm} disabled={deciding} onClick={() => { void confirmRemote(reasoning.v01TimelineProposalId as string); }}>
              {deciding ? '处理中…' : '确认时间架构'}
            </button>
            <button type="button" className={styles.draftAdjust} disabled={deciding} onClick={() => { void rejectRemote(reasoning.v01TimelineProposalId as string); }}>
              调整时间架构
            </button>
          </div>
        )}
      </div>
    )}
    {/* 阶段 11:战略时间架构预览(仅在非 V1 草案时;V1 已改由主轴本身承载)。 */}
    {!draft && (
      <StrategyArchitecturePreview
        onOpenPath={() => router.replace(`/workbench?workspace=${workspaceId}&view=path`, { scroll: false })}
      />
    )}
    {planError && <div className={styles.error} role="alert"><span>{planError}</span></div>}
    <div ref={canvas} className={styles.canvas} role="region" aria-label="成长时间线" aria-describedby="timeline-help" tabIndex={0} data-testid="timeline-canvas" data-ready={measured} data-start={start} data-density={density}
      onPointerDown={e => { if (e.button !== 0 || (e.target as HTMLElement).closest('button,input,[data-cluster-panel],[data-unscheduled-panel],[data-testid="v1-phase-detail"]')) return; e.currentTarget.setPointerCapture(e.pointerId); gesture.current = { x: e.clientX, start }; setClusterOpen(false); setUnscheduledOpen(false);
        // 点主时间轴空白 / 背景 / 既不是卡也不是控件的地方 → 关闭详情。
        if (draftSelectedId !== null) setDraftSelectedId(null);
        if (selectedId !== null) select(null);
      }}
      onPointerMove={move} onPointerUp={finish} onPointerCancel={finish}
      onKeyDown={e => { if (e.target !== e.currentTarget) return; if (['ArrowLeft', 'ArrowRight', '+', '=', '-', 'Home'].includes(e.key)) e.preventDefault(); if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') setViewport(v => ({ ...v, start: v.start + (e.key === 'ArrowLeft' ? -1 : 1) * size.width / density * .2 })); if (e.key === '+' || e.key === '=') zoomTo(density * 1.5); if (e.key === '-') zoomTo(density / 1.5); if (e.key === 'Home') setViewport(v => ({ ...v, start: today - size.width / density * .28 })); }}>
      {/* 唯一的时间轴。年份、刻度、今天、任务锚点都围绕它。 */}
      <div className={styles.axis} style={{ top: axisY }}/><div className={styles.past} style={{ top: axisY, width: Math.max(0, Math.min(size.width, x(today))) }}/><span className={styles.axisEnd} style={{ top: axisY }}>›</span>
      {years.map(year => <span key={year} className={styles.year} style={{ left: Math.max(18, x(dayNumber(`${year}-01-01`)) + 6), top: axisY - 24 }}>{year}</span>)}
      {ticks.map(t => <div key={t.day} data-testid="timeline-tick" data-major={t.major ? 'true' : 'false'} className={`${styles.tick} ${t.major ? styles.majorTick : ''}`} style={{ left: x(t.day), top: axisY }}>{t.major && <span className={styles.tickLabel}>{t.label}</span>}</div>)}
      {x(today) >= 0 && x(today) <= size.width && <div className={styles.today} style={{ left: x(today), top: 6, bottom: 6 }} data-testid="today-marker"><span className={styles.todayLabel}>{shortDate(today)}<strong>今天</strong></span><span className={styles.todayDot} style={{ top: axisY - 9 }}/></div>}
      {placed.map(({ item, x: anchorX, left, lane, rangeLane }) => {
        const id = item.itemId ?? item.node.id, upper = lane % 2 === 0;
        const draftSource = draft ? v01Items.find(entry => entry.id === id) ?? null : null;
        const cardRange = draftSource
          ? draftSource.startDate && draftSource.endDate
            ? `${shortDate(dayNumber(draftSource.startDate))} — ${shortDate(dayNumber(draftSource.endDate))}`
            : `${shortDate(item.start)} — ${shortDate(item.end)}`
          : rangeLabel(item);
        const cardSub = draftSource
          ? `${draftSource.category ? `${draftSource.category} · ` : ''}${draftSource.deliverable || draftSource.goal || ''}${draftSource.status === 'draft' ? ' · 待确认' : ''}`
          : cardNote(item, start);
        const lowerTracks = Boolean(draft) && (level === 'week' || level === 'day');
        const cardUpper = lowerTracks ? true : upper;
        const cardY = draft
          ? (cardUpper ? axisY - 100 - Math.floor(lane / 2) * 92 : axisY + 92 + Math.floor(lane / 2) * 92)
          : (upper ? axisY - 108 - Math.floor(lane / 2) * 86 : axisY + 116 + Math.floor(lane / 2) * 86);
        const color = draftSource?.category
          ? categoryColors[draftSource.category] ?? '#829dc5'
          : item.node.category
            ? colors[item.node.category]
            : '#829dc5';
        // 阶段覆盖条:干净的一条,按 rangeLane 分层避让。
        const barY = axisY + 8 + rangeLane * 20;
        const barDraft = draftSource ? draftSource.status === 'draft' : false;
        const Icon = item.kind === 'milestone' ? Flag : item.kind === 'goal' ? Target : Circle;
        return <div key={id} data-timeline-item={id} data-draft={draftSource ? draftSource.status : undefined} data-start-date={dateString(item.start)} data-end-date={dateString(item.end)} className={`${styles.object} ${effectiveSelectedId === id ? styles.selected : ''} ${hovered && hovered !== id ? styles.dim : ''}`} style={{ '--color': color } as CSSProperties} onMouseEnter={() => setHovered(id)} onMouseLeave={() => setHovered(null)}>
          {/*
           * **一条阶段只保留一个主表现。**
           *
           * V1 粗时间线(draft)只用有高度的覆盖条:不再同时挂一张固定大卡,否则
           * 「轴上条 + 旁边卡」是同一件事说两遍。目标 / 成果 / 完成标准只在点击 bar
           * 后的详情浮层里出现一次。非 V1 正式时间线保持原来的卡片 + 锚点。
           */}
          {draft && item.end > item.start && (() => {
            const barLeft = Math.max(0, x(item.start));
            const barWidth = Math.max(26, Math.min(size.width, x(item.end)) - barLeft);
            const barCenterX = barLeft + barWidth / 2;
            const cardCenterX = left + cardWidth / 2;
            const barEdgeY = cardUpper ? barY : barY + 12;
            const cardEdgeY = cardUpper ? cardY + 66 : cardY;
            const showSummary = level === 'year' || level === 'quarter' || level === 'month';
            const showRange = level !== 'day';
            const phaseIndex = v01Items.findIndex(entry => entry.id === id) + 1;
            const cardSummary = draftSource ? draftSource.deliverable || draftSource.goal || '' : '';
            return <>
              {/* 细引线:把方框卡连接到它自己的时间覆盖条。 */}
              <svg className={styles.lines} aria-hidden="true">
                <path className={styles.phaseConnector} d={`M ${barCenterX} ${barEdgeY} L ${barCenterX} ${cardEdgeY} L ${cardCenterX} ${cardEdgeY}`} />
              </svg>
              {/*
               * **覆盖条只表达“从何时到何时”。** 干净的一条色带,最多放一个极短的
               * “阶段 N”;空间不够直接隐藏,不截断、不塞标题和成果。
               */}
              <button
                type="button"
                data-testid="v1-phase-bar"
                data-phase-id={id}
                data-draft={barDraft ? 'true' : 'false'}
                className={`${styles.phaseBar} ${barDraft ? styles.phaseBarDraft : styles.phaseBarPlanned}`}
                style={{ left: barLeft, width: barWidth, top: barY, '--color': color } as CSSProperties}
                aria-label={`阶段 ${phaseIndex}：${item.node.title}，${cardRange}${barDraft ? '，草案' : ''}`}
                aria-pressed={effectiveSelectedId === id}
                title={`${item.node.title} · ${cardRange}`}
                onClick={() => choose(id)}
                onPointerDown={e => beginItem(e, item)}
              >
                {barWidth >= 54 && phaseIndex > 0 && <span className={styles.phaseBarLabel}>阶段 {phaseIndex}</span>}
              </button>
              {/*
               * **阶段方框卡表达“这段时间要做什么”。** 固定宽度、内容决定高度、
               * 标题完整可读;时间范围与摘要各占一行。点击打开同一个详情浮层。
               */}
              <button
                type="button"
                data-testid="v1-phase-card"
                data-phase-id={id}
                data-draft={barDraft ? 'true' : 'false'}
                className={`${styles.phaseCard} ${barDraft ? styles.phaseCardDraft : styles.phaseCardPlanned}`}
                style={{ left, top: cardY, width: cardWidth, '--color': color } as CSSProperties}
                aria-label={`阶段 ${phaseIndex}：${item.node.title}，${cardRange}`}
                aria-pressed={effectiveSelectedId === id}
                title={`${item.node.title} · ${cardRange}`}
                onClick={() => choose(id)}
                onPointerDown={e => beginItem(e, item)}
              >
                <strong className={styles.phaseCardTitle}>{item.node.title}</strong>
                {showRange && <span className={styles.phaseCardRange}>{cardRange}</span>}
                {showSummary && cardSummary && <span className={styles.phaseCardSummary}>{cardSummary}</span>}
              </button>
            </>;
          })()}
          {!draft && (<>
            <svg className={styles.lines} aria-hidden="true"><path className={styles.connection} d={`M ${anchorX} ${axisY} V ${upper ? cardY + 84 : cardY - 12} L ${left + cardWidth / 2} ${upper ? cardY + 72 : cardY}`}/>
              {item.end > item.start && <><line className={styles.range} x1={Math.max(0, x(item.start))} x2={Math.min(size.width, x(item.end))} y1={axisY + 7} y2={axisY + 7}/>{[item.start,item.end].filter(d => x(d) >= 0 && x(d) <= size.width).map(d => <circle key={d} className={styles.endpoint} cx={x(d)} cy={axisY + 7} r="2.5"/>)}</>}
            </svg>
            <button className={`${styles.point} ${item.start < start ? styles.continuation : item.kind === 'milestone' ? styles.milestone : item.kind === 'goal' ? styles.goal : ''}`} style={{ left: anchorX, top: axisY }} aria-label={`${item.node.title}${item.start < start ? '从此前延续' : '时间点'}`} onClick={() => choose(id)} onPointerDown={e => beginItem(e, item)}/>
            <button data-timeline-card data-draft={draftSource ? draftSource.status : undefined} className={`${styles.card} ${item.kind !== 'duration' ? styles.eventCard : ''} ${draftSource && draftSource.status === 'draft' ? styles.draftCard : ''}`} style={{ left, top: cardY, width: cardWidth }} aria-label={`${item.node.title}，${cardRange}`} aria-pressed={effectiveSelectedId === id} title={`${item.node.title} · ${cardRange}`} onClick={() => choose(id)} onPointerDown={e => beginItem(e, item)}>
              <time><Icon size={11}/>{draftSource?.index ? `阶段 ${draftSource.index} · ` : ''}{cardRange}</time><strong>{item.node.title}</strong><small>{cardSub}</small>
            </button>
            {(hovered === id || effectiveSelectedId === id) && item.start >= start && <div className={styles.hoverDate} style={{ left: anchorX, top: axisY - 21 }}><span>{shortDate(item.start)}</span></div>}
          </>)}
        </div>;
      })}
      {/* 周尺度:已确认的“本周计划 / 下周预览”条,放在所属阶段条下方。 */}
      {weekItems.map(item => {
        const id = item.itemId ?? item.node.id;
        const barLeft = Math.max(0, x(item.start));
        const barWidth = Math.max(48, Math.min(size.width, x(item.end)) - barLeft);
        const lane = weekLanes.get(id) ?? 0;
        const isPreview = item.node.title.startsWith('下周预览');
        return <div
          key={id}
          data-testid="v1-week-bar"
          data-week-id={item.node.id}
          data-preview={isPreview ? 'true' : 'false'}
          className={`${styles.weekBar} ${isPreview ? styles.weekBarPreview : ''}`}
          style={{ left: barLeft, width: barWidth, top: axisY + 122 + lane * 30 }}
          title={item.node.title}
        >
          <strong>{item.node.title.replace(/^(本周计划|下周预览)[:：]\s*/, '')}</strong>
          <small>{isPreview ? '下周预览' : '本周计划'}</small>
        </div>;
      })}
      {/* 日尺度:已确认的日工作块(排期场次),放在日轨道,不与阶段条重叠。 */}
      {dayItems.map(item => {
        const id = item.itemId ?? item.node.id;
        const lane = dayLanes.get(id) ?? 0;
        const left = Math.max(0, Math.min(size.width - 96, x(item.start) - 48));
        return <div
          key={id}
          data-testid="v1-day-block"
          data-session-id={item.itemId}
          className={styles.dayBlock}
          style={{ left, top: axisY + 198 + lane * 28 }}
          title={item.node.title}
        >
          {item.node.title}
        </div>;
      })}

      {/* 草案阶段不允许被折进“另有 N 项”:3–6 个阶段必须全部在轴上可见。 */}
      {!draft && hidden.length > 0 && <button className={styles.cluster} style={{ left: size.width / 2, top: axisY + 19 }} aria-expanded={clusterOpen} onClick={() => setClusterOpen(!clusterOpen)}>另有 {hidden.length} 项 · 展开</button>}
      {!draft && clusterOpen && hidden.length > 0 && <div className={styles.clusterPanel} data-cluster-panel onPointerDown={e => e.stopPropagation()}><header>同一时段的其他安排<button aria-label="关闭其他安排" onClick={() => setClusterOpen(false)}><X size={14}/></button></header>{hidden.map(item => <button key={item.node.id} onClick={() => reveal(item)}>{item.node.title}<small>{dateString(item.start)} — {dateString(item.end)}</small></button>)}</div>}
      {!placed.length && <p className={`${styles.empty} ${unscheduled.length ? styles.emptyWithNotice : ''}`}>这段时间暂无{level === 'day' ? '执行事项' : '安排'}，可以平移查看其他时间。</p>}
      {/*
       * 画不出来的节点。**必须能发现,但不能占一整条底部横幅。**
       *
       * 时间线上有 3 项、任务列表里有 11 项,而用户没有任何别的办法知道少的那 8 项
       * 去哪了 —— 他会以为计划就是那 3 项。所以留一个低干扰的小入口,点开是节点列表,
       * 点列表里的一项会选中并把它带进视野。
       */}
      {unscheduled.length > 0 && (
        <div className={styles.unscheduled} data-testid="unscheduled-items">
          <button type="button" className={styles.unscheduledToggle} aria-expanded={unscheduledOpen} onClick={() => setUnscheduledOpen(!unscheduledOpen)}>
            <CalendarClock size={12}/>未排期 {unscheduled.length} 项
          </button>
          {unscheduledOpen && (
            <div className={styles.unscheduledPanel} data-unscheduled-panel onPointerDown={e => e.stopPropagation()}>
              <header>还没有日期的节点<button aria-label="关闭未排期列表" onClick={() => setUnscheduledOpen(false)}><X size={14}/></button></header>
              {unscheduled.map(node => <button key={node.id} type="button" onClick={() => { choose(node.id); setUnscheduledOpen(false); }}>{node.title}</button>)}
              <p>给它们加上截止时间，或者用「排期」把它们排到具体某天，就会出现在时间线上。</p>
            </div>
          )}
        </div>
      )}
      {/* 点阶段 → 轻量浮层(不再占时间轴底部一整块)。 */}
      {draft && draftSelected && selectedPlaced && (
        <div
          className={styles.phasePopover}
          data-testid="v1-phase-detail"
          style={{
            left: Math.max(8, Math.min(Math.max(8, size.width - 272), selectedPlaced.left - 40)),
            top: Math.max(8, Math.min(size.height - 190, (selectedPlaced.lane % 2 === 0
              ? axisY - 108 - Math.floor(selectedPlaced.lane / 2) * 86
              : axisY + 116 + Math.floor(selectedPlaced.lane / 2) * 86) + 80)),
          }}
          onPointerDown={event => event.stopPropagation()}
        >
          <header>
            <strong>{draftSelected.title}</strong>
            <button type="button" aria-label="关闭阶段详情" onClick={() => setDraftSelectedId(null)}><X size={13} /></button>
          </header>
          <span>
            {draftSelected.category ? `${draftSelected.category} · ` : ''}
            {draftSelected.startDate && draftSelected.endDate
              ? `${draftSelected.startDate} → ${draftSelected.endDate}`
              : `${shortDate(anchorDay + ((draftSelected.startWeek ?? 1) - 1) * 7)} — ${shortDate(anchorDay + (draftSelected.endWeek ?? draftSelected.startWeek ?? 1) * 7)} · 第 ${draftSelected.startWeek ?? '?'}–${draftSelected.endWeek ?? draftSelected.startWeek ?? '?'} 周（预测）`}
          </span>
          {draftSelected.goal && <p>目标：{draftSelected.goal}</p>}
          {draftSelected.deliverable && <p>成果：{draftSelected.deliverable}</p>}
          {draftSelected.completionCriteria && <p>完成标准：{draftSelected.completionCriteria}</p>}
          {draftSelected.dependsOn && <p>依赖：{draftSelected.dependsOn}</p>}
          {reasoning?.v1Strategy?.riskControl && (
            <p>风险控制：{reasoning.v1Strategy.riskControl}</p>
          )}
          {draftSelected.whyHere && <p>为何排在这里：{draftSelected.whyHere}</p>}
        </div>
      )}
    </div>
    {!draft && selectedSource && <div className={styles.inspector} data-testid="date-inspector">
      {/* 截止日说的是"截止 2026-10-23",不是一个区间。印成 `2026-10-23 → 2026-10-23`
          读起来像"这一天有安排",而那一天其实什么都没排。 */}
      <div><strong>{selectedSource.node.title}</strong><span>{isDeadlinePoint(selectedSource) ? `截止 ${dateString(selectedSource.start)}（还没排出具体安排）` : `${dateString(selectedSource.start)} → ${dateString(selectedSource.end)}${selectedSource.derived ? ' · 推导范围' : ''}`}</span></div>
      {/* 截止日只有一个日期可改,而它写回的是后端的 `deadline` 字段(和节点详情
          弹窗同一条写路径)。派一个"开始/结束"的表单出来只会让用户以为自己在排期。 */}
      {isDeadlinePoint(selectedSource)
        ? <button onClick={() => { setDeadlineInput(selectedSource.node.deadline ?? ''); setEditingDeadline(true); }}><CalendarDays size={14}/>改截止时间</button>
        : !selectedSource.derived && <button onClick={() => { setStartInput(dateString(selectedSource.start)); setEndInput(dateString(selectedSource.end)); setEditing(true); }}><CalendarDays size={14}/>编辑日期</button>}
      {editingDeadline && selected && isDeadlinePoint(selectedSource) && <form className={styles.editor} onSubmit={e => { e.preventDefault(); if (!deadlineInput) return; updateNode(selected.id, { deadline: deadlineInput }); setEditingDeadline(false); }}><label>截止<input aria-label="截止日期" type="date" required value={deadlineInput} onChange={e => setDeadlineInput(e.target.value)}/></label><button type="submit" disabled={!deadlineInput}>应用</button><button type="button" aria-label="取消编辑" onClick={() => setEditingDeadline(false)}><X size={15}/></button></form>}
      {editing && selected && <form className={styles.editor} onSubmit={e => { e.preventDefault(); if (startInput && endInput >= startInput) { apply({ type: 'UPDATE_TIME', nodeId: selected.id, startDate: startInput, endDate: endInput }); setEditing(false); } }}><label>开始<input aria-label="开始日期" type="date" required value={startInput} onChange={e => setStartInput(e.target.value)}/></label><label>结束<input aria-label="结束日期" type="date" min={startInput} required value={endInput} onChange={e => setEndInput(e.target.value)}/></label><button type="submit" disabled={!startInput || !endInput || endInput < startInput}>应用</button><button type="button" aria-label="取消编辑" onClick={() => setEditing(false)}><X size={15}/></button></form>}
    </div>}
  </div>;
}
