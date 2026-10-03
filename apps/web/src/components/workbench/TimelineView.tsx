'use client';
import { useState, useRef, useEffect, useMemo, type CSSProperties, type PointerEvent } from 'react';
import { CalendarDays, CalendarClock, X, Flag, Circle, Target } from 'lucide-react';
import { useRouter } from 'next/navigation';
import type { GrowthNode } from '@/types/growth';
import { useDemo } from '@/features/growth/provider';
import { anchoredZoom, dateString, dateToX, dayNumber, getVisibleItems, layoutItems, timelineItems, timelineTicks, unscheduledNodes, zoomLevelFor, todayInTimeZone, type TimelineItem } from '@/features/growth/timeline';
import { StrategyArchitecturePreview } from './StrategyArchitecturePreview';
import { V01TimelineAxis } from './V01TimelineAxis';
import styles from './TimelineView.module.css';

const colors = { academic: '#749ce1', research: '#61ad9e', experience: '#c7a06e', personal: '#a294ce' };
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
 * ## 这一版删掉了什么、为什么
 *
 * 上一版有四组"第二套时间表达":左上尺度按钮、右上导航/缩放控制组、顶部独立 ruler、
 * 底部 overview 蓝色覆盖条。它们的共同问题是**同一条时间被画了两遍**,而且与工作台
 * 暖白主题不统一。这一版只留画布中央一条主轴,年/月/周刻度、今天标记、任务锚点
 * 全部围绕它排布。
 *
 * ## 删控件 ≠ 删能力
 *
 * 鼠标/触控板拖动平移、Ctrl/⌘+滚轮缩放、方向键平移、`+`/`-` 缩放、`Home` 回到今天
 * **全部保留**(见下面的 wheel 监听与 `onKeyDown`)。当前尺度由 `density` 自动推断,
 * 日期刻度按密度自适应,不需要用户先选"月/周"。交互说明只在 `aria-describedby` 里
 * 说一次,不再用一条常驻说明占空间。
 */
