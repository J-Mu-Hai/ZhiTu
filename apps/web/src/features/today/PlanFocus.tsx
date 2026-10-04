'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Flag, ListTodo, Target, ArrowUpRight } from 'lucide-react';
import * as backend from '@/lib/backend';
import { todayInTimeZone } from '@/features/growth/timeline';

/**
 * 首页的「规划已生效」区域。
 *
 * ## 数据来源只有一份
 *
 * 它读的是**和任务面板 / 时间线同一份**已确认 `PlanNode`(`GET /plan`),不是为首页
 * 另建的前端临时计划。草案 proposal 不在这里 —— 未确认的东西不能显示成已生效。
 *
 * ## 它补的是什么
 *
 * 粗时间线确认后,首页以前毫无变化:用户刚确认完时间架构,回到首页看到的还是"今天
 * 没有排上具体的事"。这里按层级说清楚:
 *
 *   当前阶段 / 阶段目标 / 下一里程碑  ← 粗时间线确认后
 *   本周重点(未完成数 + 最高优先级)  ← 周计划确认后
 *   今天的工作块                      ← 日计划确认后
 *
 * 还没有周计划时,如实说"尚未细化",并给一个去「做起来」的入口 —— **不伪造今日任务**。
 */

interface PlanSnapshot {
  workspaceId: string;
  workspaceTitle: string;
  nodes: backend.PlanNodePayload[];
  sessions: backend.ScheduledSessionPayload[];
}

const isStage = (node: backend.PlanNodePayload) => node.nodeType === 'stage';
const childrenOf = (snapshot: PlanSnapshot, parentId: string) =>
  snapshot.nodes.filter(node => node.parentId === parentId);

function phaseDescription(node: backend.PlanNodePayload): string {
  const raw = (node.description ?? '').replace(/\s+/g, ' ').trim();
  const goal = raw.match(/目标[:：]\s*([^成果完成标准]+)/);
  return (goal?.[1] ?? raw).slice(0, 80);
}

export function PlanFocus({ revision }: { revision: unknown }) {
  const [snapshots, setSnapshots] = useState<PlanSnapshot[]>([]);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let active = true;
    void backend.listWorkspaces()
      .then(spaces => Promise.all(spaces.map(async space => {
        const plan = await backend.getPlan(space.id);
        return {
          workspaceId: space.id,
          workspaceTitle: space.title,
          nodes: plan.nodes,
          sessions: plan.sessions,
        };
      })))
      .then(all => { if (active) setSnapshots(all.filter(item => item.nodes.length > 0)); })
      .catch(() => { if (active) setSnapshots([]); })
      .finally(() => { if (active) setReady(true); });
    return () => { active = false; };
  }, [revision]);

  if (!ready) return null;

  const today = todayInTimeZone();
  // 选一个“正在推进”的空间:有阶段、并且有未完成的周任务;否则有阶段的第一个。
  const usable = snapshots.filter(snapshot => {
    const root = snapshot.nodes.find(node => node.parentId === null);
    return root && snapshot.nodes.some(node => isStage(node) && node.parentId === root.id);
  });
  if (usable.length === 0) return null;
  const withWork = usable.find(snapshot => {
    const root = snapshot.nodes.find(node => node.parentId === null)!;
    const week = snapshot.nodes.find(node => isStage(node) && /^本周计划/.test(node.title) && node.parentId !== root.id);
    return week ? childrenOf(snapshot, week.id).some(task => task.status !== 'completed') : false;
  });
  const snapshot = withWork ?? usable[0];
  const root = snapshot.nodes.find(node => node.parentId === null)!;
  const phases = snapshot.nodes.filter(node => isStage(node) && node.parentId === root.id);
  const phase = phases.find(node => node.status !== 'completed') ?? phases[0] ?? null;
  const phaseChildren = phase ? childrenOf(snapshot, phase.id) : [];
  const milestone = phaseChildren.find(node => node.title.startsWith('月度里程碑')) ?? phases[1] ?? null;
  const week = phaseChildren.find(node => node.title.startsWith('本周计划')) ?? null;
  const weekTasks = week ? childrenOf(snapshot, week.id).filter(node => node.nodeType === 'task') : [];
  const unfinished = weekTasks.filter(task => task.status !== 'completed');
  const priorityRank = (task: backend.PlanNodePayload) => (task.priority === 'high' ? 0 : task.priority === 'medium' ? 1 : 2);
  const topTask = [...unfinished].sort((a, b) => priorityRank(a) - priorityRank(b))[0] ?? null;
  const todayBlocks = snapshot.sessions.filter(session => session.scheduledDate === today);
  const workbench = (view: string) => `/workbench?workspace=${encodeURIComponent(snapshot.workspaceId)}&view=${view}`;

  if (!phase) return null;

  return (
    <section className="plan-focus" data-testid="plan-focus" aria-label="已确认的规划">
      <div className="plan-focus-grid">
        <div className="plan-focus-card">
          <span className="eyebrow"><Target size={12} /> 当前阶段</span>
          <h3>{phase.title}</h3>
          {phaseDescription(phase) && <p>{phaseDescription(phase)}</p>}
          <Link className="text-button" href={workbench('timeline')}>在时间线里看<ArrowUpRight size={12} /></Link>
        </div>

        <div className="plan-focus-card">
          <span className="eyebrow"><Flag size={12} /> 下一里程碑</span>
          {milestone ? <><h3>{milestone.title.replace(/^月度里程碑\s*·\s*/, '')}</h3><p>{phaseDescription(milestone)}</p></> : <p className="plan-focus-quiet">这个阶段还没有单独的里程碑。</p>}
        </div>

        <div className="plan-focus-card">
          <span className="eyebrow"><ListTodo size={12} /> 本周重点</span>
          {week ? (
            <>
              <h3>{unfinished.length > 0 ? `还有 ${unfinished.length} 件事没完成` : '本周任务都完成了'}</h3>
              {topTask && <p>优先：{topTask.title}</p>}
              <Link className="text-button" href={workbench('tasks')}>去任务面板<ArrowUpRight size={12} /></Link>
            </>
          ) : (
            <>
              <p className="plan-focus-quiet">时间架构已确认，还没细化成周任务。</p>
              <Link className="text-button" href={workbench('path')}>到「做起来」细化<ArrowUpRight size={12} /></Link>
            </>
          )}
        </div>
      </div>

      {week && todayBlocks.length === 0 && (
        <p className="plan-focus-note" role="status">
          本周计划已确认，尚未生成今天的工作块。
          <Link className="text-button" href={workbench('path')}>去「做起来」生成<ArrowUpRight size={12} /></Link>
        </p>
      )}
    </section>
  );
}
