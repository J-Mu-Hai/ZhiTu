'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { CalendarRange, Check, ChevronRight, Plus, Sun, X } from 'lucide-react';
import * as backend from '@/lib/backend';
import { ApiError } from '@/lib/api';
import { useDemo } from '@/features/growth/provider';
import { useTodayPlans } from './useTodayPlans';

/**
 * 首页下方真正可用的计划区:**本周计划 / 本日计划**两个页签(默认本日计划)。
 *
 * ## 为什么是跨空间聚合
 *
 * 用户打开首页问的是"我这周/今天要做什么",不是"我这个空间要做什么"。数据来自
 * `GET /api/today/plans` —— 一次查完全部活动空间的正式计划,而不是逐个空间拉
 * `/plan`(请求数会随空间数增长,首页控制不住)。
 *
 * ## 写入走正式接口
 *
 * 勾选完成写 `PATCH /nodes/{id}`(节点状态),添加任务写 `POST /nodes` /
 * `POST /week-plans` / `POST /sessions`。"先创建本周计划"也是**用户明确勾选**的,
 * 不是系统替他生成;AI 的自动生成仍然走 proposal → 确认。
 *
 * ## 同步
 *
 * 写入成功后重取聚合数据;若目标空间正是当前 provider 所在空间,再 `refreshPlan()`,
 * 于是任务面板、时间线不用等切页就一致。
 */

type Tab = 'week' | 'day';

