'use client';
import { useState, useRef, useEffect, useMemo, type CSSProperties, type PointerEvent } from 'react';
import { ChevronLeft, ChevronRight, Minus, Plus, CalendarDays, X, Flag, Circle, Target } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { DEMO_TODAY } from '@/mock/growth-state';
import { shiftDate } from '@/features/growth/reducer';
import { anchoredZoom, dateString, dateToX, dayNumber, getVisibleItems, layoutItems, timelineItems, timelineTicks, zoomLabels, zoomLevelFor, zoomPresets, type TimelineItem, type ZoomLevel } from '@/features/growth/timeline';
import styles from './TimelineView.module.css';

const colors = { academic: '#749ce1', research: '#61ad9e', experience: '#c7a06e', personal: '#a294ce' };
const today = dayNumber(DEMO_TODAY);
const shortDate = (day: number) => { const d = new Date(day * 86400000); return `${d.getUTCMonth() + 1}.${d.getUTCDate()}`; };
const rangeLabel = (item: TimelineItem) => `${shortDate(item.start)}${item.end !== item.start ? ` — ${dateString(item.start).slice(0, 4) !== dateString(item.end).slice(0, 4) ? dateString(item.end).replaceAll('-', '.') : shortDate(item.end)}` : ''}`;
type Gesture = { kind: 'pan'; x: number; start: number } | { kind: 'item'; x: number; id: string; days: number };

