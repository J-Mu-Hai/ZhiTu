'use client';
import { useState } from 'react';
import { Check, Clock3, ArrowUpRight } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { isInSpace } from '@/features/growth/selectors';
import { categories, DEMO_TODAY } from '@/mock/growth-state';
export function TaskView() {
  const { growth, selectedId, select, apply, spaceId } = useDemo(); const [filter, setFilter] = useState('全部');
  const tasks = Object.values(growth.nodes).filter(n => n.type === 'task' && isInSpace(growth, n.id, spaceId));
  const stageTasks = tasks.filter(n => n.stageId === growth.currentStageId); const done = stageTasks.filter(n => n.status === 'completed').length;
  const progress = stageTasks.length ? Math.round(done / stageTasks.length * 100) : 0;
  const filtered = tasks.filter(n => filter === '全部' || (filter === '本阶段' && n.stageId === growth.currentStageId) || (filter === '今天' && n.scheduledDate === DEMO_TODAY) || (filter === '本周' && n.scheduledDate && n.scheduledDate >= '2026-09-14' && n.scheduledDate <= '2026-09-20'));
  return <div className="task-view scroll-area"><div className="section-heading"><div><span className="eyebrow">MAKE IT HAPPEN</span><h2>让下一步，清晰一点。</h2></div><span className="muted">{filtered.length} 项任务</span></div>{filtered.length === 0 && <p className="empty-note">当前范围没有任务。可以回到路径，在这个空间添加一个新节点。</p>}<div className="filter-row">{['全部','本阶段','本周','今天'].map(f => <button key={f} className={filter === f ? 'active' : ''} onClick={() => setFilter(f)}>{f}</button>)}</div><div className="stage-summary"><div><span className="eyebrow">当前阶段 · SEP — DEC 2026</span><h3>{growth.nodes[growth.currentStageId].title}</h3></div><strong>{progress}<small>%</small></strong><div className="progress-track"><span style={{ width: `${progress}%` }}/></div></div>
    {categories.map(c => { const group = filtered.filter(n => n.category === c.id); return group.length ? <section className={`task-group ${c.id}`} key={c.id}><header><span>{c.number}</span><h3>{c.title}</h3><small>{group.filter(n => n.status === 'completed').length} / {group.length}</small></header>{group.map(n => <div className={`task-row ${selectedId === n.id ? 'selected-row' : ''} ${n.status === 'completed' ? 'completed-row' : ''}`} key={n.id}><button className="task-check" aria-label={`${n.status === 'completed' ? '取消完成' : '完成'}${n.title}`} aria-pressed={n.status === 'completed'} onClick={() => apply({ type: 'UPDATE_STATUS', nodeId: n.id, status: n.status === 'completed' ? 'pending' : 'completed' })}>{n.status === 'completed' && <Check size={13}/>}</button><button className="task-detail" onClick={() => select(n.id)}><span>{n.title}{n.estimatedHours && <small><Clock3 size={11}/>预计 {n.estimatedHours}h</small>}</span><time>{n.scheduledDate?.slice(5).replace('-', ' / ')}</time><ArrowUpRight size={14}/></button></div>)}</section> : null; })}
  </div>;
}