export function TimelineView() {
  const { growth, selectedId, select, apply, updateNode, spaceId, workspaceId, isRealSpace, planError, timelineViewport: viewport, setTimelineViewport: setViewport, reasoning, confirmRemote, rejectRemote, deciding } = useDemo();
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
  const [editingDeadline, setEditingDeadline] = useState(false);
  const [deadlineInput, setDeadlineInput] = useState('');
  const [startInput, setStartInput] = useState(''), [endInput, setEndInput] = useState('');
  const canvas = useRef<HTMLDivElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const { start, density } = viewport;
  const level = zoomLevelFor(density), end = start + size.width / density;
  // **中央唯一主轴**:卡片从中轴上下错开,引线连到轴上。
  const axisY = Math.max(150, size.height * .5);
  const layers = size.height >= 600 ? 2 : 1;
  const cardWidth = Math.min(164, Math.max(126, size.width * .23));
  const all = useMemo(() => timelineItems(growth, spaceId), [growth, spaceId]);
  // 这段时间线上画不出来的节点。它们没有消失,只是没日期 —— 见 `unscheduledNodes`。
  const unscheduled = useMemo(() => unscheduledNodes(growth, spaceId), [growth, spaceId]);
  const displayItems = getVisibleItems(all, level);
  const selectedSource = all.find(i => i.node.id === selectedId);
  // Keep a deliberately selected object discoverable across semantic levels.
  if (selectedSource && !displayItems.some(i => i.node.id === selectedId)) displayItems.push(selectedSource);
  const { placed, hidden } = layoutItems(displayItems, start, density, size.width, selectedId, cardWidth, layers);
  const ticks = timelineTicks(start, end, level);
  const firstYear = new Date(start * 86400000).getUTCFullYear();
  const years = Array.from({ length: new Date(end * 86400000).getUTCFullYear() - firstYear + 1 }, (_, i) => firstYear + i);
  const x = (day: number) => dateToX(day, start, density);
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
      if ((event.target as HTMLElement).closest('[data-cluster-panel],[data-unscheduled-panel]')) return;
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        const anchor = event.clientX - element.getBoundingClientRect().left;
        setViewport(v => { const next = Math.max(.25, Math.min(160, v.density * Math.exp(-event.deltaY * .008))); return { start: anchoredZoom(v.start, v.density, next, anchor), density: next }; });
      } else setViewport(v => ({ ...v, start: v.start + (event.deltaX || event.deltaY) / v.density }));
    };
    element.addEventListener('wheel', wheel, { passive: false });
    return () => element.removeEventListener('wheel', wheel);
  }, [setViewport]);

  function zoomTo(nextDensity: number) {
    const next = Math.max(.25, Math.min(160, nextDensity));
    setViewport(v => ({ start: anchoredZoom(v.start, v.density, next, size.width / 2), density: next }));
    setClusterOpen(false);
  }
  function choose(id: string) { select(id); setEditing(false); }
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
  const v01Items = reasoning?.v01Timeline ?? [];
  // 规划智能体 V0.1:时间线**就是主轴本身** —— 不再在轴上再摆一块
  // StrategyArchitecturePreview。确认前的草案是虚线,确认后是实线,结构一致。
  if (v01Items.length > 0) {
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
    <p id="timeline-help" className={styles.srOnly}>拖动空白平移，Ctrl 或 Command 加滚轮缩放，方向键平移，加号减号缩放，Home 回到今天。改具体安排请用「排期」。</p>
    {/* 阶段 11:战略时间架构预览(未确认前;与路径页同一份 reasoning 数据)。 */}
    <StrategyArchitecturePreview
      onOpenPath={() => router.replace(`/workbench?workspace=${workspaceId}&view=path`, { scroll: false })}
    />
    {planError && <div className={styles.error} role="alert"><span>{planError}</span></div>}
    <div ref={canvas} className={styles.canvas} role="region" aria-label="成长时间线" aria-describedby="timeline-help" tabIndex={0} data-testid="timeline-canvas" data-ready={measured} data-start={start} data-density={density}
      onPointerDown={e => { if (e.button !== 0 || (e.target as HTMLElement).closest('button,input,[data-cluster-panel],[data-unscheduled-panel]')) return; e.currentTarget.setPointerCapture(e.pointerId); gesture.current = { x: e.clientX, start }; setClusterOpen(false); setUnscheduledOpen(false); }}
      onPointerMove={move} onPointerUp={finish} onPointerCancel={finish}
      onKeyDown={e => { if (e.target !== e.currentTarget) return; if (['ArrowLeft', 'ArrowRight', '+', '=', '-', 'Home'].includes(e.key)) e.preventDefault(); if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') setViewport(v => ({ ...v, start: v.start + (e.key === 'ArrowLeft' ? -1 : 1) * size.width / density * .2 })); if (e.key === '+' || e.key === '=') zoomTo(density * 1.5); if (e.key === '-') zoomTo(density / 1.5); if (e.key === 'Home') setViewport(v => ({ ...v, start: today - size.width / density * .28 })); }}>
      {/* 唯一的时间轴。年份、刻度、今天、任务锚点都围绕它。 */}
      <div className={styles.axis} style={{ top: axisY }}/><div className={styles.past} style={{ top: axisY, width: Math.max(0, Math.min(size.width, x(today))) }}/><span className={styles.axisEnd} style={{ top: axisY }}>›</span>
      {years.map(year => <span key={year} className={styles.year} style={{ left: Math.max(18, x(dayNumber(`${year}-01-01`)) + 6), top: axisY - 24 }}>{year}</span>)}
      {ticks.map(t => <div key={t.day} className={`${styles.tick} ${t.major ? styles.majorTick : ''}`} style={{ left: x(t.day), top: axisY }}>{t.major && <span className={styles.tickLabel}>{t.label}</span>}</div>)}
      {x(today) >= 0 && x(today) <= size.width && <div className={styles.today} style={{ left: x(today), top: 6, bottom: 6 }} data-testid="today-marker"><span className={styles.todayLabel}>{shortDate(today)}<strong>今天</strong></span><span className={styles.todayDot} style={{ top: axisY - 9 }}/></div>}
      {placed.map(({ item, x: anchorX, left, lane, rangeLane }) => {
        const id = item.node.id, upper = lane % 2 === 0;
        const cardY = upper ? axisY - 108 - Math.floor(lane / 2) * 86 : axisY + 40 + Math.floor(lane / 2) * 86;
        const color = item.node.category ? colors[item.node.category] : '#829dc5';
        const rangeY = axisY + 7 + rangeLane * 4;
        const Icon = item.kind === 'milestone' ? Flag : item.kind === 'goal' ? Target : Circle;
        return <div key={id} data-timeline-item={id} data-start-date={dateString(item.start)} data-end-date={dateString(item.end)} className={`${styles.object} ${selectedId === id ? styles.selected : ''} ${hovered && hovered !== id ? styles.dim : ''}`} style={{ '--color': color } as CSSProperties} onMouseEnter={() => setHovered(id)} onMouseLeave={() => setHovered(null)}>
          <svg className={styles.lines} aria-hidden="true"><path className={styles.connection} d={`M ${anchorX} ${axisY} V ${upper ? cardY + 84 : cardY - 12} L ${left + cardWidth / 2} ${upper ? cardY + 72 : cardY}`}/>
            {item.end > item.start && <><line className={styles.range} x1={Math.max(0, x(item.start))} x2={Math.min(size.width, x(item.end))} y1={rangeY} y2={rangeY}/>{[item.start,item.end].filter(d => x(d) >= 0 && x(d) <= size.width).map(d => <circle key={d} className={styles.endpoint} cx={x(d)} cy={rangeY} r="2.5"/>)}</>}
          </svg>
          <button className={`${styles.point} ${item.start < start ? styles.continuation : item.kind === 'milestone' ? styles.milestone : item.kind === 'goal' ? styles.goal : ''}`} style={{ left: anchorX, top: axisY }} aria-label={`${item.node.title}${item.start < start ? '从此前延续' : '时间点'}`} onClick={() => choose(id)} onPointerDown={e => beginItem(e, item)}/>
          <button data-timeline-card className={`${styles.card} ${item.kind !== 'duration' ? styles.eventCard : ''}`} style={{ left, top: cardY, width: cardWidth }} aria-label={`${item.node.title}，${dateString(item.start)}至${dateString(item.end)}`} aria-pressed={selectedId === id} title={`${item.node.title} · ${dateString(item.start)} — ${dateString(item.end)}${item.derived ? '（根据子任务推导）' : ''}`} onClick={() => choose(id)} onPointerDown={e => beginItem(e, item)}>
            <time><Icon size={11}/>{rangeLabel(item)}</time><strong>{item.node.title}</strong><small>{cardNote(item, start)}</small>
          </button>
          {(hovered === id || selectedId === id) && item.start >= start && <div className={styles.hoverDate} style={{ left: anchorX, top: axisY - 21 }}><span>{shortDate(item.start)}</span></div>}
        </div>;
      })}
      {hidden.length > 0 && <button className={styles.cluster} style={{ left: size.width / 2, top: axisY + 19 }} aria-expanded={clusterOpen} onClick={() => setClusterOpen(!clusterOpen)}>另有 {hidden.length} 项 · 展开</button>}
      {clusterOpen && hidden.length > 0 && <div className={styles.clusterPanel} data-cluster-panel onPointerDown={e => e.stopPropagation()}><header>同一时段的其他安排<button aria-label="关闭其他安排" onClick={() => setClusterOpen(false)}><X size={14}/></button></header>{hidden.map(item => <button key={item.node.id} onClick={() => reveal(item)}>{item.node.title}<small>{dateString(item.start)} — {dateString(item.end)}</small></button>)}</div>}
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
    </div>
    {selectedSource && <div className={styles.inspector} data-testid="date-inspector">
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