export function TimelineView() {
  const { growth, selectedId, select, apply, impact, setImpact, proposals, previewProposalId, spaceId } = useDemo();
  const [viewport, setViewport] = useState({ start: today - 25, density: 4 });
  const [size, setSize] = useState({ width: 760, height: 570 });
  const [measured, setMeasured] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ id: string; days: number } | null>(null);
  const [clusterOpen, setClusterOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [startInput, setStartInput] = useState(''), [endInput, setEndInput] = useState('');
  const canvas = useRef<HTMLDivElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const overviewGesture = useRef<{ x: number; start: number; daysPerPixel: number } | null>(null);
  const { start, density } = viewport;
  const level = zoomLevelFor(density), end = start + size.width / density;
  const axisY = Math.max(215, size.height * .53), layers = size.height >= 600 ? 2 : 1;
  const cardWidth = Math.min(164, Math.max(126, size.width * .23));
  const all = useMemo(() => timelineItems(growth, spaceId), [growth, spaceId]);
  const proposal = impact ? proposals.find(p => p.id === previewProposalId && p.status === 'pending') : undefined;
  const changes = proposal?.actions.filter(a => a.type === 'UPDATE_TIME') ?? [];
  const displayItems = getVisibleItems(all, level).map(item => {
    const change = changes.find(a => a.nodeId === item.node.id), days = draft?.id === item.node.id ? draft.days : 0;
    return { ...item, start: (change ? dayNumber(change.startDate) : item.start) + days, end: (change ? dayNumber(change.endDate) : item.end) + days };
  });
  const selectedSource = all.find(i => i.node.id === selectedId);
  // Keep a deliberately selected object discoverable across semantic levels.
  if (selectedSource && !displayItems.some(i => i.node.id === selectedId)) {
    const change = changes.find(a => a.nodeId === selectedId);
    displayItems.push(change ? { ...selectedSource, start: dayNumber(change.startDate), end: dayNumber(change.endDate) } : { ...selectedSource, start: selectedSource.start + (draft?.id === selectedId ? draft.days : 0), end: selectedSource.end + (draft?.id === selectedId ? draft.days : 0) });
  }
  const { placed, hidden } = layoutItems(displayItems, start, density, size.width, selectedId, cardWidth, layers);
  const ticks = timelineTicks(start, end, level);
  const firstYear = new Date(start * 86400000).getUTCFullYear();
  const years = Array.from({ length: new Date(end * 86400000).getUTCFullYear() - firstYear + 1 }, (_, i) => firstYear + i);
  const x = (day: number) => dateToX(day, start, density);
  const selected = selectedId ? growth.nodes[selectedId] : null;
  const overviewStart = Math.min(today - 30, start, ...all.map(i => i.start - 20));
  const overviewEnd = Math.max(today + 90, end, ...all.map(i => i.end + 20), ...changes.map(a => dayNumber(a.endDate) + 20));
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
  }, []);
  useEffect(() => {
    if (!proposal) return;
    const ranges = proposal.actions.flatMap(a => a.type === 'UPDATE_TIME' ? [dayNumber(a.startDate), dayNumber(a.endDate), dayNumber(growth.nodes[a.nodeId].startDate ?? a.startDate)] : []);
    if (!ranges.length) return;
    const from = Math.min(...ranges) - 20, to = Math.max(...ranges) + 20;
    setViewport({ start: from, density: Math.min(8, size.width / (to - from)) });
    // Frame only a newly opened proposal; leave subsequent navigation to the user.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proposal?.id]);

  function zoomTo(nextDensity: number) {
    const next = Math.max(.25, Math.min(160, nextDensity));
    setViewport(v => ({ start: anchoredZoom(v.start, v.density, next, size.width / 2), density: next }));
    setClusterOpen(false);
  }
  function choose(id: string) { select(id); setEditing(false); }
  function beginItem(event: PointerEvent<HTMLButtonElement>, item: TimelineItem) {
    event.stopPropagation(); choose(item.node.id);
    if (event.button !== 0 || item.derived || proposal) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    gesture.current = { kind: 'item', x: event.clientX, id: item.node.id, days: 0 };
  }
  function move(event: PointerEvent) {
    const drag = gesture.current;
    if (!drag) return;
    if (drag.kind === 'pan') setViewport(v => ({ ...v, start: drag.start - (event.clientX - drag.x) / v.density }));
    else { drag.days = Math.round((event.clientX - drag.x) / density); setDraft({ id: drag.id, days: drag.days }); }
  }
  function finish() {
    const drag = gesture.current; gesture.current = null; setDraft(null);
    if (drag?.kind !== 'item' || !drag.days) return;
    const node = growth.nodes[drag.id], from = node.startDate ?? node.scheduledDate;
    if (from) apply({ type: 'UPDATE_TIME', nodeId: node.id, startDate: shiftDate(from, drag.days), endDate: shiftDate(node.endDate ?? from, drag.days) });
  }
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
    <div className={styles.hint}><span>拖动空白平移 · Ctrl / ⌘ + 滚轮缩放 · 拖动节点调整日期</span><span>{level === 'year' ? '目标与重要节点' : level === 'quarter' ? '阶段与主要安排' : level === 'month' ? '任务与里程碑' : level === 'week' ? '本周的具体安排' : '每天的小行动'}</span></div>
    {proposal && <div className={styles.previewBanner}><span>调整预览 · 虚线为原计划，实线为建议安排<br/>接受调整后才会更新计划</span><button onClick={() => setImpact(false)}>关闭预览</button></div>}
    <div ref={canvas} className={styles.canvas} role="region" aria-label="成长时间线，方向键平移，加减键缩放" tabIndex={0} data-testid="timeline-canvas" data-ready={measured} data-start={start} data-density={density}
      onPointerDown={e => { if (e.button !== 0 || (e.target as HTMLElement).closest('button,input,[data-cluster-panel]')) return; e.currentTarget.setPointerCapture(e.pointerId); gesture.current = { kind: 'pan', x: e.clientX, start }; setClusterOpen(false); }}
      onPointerMove={move} onPointerUp={finish} onPointerCancel={() => { gesture.current = null; setDraft(null); }}
      onKeyDown={e => { if (e.target !== e.currentTarget) return; if (['ArrowLeft', 'ArrowRight', '+', '=', '-', 'Home'].includes(e.key)) e.preventDefault(); if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') setViewport(v => ({ ...v, start: v.start + (e.key === 'ArrowLeft' ? -1 : 1) * size.width / density * .2 })); if (e.key === '+' || e.key === '=') zoomTo(density * 1.5); if (e.key === '-') zoomTo(density / 1.5); if (e.key === 'Home') setViewport(v => ({ ...v, start: today - size.width / density * .28 })); }}>
      <div className={styles.ruler}/>{years.map(year => <span key={year} className={styles.year} style={{ left: Math.max(18, x(dayNumber(`${year}-01-01`)) + 6) }}>{year}</span>)}
      {ticks.map(t => <div key={t.day} className={`${styles.tick} ${t.major ? styles.majorTick : ''}`} style={{ left: x(t.day) }}>{t.major && <span className={styles.tickLabel}>{t.label}</span>}</div>)}
      <div className={styles.axis} style={{ top: axisY }}/><div className={styles.past} style={{ top: axisY, width: Math.max(0, Math.min(size.width, x(today))) }}/><span className={styles.axisEnd} style={{ top: axisY }}>›</span>
      {x(today) >= 0 && x(today) <= size.width && <div className={styles.today} style={{ left: x(today) }} data-testid="today-marker"><span className={styles.todayLabel}>{shortDate(today)}<strong>今天</strong></span><span className={styles.todayDot} style={{ top: axisY - 86 }}/></div>}
      {placed.map(({ item, x: anchorX, left, lane, rangeLane }) => {
        const id = item.node.id, upper = lane % 2 === 0;
        const cardY = upper ? axisY - 112 - Math.floor(lane / 2) * 86 : axisY + 45 + Math.floor(lane / 2) * 86;
        const color = item.node.category ? colors[item.node.category] : '#829dc5';
        const original = all.find(i => i.node.id === id)!, changed = changes.some(a => a.nodeId === id), rangeY = axisY + 7 + rangeLane * 4;
        const Icon = item.kind === 'milestone' ? Flag : item.kind === 'goal' ? Target : Circle;
        return <div key={id} data-timeline-item={id} data-start-date={dateString(item.start)} data-end-date={dateString(item.end)} className={`${styles.object} ${selectedId === id ? styles.selected : ''} ${hovered && hovered !== id ? styles.dim : ''}`} style={{ '--color': color } as CSSProperties} onMouseEnter={() => setHovered(id)} onMouseLeave={() => setHovered(null)}>
          <svg className={styles.lines} aria-hidden="true"><path className={styles.connection} d={`M ${anchorX} ${axisY} V ${upper ? cardY + 84 : cardY - 12} L ${left + cardWidth / 2} ${upper ? cardY + 72 : cardY}`}/>
            {item.end > item.start && <><line className={styles.range} x1={Math.max(0, x(item.start))} x2={Math.min(size.width, x(item.end))} y1={rangeY} y2={rangeY}/>{[item.start,item.end].filter(d => x(d) >= 0 && x(d) <= size.width).map(d => <circle key={d} className={styles.endpoint} cx={x(d)} cy={rangeY} r="2.5"/>)}</>}
            {changed && <g className={styles.ghost} data-testid="plan-ghost"><line className={styles.range} x1={x(original.start)} x2={x(original.end)} y1={axisY - 15} y2={axisY - 15}/><circle className={styles.endpoint} cx={x(original.start)} cy={axisY - 15} r="4"/><circle className={styles.endpoint} cx={x(original.end)} cy={axisY - 15} r="3"/></g>}
          </svg>
          {changed && <span className={styles.ghostLabel} style={{ left: Math.max(64, Math.min(size.width - 64, x(original.start))), top: axisY - 33 }}>原计划 {rangeLabel(original)}</span>}
          <button className={`${styles.point} ${item.start < start ? styles.continuation : item.kind === 'milestone' ? styles.milestone : item.kind === 'goal' ? styles.goal : ''}`} style={{ left: anchorX, top: axisY }} aria-label={`${item.node.title}${item.start < start ? '从此前延续' : '时间点'}`} onClick={() => choose(id)} onPointerDown={e => beginItem(e, item)}/>
          <button data-timeline-card className={`${styles.card} ${item.kind !== 'duration' ? styles.eventCard : ''}`} style={{ left, top: cardY, width: cardWidth }} aria-label={`${item.node.title}，${dateString(item.start)}至${dateString(item.end)}`} aria-pressed={selectedId === id} title={`${item.node.title} · ${dateString(item.start)} — ${dateString(item.end)}${item.derived ? '（根据子任务推导）' : ''}`} onClick={() => choose(id)} onPointerDown={e => beginItem(e, item)}>
            <time><Icon size={11}/>{rangeLabel(item)}</time><strong>{item.node.title}</strong><small className={changed ? styles.previewTag : ''}>{changed ? '建议安排 · 等待接受' : item.derived ? '计划范围 · 根据子任务推导' : item.node.status === 'completed' ? '已完成' : item.start < start ? '此前开始 · 持续进行' : item.node.description ?? (item.kind === 'milestone' ? '重要节点' : item.kind === 'duration' ? '持续安排' : '当日行动')}</small>
          </button>
          {(hovered === id || selectedId === id) && item.start >= start && <div className={styles.hoverDate} style={{ left: anchorX }}><span>{shortDate(item.start)}</span></div>}
        </div>;
      })}
      {hidden.length > 0 && <button className={styles.cluster} style={{ left: size.width / 2, top: axisY + 19 }} aria-expanded={clusterOpen} onClick={() => setClusterOpen(!clusterOpen)}>另有 {hidden.length} 项 · 展开</button>}
      {clusterOpen && hidden.length > 0 && <div className={styles.clusterPanel} data-cluster-panel onPointerDown={e => e.stopPropagation()}><header>同一时段的其他安排<button aria-label="关闭其他安排" onClick={() => setClusterOpen(false)}><X size={14}/></button></header>{hidden.map(item => <button key={item.node.id} onClick={() => reveal(item)}>{item.node.title}<small>{dateString(item.start)} — {dateString(item.end)}</small></button>)}</div>}
      {!placed.length && <p className={styles.empty}>这段时间暂无{level === 'day' ? '执行事项' : '安排'}，可以平移查看其他时间或切换尺度。</p>}
    </div>
    <div className={styles.overview} aria-label="整个计划的时间概览" data-testid="timeline-overview" onPointerDown={beginOverview} onPointerMove={e => { const drag = overviewGesture.current; if (drag) setViewport(v => ({ ...v, start: drag.start + (e.clientX - drag.x) * drag.daysPerPixel })); }} onPointerUp={() => { overviewGesture.current = null; }} onPointerCancel={() => { overviewGesture.current = null; }}>
      <div className={styles.overviewLine}/><span className={styles.overviewYear} style={{ left: 12 }}>{dateString(overviewStart).slice(0,4)}</span><span className={styles.overviewYear} style={{ right: 12 }}>{dateString(overviewEnd).slice(0,4)}</span>{all.filter(i => i.node.type !== 'goal').map(item => <span key={item.node.id} className={styles.overviewDot} style={{ left: `${overviewPercent(item.start)}%`, background: item.node.category ? colors[item.node.category] : undefined }}/>) }
      <div data-viewport className={styles.viewport} style={{ left: `${overviewPercent(start)}%`, width: `${(end - start) / overviewSpan * 100}%` }} role="slider" tabIndex={0} aria-label="概览视窗位置" aria-valuemin={Math.floor(overviewStart)} aria-valuemax={Math.ceil(overviewEnd)} aria-valuenow={Math.round(start)} aria-valuetext={`${dateString(start)} 至 ${dateString(end)}`} onKeyDown={e => { if (['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) { e.preventDefault(); setViewport(v => ({ ...v, start: e.key === 'Home' ? overviewStart : e.key === 'End' ? overviewEnd - size.width / density : v.start + (e.key === 'ArrowLeft' ? -1 : 1) * size.width / density * .2 })); } }}/>
    </div>
    {selectedSource && <div className={styles.inspector} data-testid="date-inspector"><div><strong>{selectedSource.node.title}</strong><span>{dateString(selectedSource.start)} → {dateString(selectedSource.end)}{selectedSource.derived ? ' · 推导范围' : ''}</span></div>{!selectedSource.derived && <button onClick={() => { setStartInput(dateString(selectedSource.start)); setEndInput(dateString(selectedSource.end)); setEditing(true); }}><CalendarDays size={14}/>编辑日期</button>}
      {editing && selected && <form className={styles.editor} onSubmit={e => { e.preventDefault(); if (startInput && endInput >= startInput) { apply({ type: 'UPDATE_TIME', nodeId: selected.id, startDate: startInput, endDate: endInput }); setEditing(false); } }}><label>开始<input aria-label="开始日期" type="date" required value={startInput} onChange={e => setStartInput(e.target.value)}/></label><label>结束<input aria-label="结束日期" type="date" min={startInput} required value={endInput} onChange={e => setEndInput(e.target.value)}/></label><button type="submit" disabled={!startInput || !endInput || endInput < startInput}>应用</button><button type="button" aria-label="取消编辑" onClick={() => setEditing(false)}><X size={15}/></button></form>}
    </div>}
  </div>;
}
