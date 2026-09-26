'use client';
import { useState, useRef, useEffect, useMemo, type CSSProperties, type PointerEvent } from 'react';
import { ChevronLeft, ChevronRight, Minus, Plus, CalendarDays, X, Flag, Circle, Target } from 'lucide-react';
import type { GrowthNode } from '@/types/growth';
import { useDemo } from '@/features/growth/provider';
import { anchoredZoom, dateString, dateToX, dayNumber, getVisibleItems, layoutItems, timelineItems, timelineTicks, unscheduledNodes, zoomLabels, zoomLevelFor, zoomPresets, todayInTimeZone, type TimelineItem, type ZoomLevel } from '@/features/growth/timeline';
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
 * 抽出来是因为这里原来是一条七层嵌套的三元表达式,而"截止日的节点"需要多一档 ——
 * 再往那个链子里塞一层,下一个改它的人只会把顺序改错。**顺序是有意义的**:
 * 推导出来的范围 > 已完成 > 截止日 > 描述 > 兜底。
 *
 * 最前面原来还有一档"建议安排 · 等待接受",对应的是示例空间那份**本地**提案的
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

export function TimelineView() {
  const { growth, selectedId, select, apply, updateNode, spaceId, isRealSpace, planError, timelineViewport: viewport, setTimelineViewport: setViewport } = useDemo();
  // 每次渲染重新算一次。它只在跨过午夜时才会变,而这个组件本来就会因为别的原因
  // 重渲染很多次 —— 为它加一个定时器是没必要的复杂度。
  const today = dayNumber(todayInTimeZone());
  /*
   * **"看到哪一段、放多大"存在 Provider 里,不在这里。**
   *
   * 它以前是一个 `useState`。而工作台的四个视图是一个三元表达式:切一下页签,这个组件
   * 连同它的 state 一起没了 —— 于是从时间线切到排期再切回来,时间线跳回今天、缩放
   * 回到默认。这不是"重新算一次"的问题,是**用户刚才的视角被丢掉了**。
   * 存到 Provider(它的 key 是空间 id)之后:同一个空间里怎么切都在,
   * 换空间才重新开始 —— 那正是该有的边界。
   */

  const [size, setSize] = useState({ width: 760, height: 570 });
  const [measured, setMeasured] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);
  const [clusterOpen, setClusterOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editingDeadline, setEditingDeadline] = useState(false);
  const [deadlineInput, setDeadlineInput] = useState('');
  const [startInput, setStartInput] = useState(''), [endInput, setEndInput] = useState('');
  const canvas = useRef<HTMLDivElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const overviewGesture = useRef<{ x: number; start: number; daysPerPixel: number } | null>(null);
  const { start, density } = viewport;
  const level = zoomLevelFor(density), end = start + size.width / density;
  const axisY = Math.max(215, size.height * .53), layers = size.height >= 600 ? 2 : 1;
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
  const overviewStart = Math.min(today - 30, start, ...all.map(i => i.start - 20));
  const overviewEnd = Math.max(today + 90, end, ...all.map(i => i.end + 20));
  const overviewSpan = overviewEnd - overviewStart;
  const overviewPercent = (day: number) => (day - overviewStart) / overviewSpan * 100;

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
      if ((event.target as HTMLElement).closest('[data-cluster-panel]')) return;
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        const anchor = event.clientX - element.getBoundingClientRect().left;
        setViewport(v => { const next = Math.max(.25, Math.min(160, v.density * Math.exp(-event.deltaY * .008))); return { start: anchoredZoom(v.start, v.density, next, anchor), density: next }; });
      } else setViewport(v => ({ ...v, start: v.start + (event.deltaX || event.deltaY) / v.density }));
    };
    element.addEventListener('wheel', wheel, { passive: false });
    return () => element.removeEventListener('wheel', wheel);
    // `setViewport` 现在是 Provider 里那个 `useState` 的 setter,身份稳定 ——
    // 写进依赖数组不会让监听器重挂,只是把这件事说明白。
  }, [setViewport]);
  // 这里原本还有一个 effect:打开一份"本地提案"时把视口框到它涉及的那几天。
  // 那份提案是示例空间在浏览器里编的,现在没有了 —— 后端的提案走的是下面
  // 那条"确认后重拉计划"的路,不需要预览框。

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
   * 现在整个删了,两个原因都成立:
   *
   * 1. 它改的是一场真实存在的安排,而写入路径是「排期」(哪一天、多长时间),
   *    不是把 `startDate` 往前推几天 —— 后端根本没有这个字段。真正会写下去的
   *    只有 `deadline`,而那是**截止日**,悄悄拿它当排期用会改掉用户设的截止时间,
   *    界面上的说辞却是"调整了安排"。
   * 2. 它原来是配合"本地提案预览"用的(拖一下 → 出一份提案 → 用户点接受才生效),
   *    而那份提案是示例空间在浏览器内存里编的。示例空间删掉之后没有预览可给了。
   *
   * 让它拖起来再报错,不如**根本不开始拖** —— 手感上"拖不动"比"拖完了才说不支持"
   * 少一次白费的动作。改安排请走工作台的「排期」。
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
  function beginOverview(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    const rect = event.currentTarget.getBoundingClientRect(), daysPerPixel = overviewSpan / rect.width;
    const nextStart = (event.target as HTMLElement).closest('[data-viewport]') ? start : overviewStart + (event.clientX - rect.left) * daysPerPixel - size.width / density / 2;
    overviewGesture.current = { x: event.clientX, start: nextStart, daysPerPixel };
    event.currentTarget.setPointerCapture(event.pointerId); setViewport(v => ({ ...v, start: nextStart }));
  }
  function reveal(item: TimelineItem) { choose(item.node.id); setClusterOpen(false); setViewport(v => ({ ...v, start: item.start - size.width / v.density * .35 })); }
  return <div className={styles.view} data-testid="timeline-view" data-zoom={level}>
    <div className={styles.toolbar}>
      <div className={styles.scales} role="group" aria-label="时间尺度">{(Object.keys(zoomPresets) as ZoomLevel[]).map(z => <button key={z} aria-pressed={level === z} className={level === z ? styles.active : ''} onClick={() => zoomTo(zoomPresets[z])}>{zoomLabels[z]}</button>)}</div>
      <div className={styles.controls}><button aria-label="上一时段" onClick={() => setViewport(v => ({ ...v, start: v.start - size.width / density * .7 }))}><ChevronLeft size={14}/></button><button aria-label="缩小时间线" onClick={() => zoomTo(density / 1.5)}><Minus size={14}/></button><input aria-label="时间线缩放" type="range" min={Math.log(.25)} max={Math.log(160)} step="0.01" value={Math.log(density)} onChange={e => zoomTo(Math.exp(Number(e.target.value)))}/><button aria-label="放大时间线" onClick={() => zoomTo(density * 1.5)}><Plus size={14}/></button><button onClick={() => setViewport(v => ({ ...v, start: today - size.width / v.density * .28 }))}>今天</button><button aria-label="下一时段" onClick={() => setViewport(v => ({ ...v, start: v.start + size.width / density * .7 }))}><ChevronRight size={14}/></button></div>
    </div>
    <div className={styles.hint}>
      <span>拖动空白平移 · Ctrl / ⌘ + 滚轮缩放 · 卡片画的是已排的日期与截止时间，改安排请用「排期」</span>
      <span>{level === 'year' ? '目标与重要节点' : level === 'quarter' ? '阶段与主要安排' : level === 'month' ? '任务与里程碑' : level === 'week' ? '本周的具体安排' : '每天的小行动'}</span>
    </div>
    {planError && <div className={styles.hint} role="alert"><span>{planError}</span></div>}
    <div ref={canvas} className={styles.canvas} role="region" aria-label="成长时间线，方向键平移，加减键缩放" tabIndex={0} data-testid="timeline-canvas" data-ready={measured} data-start={start} data-density={density}
      onPointerDown={e => { if (e.button !== 0 || (e.target as HTMLElement).closest('button,input,[data-cluster-panel]')) return; e.currentTarget.setPointerCapture(e.pointerId); gesture.current = { x: e.clientX, start }; setClusterOpen(false); }}
      onPointerMove={move} onPointerUp={finish} onPointerCancel={finish}
      onKeyDown={e => { if (e.target !== e.currentTarget) return; if (['ArrowLeft', 'ArrowRight', '+', '=', '-', 'Home'].includes(e.key)) e.preventDefault(); if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') setViewport(v => ({ ...v, start: v.start + (e.key === 'ArrowLeft' ? -1 : 1) * size.width / density * .2 })); if (e.key === '+' || e.key === '=') zoomTo(density * 1.5); if (e.key === '-') zoomTo(density / 1.5); if (e.key === 'Home') setViewport(v => ({ ...v, start: today - size.width / density * .28 })); }}>
      <div className={styles.ruler}/>{years.map(year => <span key={year} className={styles.year} style={{ left: Math.max(18, x(dayNumber(`${year}-01-01`)) + 6) }}>{year}</span>)}
      {ticks.map(t => <div key={t.day} className={`${styles.tick} ${t.major ? styles.majorTick : ''}`} style={{ left: x(t.day) }}>{t.major && <span className={styles.tickLabel}>{t.label}</span>}</div>)}
      <div className={styles.axis} style={{ top: axisY }}/><div className={styles.past} style={{ top: axisY, width: Math.max(0, Math.min(size.width, x(today))) }}/><span className={styles.axisEnd} style={{ top: axisY }}>›</span>
      {x(today) >= 0 && x(today) <= size.width && <div className={styles.today} style={{ left: x(today) }} data-testid="today-marker"><span className={styles.todayLabel}>{shortDate(today)}<strong>今天</strong></span><span className={styles.todayDot} style={{ top: axisY - 86 }}/></div>}
      {placed.map(({ item, x: anchorX, left, lane, rangeLane }) => {
        const id = item.node.id, upper = lane % 2 === 0;
        const cardY = upper ? axisY - 112 - Math.floor(lane / 2) * 86 : axisY + 45 + Math.floor(lane / 2) * 86;
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
          {(hovered === id || selectedId === id) && item.start >= start && <div className={styles.hoverDate} style={{ left: anchorX }}><span>{shortDate(item.start)}</span></div>}
        </div>;
      })}
      {hidden.length > 0 && <button className={styles.cluster} style={{ left: size.width / 2, top: axisY + 19 }} aria-expanded={clusterOpen} onClick={() => setClusterOpen(!clusterOpen)}>另有 {hidden.length} 项 · 展开</button>}
      {clusterOpen && hidden.length > 0 && <div className={styles.clusterPanel} data-cluster-panel onPointerDown={e => e.stopPropagation()}><header>同一时段的其他安排<button aria-label="关闭其他安排" onClick={() => setClusterOpen(false)}><X size={14}/></button></header>{hidden.map(item => <button key={item.node.id} onClick={() => reveal(item)}>{item.node.title}<small>{dateString(item.start)} — {dateString(item.end)}</small></button>)}</div>}
      {/* 底下那条"没有日期的节点"和这句话抢同一个位置,所以它出现时把这句话往上抬。 */}
      {!placed.length && <p className={`${styles.empty} ${unscheduled.length ? styles.emptyWithNotice : ''}`}>这段时间暂无{level === 'day' ? '执行事项' : '安排'}，可以平移查看其他时间或切换尺度。</p>}
      {/* 画不出来的节点。**必须出现在界面上。** 时间线上有 3 项、任务列表里有 11 项,
          而用户没有任何别的办法知道少的那 8 项去哪了 —— 他会以为计划就是那 3 项。 */}
      {unscheduled.length > 0 && (
        <div className={styles.unscheduled} data-testid="unscheduled-items" role="status">
          <span className={styles.unscheduledLabel}>还有 {unscheduled.length} 项没有日期，暂不在时间线上</span>
          <span className={styles.unscheduledList}>
            {unscheduled.slice(0, 6).map(node => <button key={node.id} onClick={() => choose(node.id)}>{node.title}</button>)}
            {unscheduled.length > 6 && <span>…等 {unscheduled.length} 项</span>}
          </span>
          <span className={styles.unscheduledHint}>给它们加上截止时间，或者用「排期」把它们排到具体某天，就会出现在这里。</span>
        </div>
      )}
    </div>
    <div className={styles.overview} aria-label="整个计划的时间概览" data-testid="timeline-overview" onPointerDown={beginOverview} onPointerMove={e => { const drag = overviewGesture.current; if (drag) setViewport(v => ({ ...v, start: drag.start + (e.clientX - drag.x) * drag.daysPerPixel })); }} onPointerUp={() => { overviewGesture.current = null; }} onPointerCancel={() => { overviewGesture.current = null; }}>
      <div className={styles.overviewLine}/><span className={styles.overviewYear} style={{ left: 12 }}>{dateString(overviewStart).slice(0,4)}</span><span className={styles.overviewYear} style={{ right: 12 }}>{dateString(overviewEnd).slice(0,4)}</span>{all.filter(i => i.node.type !== 'goal').map(item => <span key={item.node.id} className={styles.overviewDot} style={{ left: `${overviewPercent(item.start)}%`, background: item.node.category ? colors[item.node.category] : undefined }}/>) }
      <div data-viewport className={styles.viewport} style={{ left: `${overviewPercent(start)}%`, width: `${(end - start) / overviewSpan * 100}%` }} role="slider" tabIndex={0} aria-label="概览视窗位置" aria-valuemin={Math.floor(overviewStart)} aria-valuemax={Math.ceil(overviewEnd)} aria-valuenow={Math.round(start)} aria-valuetext={`${dateString(start)} 至 ${dateString(end)}`} onKeyDown={e => { if (['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) { e.preventDefault(); setViewport(v => ({ ...v, start: e.key === 'Home' ? overviewStart : e.key === 'End' ? overviewEnd - size.width / density : v.start + (e.key === 'ArrowLeft' ? -1 : 1) * size.width / density * .2 })); } }}/>
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
