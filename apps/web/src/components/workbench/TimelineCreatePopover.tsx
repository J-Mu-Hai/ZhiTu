'use client';
import { useState } from 'react';
import { CalendarPlus, Flag, ListTodo, Target, X } from 'lucide-react';
import styles from './TimelineView.module.css';

/**
 * 时间线上的**轻量创建入口**。
 *
 * 它出现在用户点击的月 / 周 / 日槽里(不是全局大弹窗),只做一件事:把用户输入的
 * 标题交给上层去写正式计划。写入成功上层会关闭它;写入失败它留在原地、**输入草稿
 * 不丢**,并把真实的失败原因显示在旁边 —— "保存失败但界面不说"是这个功能最容易
 * 犯的错。
 *
 * 三种尺度各自能做什么,由 `target` 决定,组件本身不做业务判断:
 * - 月:月度里程碑 / 任务(挂在对应阶段下);
 * - 周:创建该阶段的「本周计划」,或在该周计划下新增任务;
 * - 日:当天任务 / 日工作块(工作块会写一条排期场次)。
 */
export type CreateScale = 'month' | 'week' | 'day';

export interface TimelineCreateTarget {
  scale: CreateScale;
  /** 点击落在第几天(合成日轴)。 */
  day: number;
  /** 是给人看的槽位标签,例如「10月 · 第2周」「2026-10-08」。 */
  dayLabel: string;
  /** 挂到哪个正式阶段下。 */
  phaseId: string;
  phaseTitle: string;
  /** 周尺度:那周的周一(第几天),以及该周已有的活跃周计划 id。 */
  weekStart: number;
  existingWeekId: string | null;
  /** 摆在哪(相对画布)。 */
  x: number;
  y: number;
}

export type CreateKind = 'milestone' | 'task' | 'week-plan' | 'workblock';

export interface TimelineCreateSubmission {
  title: string;
  kind: CreateKind;
  /** 仅工作块使用,单位分钟。 */
  minutes?: number;
}

export function TimelineCreatePopover({
  target,
  saving,
  error,
  onSubmit,
  onClose,
}: {
  target: TimelineCreateTarget;
  saving: boolean;
  error: string | null;
  onSubmit: (submission: TimelineCreateSubmission) => Promise<boolean>;
  onClose: () => void;
}) {
  const isWeek = target.scale === 'week';
  const isDay = target.scale === 'day';
  const weekNeedsPlan = isWeek && !target.existingWeekId;
  // 周:没有周计划就只能先建周计划;有周计划就只能往里加任务。月和日给用户二选一。
  const [kind, setKind] = useState<CreateKind>(
    target.scale === 'month' ? 'milestone' : isWeek ? (weekNeedsPlan ? 'week-plan' : 'task') : 'workblock',
  );
  const [title, setTitle] = useState('');
  const [minutes, setMinutes] = useState('30');

  const effectiveKind: CreateKind = weekNeedsPlan ? 'week-plan' : kind;
  const titleRequired = effectiveKind !== 'week-plan';
  const canSubmit = !saving && (!titleRequired || title.trim().length > 0);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    const parsed = Number(minutes);
    const ok = await onSubmit({
      title: title.trim(),
      kind: effectiveKind,
      minutes: effectiveKind === 'workblock' && Number.isFinite(parsed) && parsed > 0 ? Math.round(parsed) : undefined,
    });
    // 成功才由上层关闭;失败时这里什么都不做,草稿留在输入框里。
    if (ok) setTitle('');
  }

  return (
    <form
      className={styles.createPopover}
      data-testid="timeline-create-popover"
      data-scale={target.scale}
      style={{ left: Math.max(8, Math.min(target.x, 9999)), top: Math.max(8, target.y) }}
      onSubmit={submit}
      onPointerDown={event => event.stopPropagation()}
      onClick={event => event.stopPropagation()}
    >
      <header>
        <strong>
          {target.scale === 'month' ? <Flag size={12} /> : target.scale === 'week' ? <CalendarPlus size={12} /> : <Target size={12} />}
          {target.dayLabel}
        </strong>
        <button type="button" aria-label="关闭新建" onClick={onClose} disabled={saving}><X size={13} /></button>
      </header>
      <p className={styles.createPhase}>挂在「{target.phaseTitle}」下</p>

      {target.scale === 'month' && (
        <div className={styles.createKinds} role="radiogroup" aria-label="新建类型">
          <button type="button" role="radio" aria-checked={kind === 'milestone'} className={kind === 'milestone' ? styles.createKindActive : ''} onClick={() => setKind('milestone')}>月度里程碑</button>
          <button type="button" role="radio" aria-checked={kind === 'task'} className={kind === 'task' ? styles.createKindActive : ''} onClick={() => setKind('task')}>任务</button>
        </div>
      )}
      {isDay && (
        <div className={styles.createKinds} role="radiogroup" aria-label="新建类型">
          <button type="button" role="radio" aria-checked={kind === 'workblock'} className={kind === 'workblock' ? styles.createKindActive : ''} onClick={() => setKind('workblock')}>日工作块</button>
          <button type="button" role="radio" aria-checked={kind === 'task'} className={kind === 'task' ? styles.createKindActive : ''} onClick={() => setKind('task')}>当天任务</button>
        </div>
      )}

      {weekNeedsPlan ? (
        <p className={styles.createHint}><ListTodo size={11} />这一周还没有周计划。先创建它,再往里加任务。</p>
      ) : (
        <label className={styles.createField}>
          标题
          <input
            autoFocus
            aria-label="新建标题"
            value={title}
            placeholder={effectiveKind === 'workblock' ? '这块时间做什么' : '例如:写第一版脚本'}
            onChange={event => setTitle(event.target.value)}
            disabled={saving}
          />
        </label>
      )}

      {effectiveKind === 'workblock' && (
        <label className={styles.createField}>
          预计分钟
          <input
            type="number"
            min={1}
            aria-label="工作块分钟数"
            value={minutes}
            onChange={event => setMinutes(event.target.value)}
            disabled={saving}
          />
        </label>
      )}

      {error && <p className={styles.createError} role="alert">{error}</p>}

      <div className={styles.createActions}>
        <button type="submit" className={styles.createSubmit} disabled={!canSubmit}>
          {saving ? '写入中…' : effectiveKind === 'week-plan' ? '创建本周计划' : isWeek ? '加入本周计划' : effectiveKind === 'workblock' ? '排入这一天' : '创建'}
        </button>
        <button type="button" onClick={onClose} disabled={saving}>取消</button>
      </div>
    </form>
  );
}
