'use client';
import { useMemo, useState } from 'react';
import { Check, Clock3, ArrowUpRight } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { isInSpace } from '@/features/growth/selectors';
import { categories } from '@/features/growth/categories';
import { todayInTimeZone, weekBounds } from '@/features/growth/timeline';
import type { GrowthNode } from '@/types/growth';

/**
 * 这个节点落在哪些日子上。
 *
 * 真实空间里是**排期算出来的场次**(可能很多场,一个任务分几次做);示例空间没有
 * 场次这个概念,它的 `scheduledDate` 就是那份演示数据里的"安排在哪天"。
 *
 * 两者的区别不是实现细节:场次是后端真的写下来的行,而 `scheduledDate` 在真实节点上
 * 只在场次存在时才有值(见 planProjection)—— 所以这个函数在真实空间里不会把
 * 截止时间误当成"这天要做"。
 */
function daysOf(node: GrowthNode): string[] {
  if (node.sessions?.length) return node.sessions.map((session) => session.date);
  return node.scheduledDate ? [node.scheduledDate] : [];
}

const fallsWithin = (node: GrowthNode, from: string, to: string) =>
  daysOf(node).some((day) => day >= from && day <= to);

export function TaskView() {
  const { growth, selectedId, select, apply, spaceId } = useDemo(); const [filter, setFilter] = useState('全部');
  // `purpose !== 'information'` 那一半不是可选的修饰:信息用途的节点**不进排期**
  // (它在服务端的排期查询里就被排掉了,「今天」也不会给它安排时间),把它当任务列在
  // 这里,用户会对着一个既没有勾选框、又永远不会出现在日历上的条目反复找原因。
  // 后端那边 `totalNodes` / `completedNodes` 用的是同一条判据。
  const tasks = Object.values(growth.nodes).filter(
    n => n.type === 'task' && n.purpose !== 'information' && isInSpace(growth, n.id, spaceId),
  );
  // "今天"和"本周"以前连着 `DEMO_TODAY` 和两个写死的日期 —— 见 timeline.ts 里
  // `todayInTimeZone` 的注释。现在跟着用户所在时区的真实日期走。
  const today = todayInTimeZone(); const week = weekBounds(today);

  /*
   * "本阶段"是哪一个阶段。
   *
   * 以前比的是 `growth.currentStageId`,而真实空间里它恒等于根目标 —— 于是所有挂在
   * 阶段下面的任务 **全都不算"本阶段"**,进任何一个阶段空间,进度条都是 0%。
   *
   * 现在的判据是"你现在看的是哪一层":进了一个阶段空间,本阶段就是它;在根这一层,
   * 本阶段就是直接挂在根下面的那些任务。`stageId` 由投影层从父链上算出来
   * (最近的 stage 祖先),后端没有这个字段。
   */
  const currentStage = growth.nodes[spaceId]?.type === 'stage' ? spaceId : growth.goalId;
  const stageTasks = tasks.filter(n => n.stageId === currentStage);
  const done = stageTasks.filter(n => n.status === 'completed').length;
  const progress = stageTasks.length ? Math.round(done / stageTasks.length * 100) : 0;

  const filtered = tasks.filter(n => filter === '全部' || (filter === '本阶段' && n.stageId === currentStage)
    || (filter === '今天' && fallsWithin(n, today, today))
    || (filter === '本周' && fallsWithin(n, week.start, week.end)));

  // 这个空间里到底有没有排过期的任务。空列表的原因有两种,说错哪一种都会把人引向
  // 错误的下一步:"还没排"该去排期,"这几天恰好没有"什么也不用做。
  const hasAnySession = Object.values(growth.nodes).some(n => n.sessions?.length);

  /*
   * 分组。
   *
   * 示例空间的 4 个分类是那份演示数据自带的(`category` 字段),后端**没有这个字段** ——
   * 而这里以前只按那 4 个分类分组,于是真实空间里 `filtered` 有 20 条、屏幕上一条都
   * 不显示:每一组的 `group.length` 都是 0。这正是"接口里有的东西界面上没有"的那类
   * 故障,只不过这次是静默地空着。
   *
   * 所以现在按**真实结构**兜底:没有分类就按它所属的阶段分组,那是后端真的有的东西。
   */
  const groups = useMemo(() => {
    const known = new Map(categories.map(category => [category.id as string, category]));
    const buckets = new Map<string, { id: string; className: string; title: string; number: string; tasks: GrowthNode[] }>();
    for (const node of filtered) {
      /*
       * 分组的**真实键**是 category 或 stageId,不是显示用的 id。
       *
       * 旧代码把**所有**非示例分类的桶都写成 `id: 'general'` —— 于是“阶段 A 的任务”
       * 和“阶段 B 的任务”是两个不同的桶,却拿到同一个 React key `general`,直接触发
       * “Encountered two children with the same key”。这里用稳定且有语义的复合 id:
       * 已知分类用分类 id;某个阶段用 `general:<stageId>`;没有阶段用 `general:ungrouped`。
       */
      const realKey = node.category ?? node.stageId ?? null;
      const bucketKey = realKey ?? 'ungrouped';
      const bucket = buckets.get(bucketKey);
      if (bucket) { bucket.tasks.push(node); continue; }
      const category = realKey ? known.get(realKey) : undefined;
      buckets.set(bucketKey, {
        id: category ? category.id : realKey ? `general:${realKey}` : 'general:ungrouped',
        className: category ? category.id : 'general',
        title: category ? category.title : realKey ? growth.nodes[realKey]?.title ?? '其他任务' : '其他任务',
        number: category ? category.number : '',
        tasks: [node],
      });
    }
    const order = categories.map(category => category.id as string);
    // 已知分类按示例数据里的顺序在前,其余的按标题排 —— 顺序必须是稳定的,否则
    // 每次重渲染这几组会在屏幕上换位置。
    return [...buckets.entries()]
      .sort(([a], [b]) => (order.indexOf(a) + 1 || order.length + 1) - (order.indexOf(b) + 1 || order.length + 1) || a.localeCompare(b))
      .map(([, bucket], index) => ({ ...bucket, number: bucket.number || String(index + 1).padStart(2, '0') }));
  }, [filtered, growth]);

  const stageNode = growth.nodes[currentStage];
  return <div className="task-view scroll-area"><div className="section-heading"><div><span className="eyebrow">MAKE IT HAPPEN</span><h2>让下一步，清晰一点。</h2></div><span className="muted">{filtered.length} 项任务</span></div>{filtered.length === 0 && <p className="empty-note">{filter === '今天' || filter === '本周'
    ? hasAnySession
      ? '这几天没有安排。计划里已经有排好的场次，切到别的时间范围看看。'
      : '这个范围里还没有安排到具体某天的任务。计划里的节点都带截止时间，但“哪天做”还没有排——打开「排期」预览一次，就能把它们落到具体日期上。'
    : '当前范围没有任务。可以回到路径，在这个空间添加一个新节点。'}</p>}<div className="filter-row">{['全部','本阶段','本周','今天'].map(f => <button key={f} className={filter === f ? 'active' : ''} onClick={() => setFilter(f)}>{f}</button>)}</div><div className="stage-summary"><div><span className="eyebrow">{currentStage === growth.goalId ? '当前空间' : '当前阶段'}{stageNode?.deadline ? ` · 截止 ${stageNode.deadline}` : ''}</span><h3>{stageNode?.title ?? growth.title}</h3></div>{stageTasks.length > 0 && <><strong>{progress}<small>%</small></strong><div className="progress-track"><span style={{ width: `${progress}%` }}/></div></>}</div>
    {groups.map(group => <section className={`task-group ${group.className}`} key={group.id}><header><span>{group.number}</span><h3>{group.title}</h3><small>{group.tasks.filter(n => n.status === 'completed').length} / {group.tasks.length}</small></header>{group.tasks.map(n => <div className={`task-row ${selectedId === n.id ? 'selected-row' : ''} ${n.status === 'completed' ? 'completed-row' : ''}`} key={n.id}><button className="task-check" aria-label={`${n.status === 'completed' ? '取消完成' : '完成'}${n.title}`} aria-pressed={n.status === 'completed'} onClick={() => apply({ type: 'UPDATE_STATUS', nodeId: n.id, status: n.status === 'completed' ? 'pending' : 'completed' })}>{n.status === 'completed' && <Check size={13}/>}</button><button className="task-detail" onClick={() => select(n.id)}><span>{n.title}{n.estimatedHours && <small><Clock3 size={11}/>预计 {n.estimatedHours}h</small>}</span><time>{n.scheduledDate?.slice(5).replace('-', ' / ')}</time><ArrowUpRight size={14}/></button></div>)}</section>)}
  </div>;
}