function clockLabel(startMinute: number | null, endMinute: number | null): string | null {
  if (startMinute === null) return null;
  const at = (minute: number) => `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
  return endMinute === null ? at(startMinute) : `${at(startMinute)}–${at(endMinute)}`;
}

/** 取任务最靠前的一个日期:有排期用排期,没有就退到截止日。 */
function taskDate(task: backend.AgendaWeekTaskView): string | null {
  const dates = task.sessions.map(session => session.scheduledDate).sort();
  return dates[0] ?? task.deadline ?? null;
}

function monthDay(value: string): string {
  return value.slice(5).replace('-', '.');
}

export function TodayPlanTabs({ onChanged }: { onChanged?: () => void } = {}) {
  const { data, error, loading, refresh, setError } = useTodayPlans();
  const { workspaceId: activeWorkspaceId, isRealSpace, refreshPlan } = useDemo();
  const [tab, setTab] = useState<Tab>('day');
  const [adding, setAdding] = useState(false);
  const [busyNode, setBusyNode] = useState<string | null>(null);

  /** 写入成功后的统一收口:重取聚合;目标是当前空间时同时刷新 provider plan。 */
  async function afterWrite(workspaceId: string) {
    await refresh();
    if (isRealSpace && workspaceId === activeWorkspaceId) await refreshPlan();
    // 顶部「本周时间线」也读同一份聚合,让它一起重取。
    onChanged?.();
  }

  async function toggle(nodeId: string, workspaceId: string, status: string) {
    setBusyNode(nodeId);
    setError(null);
    try {
      await backend.updateNode(workspaceId, nodeId, {
        status: status === 'completed' ? 'pending' : 'completed',
      });
      await afterWrite(workspaceId);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '状态没有保存上,请重试。');
    } finally {
      setBusyNode(null);
    }
  }

  /** 把本周清单中的一项明确放到“今天”。这不是复制任务，只为同一个任务建立今日场次。 */
  async function putOnToday(task: backend.AgendaWeekTaskView, workspaceId: string) {
    setBusyNode(task.nodeId);
    setError(null);
    try {
      await backend.createSession(workspaceId, {
        nodeId: task.nodeId,
        scheduledDate: data?.today ?? new Date().toISOString().slice(0, 10),
        plannedMinutes: task.estimateMinutes ?? 30,
      });
      await afterWrite(workspaceId);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '没有加入今天的安排，请重试。');
    } finally {
      setBusyNode(null);
    }
  }

  const weekGroups = useMemo(() => {
    const groups = new Map<string, { title: string; plans: backend.AgendaWeekPlanView[] }>();
    for (const plan of data?.weekPlans ?? []) {
      const existing = groups.get(plan.workspaceId);
      if (existing) existing.plans.push(plan);
      else groups.set(plan.workspaceId, { title: plan.workspaceTitle, plans: [plan] });
    }
    return [...groups.entries()];
  }, [data]);

  const isEmptyWeek = (data?.weekPlans.length ?? 0) === 0;
  const isEmptyDay = (data?.todayItems.length ?? 0) === 0;

  return (
    <section className="today-plan" data-testid="today-plan" aria-label="计划">
      <div className="today-plan-tabs" role="tablist" aria-label="计划范围">
        <button type="button" role="tab" aria-selected={tab === 'week'} className={tab === 'week' ? 'selected' : ''} onClick={() => { setTab('week'); setAdding(false); setError(null); }}>
          <CalendarRange size={15} />本周计划
        </button>
        <button type="button" role="tab" aria-selected={tab === 'day'} className={tab === 'day' ? 'selected' : ''} onClick={() => { setTab('day'); setAdding(false); setError(null); }}>
          <Sun size={15} />本日计划
        </button>
      </div>

      {error && <p className="today-plan-error" role="alert">{error}</p>}

      {adding && data && (
        <AgendaAddForm
          mode={tab}
          data={data}
          onClose={() => setAdding(false)}
          onSaved={afterWrite}
        />
      )}

      {!adding && (
        <div className="today-plan-actions">
          <button type="button" className="today-plan-add" onClick={() => { setAdding(true); setError(null); }}>
            <Plus size={14} />{tab === 'week' ? '添加本周任务' : '添加今日任务'}
          </button>
        </div>
      )}

      {loading && !data && <p className="today-plan-note">正在读取计划…</p>}

      {data && tab === 'week' && (
        isEmptyWeek ? (
          <div className="today-plan-empty" data-testid="week-empty">
            <p>本周还没有已确认计划。</p>
            {!adding && <button type="button" className="today-plan-add" onClick={() => setAdding(true)}><Plus size={14} />添加本周任务</button>}
          </div>
        ) : (
          <div className="today-plan-groups" data-testid="week-groups">
            {weekGroups.map(([workspaceId, group]) => (
              <section className="today-plan-group" key={workspaceId} data-workspace-id={workspaceId}>
                <header><span className="tiny-dot" />{group.title}</header>
                {group.plans.map(plan => (
                  <div className="today-plan-subgroup" key={plan.planNodeId} data-testid="week-plan" data-plan-id={plan.planNodeId}>
                    <div className="today-plan-subhead">{plan.stageTitle ?? '未指定阶段'}</div>
                    {plan.tasks.length === 0 && <p className="today-plan-quiet">这一周还没有任务。</p>}
                    {plan.tasks.map(task => {
                      const date = taskDate(task);
                      const done = task.status === 'completed';
                      const isToday = task.sessions.some(session => session.scheduledDate === data.today);
                      return (
                        <div className={`today-plan-row${done ? ' done' : ''}`} key={task.nodeId} data-testid="week-task" data-node-id={task.nodeId}>
                          <button type="button" className="task-check" aria-label={`${done ? '取消完成' : '完成'}${task.title}`} aria-pressed={done} disabled={busyNode === task.nodeId} onClick={() => void toggle(task.nodeId, plan.workspaceId, task.status)}>
                            {done && <Check size={13} />}
                          </button>
                          <div className="today-plan-row-main">
                            <Link className="today-plan-title" href={`/workbench?workspace=${encodeURIComponent(plan.workspaceId)}&view=tasks`}>{task.title}</Link>
                            <span className="today-plan-meta">
                              {plan.workspaceTitle}
                              {plan.stageTitle && ` · ${plan.stageTitle}`}
                              {date && ` · ${monthDay(date)}`}
                              {task.sessions.length === 0 && ' · 待安排'}
                            </span>
                          </div>
                          <button
                            type="button"
                            className="today-plan-pick"
                            disabled={isToday || busyNode === task.nodeId || done}
                            onClick={() => void putOnToday(task, plan.workspaceId)}
                          >
                            {isToday ? '已安排今天' : '安排到今天'}
                          </button>
                          <ChevronRight size={14} />
                        </div>
                      );
                    })}
                  </div>
                ))}
              </section>
            ))}
          </div>
        )
      )}

      {data && tab === 'day' && (
        isEmptyDay ? (
          <div className="today-plan-empty" data-testid="day-empty">
            <p>今天还没有安排，可以从这里添加一件事。</p>
            {!adding && <button type="button" className="today-plan-add" onClick={() => setAdding(true)}><Plus size={14} />添加今日任务</button>}
          </div>
        ) : (
          <div className="today-plan-list" data-testid="day-list">
            {data.todayItems.map(item => {
              const done = item.nodeStatus === 'completed';
              const clock = clockLabel(item.startMinute, item.endMinute);
              const key = item.sessionId ?? item.nodeId;
              return (
                <div className={`today-plan-row${done ? ' done' : ''}`} key={key} data-testid="day-item" data-node-id={item.nodeId}>
                  <button type="button" className="task-check" aria-label={`${done ? '取消完成' : '完成'}${item.title}`} aria-pressed={done} disabled={busyNode === item.nodeId} onClick={() => void toggle(item.nodeId, item.workspaceId, item.nodeStatus)}>
                    {done && <Check size={13} />}
                  </button>
                  <div className="today-plan-row-main">
                    <Link className="today-plan-title" href={`/workbench?workspace=${encodeURIComponent(item.workspaceId)}&view=timeline`}>{item.title}</Link>
                    <span className="today-plan-meta">
                      {item.workspaceTitle}
                      {item.stageTitle && ` · ${item.stageTitle}`}
                      {` · ${clock ?? (item.plannedMinutes ? `${item.plannedMinutes} 分钟 · 待安排` : '待安排')}`}
                    </span>
                  </div>
                  <span className="today-plan-time">{clock ?? '待安排'}</span>
                </div>
              );
            })}
          </div>
        )
      )}
    </section>
  );
}

/**
 * 轻量添加表单。
 *
 * 周/日两种模式共用一套选择:空间 -> 阶段 -> (若该阶段本周没有周计划)是否先创建。
 * 提交走正式接口;失败留在原地,输入不丢。
 */
function AgendaAddForm({ mode, data, onClose, onSaved }: {
  mode: Tab;
  data: backend.TodayPlansResponse;
  onClose: () => void;
  onSaved: (workspaceId: string) => Promise<void>;
}) {
  const usable = data.targets.filter(target => target.phases.length > 0);
  const [workspaceId, setWorkspaceId] = useState(usable[0]?.workspaceId ?? data.targets[0]?.workspaceId ?? '');
  const current = data.targets.find(target => target.workspaceId === workspaceId) ?? usable[0] ?? data.targets[0];
  const phases = current?.phases ?? [];
  const [stageId, setStageId] = useState(phases[0]?.stageId ?? '');
  const phase = phases.find(item => item.stageId === stageId) ?? phases[0];
  const [title, setTitle] = useState('');
  const [date, setDate] = useState(mode === 'day' ? data.today : '');
  const [description, setDescription] = useState('');
  const [time, setTime] = useState('');
  const [minutes, setMinutes] = useState('30');
  const [createPlan, setCreatePlan] = useState(true);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const needsPlan = Boolean(phase) && !phase!.weekPlanId;
  const canSubmit = !saving && title.trim().length > 0 && Boolean(phase) && (!needsPlan || mode === 'day' || createPlan);

  function changeWorkspace(nextId: string) {
    setWorkspaceId(nextId);
    const next = data.targets.find(target => target.workspaceId === nextId);
    setStageId(next?.phases[0]?.stageId ?? '');
  }

  function parsedStartMinute(): number | null {
    if (!time) return null;
    const [hours, mins] = time.split(':').map(Number);
    if (!Number.isFinite(hours) || !Number.isFinite(mins)) return null;
    return Math.max(0, Math.min(24 * 60 - 1, hours * 60 + mins));
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit || !phase) return;
    setSaving(true);
    setFormError(null);
    try {
      let parentId = phase.weekPlanId;
      if (!parentId && (mode === 'week' || createPlan)) {
        const plan = await backend.createWeekPlan(workspaceId, {
          parentId: phase.stageId,
          weekStart: data.weekStart,
        });
        parentId = plan.node.id;
      }
      parentId = parentId ?? phase.stageId;

      if (mode === 'week') {
        await backend.createNode(workspaceId, {
          parentId,
          title: title.trim(),
          nodeType: 'task',
          planningLevel: 'week',
          deadline: date || null,
          description: description.trim() || null,
        });
      } else {
        const created = await backend.createNode(workspaceId, {
          parentId,
          title: title.trim(),
          nodeType: 'task',
          planningLevel: 'day',
          deadline: date,
          description: description.trim() || null,
        });
        const parsed = Number(minutes);
        const planned = Number.isFinite(parsed) && parsed > 0 ? Math.round(parsed) : 30;
        const startMinute = parsedStartMinute();
        const endMinute = startMinute === null ? null : Math.min(24 * 60, startMinute + planned);
        await backend.createSession(workspaceId, {
          nodeId: created.node.id,
          scheduledDate: date,
          plannedMinutes: planned,
          ...(startMinute !== null && endMinute !== null && endMinute > startMinute ? { startMinute, endMinute } : {}),
        });
      }
      await onSaved(workspaceId);
      onClose();
    } catch (cause) {
      // **失败留在原地,输入不丢**。真实错误照实显示。
      setFormError(cause instanceof ApiError ? cause.message : '没有保存上,请重试。');
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="today-plan-form" data-testid="today-plan-form" onSubmit={submit}>
      <header>
        <strong>{mode === 'week' ? '添加本周任务' : '添加今日任务'}</strong>
        <button type="button" aria-label="关闭添加" onClick={onClose} disabled={saving}><X size={14} /></button>
      </header>

      {usable.length === 0 ? (
        <p className="today-plan-error">还没有可用的空间阶段。先在工作台里给空间建一个阶段。</p>
      ) : (
        <>
          <div className="today-plan-form-row">
            <label>空间
              <select value={workspaceId} onChange={event => changeWorkspace(event.target.value)} disabled={saving}>
                {data.targets.map(target => (
                  <option key={target.workspaceId} value={target.workspaceId}>{target.workspaceTitle}</option>
                ))}
              </select>
            </label>
            <label>所属阶段
              <select value={phase?.stageId ?? ''} onChange={event => setStageId(event.target.value)} disabled={saving || phases.length === 0}>
                {phases.map(item => <option key={item.stageId} value={item.stageId}>{item.stageTitle}</option>)}
              </select>
            </label>
          </div>

          <label>标题
            <input autoFocus value={title} onChange={event => setTitle(event.target.value)} placeholder={mode === 'week' ? '例如:写第一版脚本' : '例如:整理今天的笔记'} disabled={saving} />
          </label>

          {needsPlan && (
            <label className="today-plan-check">
              <input type="checkbox" checked={createPlan} onChange={event => setCreatePlan(event.target.checked)} disabled={saving} />
              这个阶段本周还没有周计划，先创建「本周计划」再添加
            </label>
          )}

          <div className="today-plan-form-row">
            <label>{mode === 'week' ? '日期(可选)' : '日期'}
              <input type="date" value={date} onChange={event => setDate(event.target.value)} disabled={saving} required={mode === 'day'} />
            </label>
            {mode === 'day' && (
              <>
                <label>开始时间(可选)
                  <input type="time" value={time} onChange={event => setTime(event.target.value)} disabled={saving} />
                </label>
                <label>预计分钟
                  <input type="number" min={1} value={minutes} onChange={event => setMinutes(event.target.value)} disabled={saving} />
                </label>
              </>
            )}
          </div>

          <label>说明(可选)
            <input value={description} onChange={event => setDescription(event.target.value)} disabled={saving} />
          </label>

          {formError && <p className="today-plan-error" role="alert">{formError}</p>}

          <div className="today-plan-form-actions">
            <button type="submit" className="primary-button" disabled={!canSubmit}>{saving ? '写入中…' : '添加'}</button>
            <button type="button" onClick={onClose} disabled={saving}>取消</button>
          </div>
        </>
      )}
    </form>
  );
}
