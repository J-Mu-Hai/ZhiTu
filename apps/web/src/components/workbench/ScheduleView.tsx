'use client';
import { useCallback, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CalendarCheck, CalendarRange, Check, Clock3, Loader2, RefreshCw, Sparkles } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { todayInTimeZone, weekBounds } from '@/features/growth/timeline';
import { ApiError } from '@/lib/api';
import {
  applySchedule,
  bindingConstraintLabel,
  gapReasonLabel,
  previewSchedule,
  type ScheduleApplyResponse,
  type SchedulePreviewResponse,
  type ScheduledSessionPayload,
} from '@/lib/backend';
import styles from './ScheduleView.module.css';

/**
 * 排期 —— 周计划与日计划,以及"预览 → 应用"这条写入路径。
 *
 * ## 为什么预览和应用是两个按钮,而不是"一键排好"
 *
 * 排期会动用户日历里**已经存在**的安排(挪走几场、取消几场)。这些变化如果不由用户
 * 点头,他第二天打开时会发现整个日历都变了,而没有任何地方解释为什么 —— 那正是
 * "系统每天偷偷改计划"的来源。所以:预览只读,应用才写,而且应用的那一份必须是
 * 用户刚刚在屏幕上看到的那一份(靠 `scheduleVersion` 校验)。
 *
 * ## 为什么这里要说"覆盖了哪几个空间"
 *
 * 时间池按**人**算,不按空间算:同一晚只能做一件事。所以从这里点"应用",另一个
 * 空间里的任务也会被排上 —— 那是正确的行为,但如果界面不说,用户在另一个空间看到
 * 自己的任务被挪了日子,只会认为系统乱动了他的计划。
 */
export function ScheduleView() {
  const { plan, isRealSpace, workspaceId, planError, setPlanError, refreshPlan, planLoading } = useDemo();
  const [preview, setPreview] = useState<SchedulePreviewResponse | null>(null);
  const [applied, setApplied] = useState<ScheduleApplyResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAllDays, setShowAllDays] = useState(false);

  /**
   * 幂等键:每份预览一个,生成一次之后**重试复用同一个**。
   *
   * 每次点击都新生成一个的话,用户双击"应用"就是两次不同的请求 —— 后端那两道闸门
   * (守卫写 + 幂等台账)本来就是为了挡住这件事,而一个每次都变的键会让它们都失效。
   * 重新预览拿到的是另一个版本号,那是**另一次**应用,于是配一个新键。
   */
  const keyRef = useRef<{ version: string; key: string } | null>(null);
  const keyFor = (version: string): string => {
    if (keyRef.current?.version !== version) {
      keyRef.current = { version, key: crypto.randomUUID() };
    }
    return keyRef.current.key;
  };

  const today = todayInTimeZone();
  const week = weekBounds(today);
  // `?? []` 每次渲染都会造一个新数组,直接把它当 `useMemo` 的依赖会让下面那个分组
  // **每次渲染都重算**(缓存形同虚设)。按数据本身记忆,而不是按渲染次数。
  const sessions = useMemo(() => plan?.sessions ?? [], [plan]);

  const byDay = useMemo(() => {
    const grouped = new Map<string, ScheduledSessionPayload[]>();
    for (const session of sessions) {
      const list = grouped.get(session.scheduledDate);
      if (list) list.push(session);
      else grouped.set(session.scheduledDate, [session]);
    }
    return [...grouped.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [sessions]);

  const todaySessions = byDay.find(([day]) => day === today)?.[1] ?? [];
  const weekDays = byDay.filter(([day]) => day >= week.start && day <= week.end);
  const laterCount = sessions.length - byDay.filter(([day]) => day <= week.end).reduce((sum, [, list]) => sum + list.length, 0);

  const runPreview = useCallback(async () => {
    setBusy(true); setError(null); setApplied(null); setPlanError(null);
    try {
      setPreview(await previewSchedule(workspaceId));
    } catch (cause) {
      setPreview(null);
      setError(cause instanceof ApiError ? cause.message : '预览排期失败。');
    } finally {
      setBusy(false);
    }
  }, [setPlanError, workspaceId]);

  const runApply = useCallback(async () => {
    if (!preview) return;
    setBusy(true); setError(null);
    try {
      const result = await applySchedule(workspaceId, preview.scheduleVersion, keyFor(preview.scheduleVersion));
      setApplied(result);
      // 应用之后**重新拉整份计划**,而不是把结果就地拼进界面:写进去的是真实行,
      // 而"我的计划是什么"这个问题只有 `/plan` 答得出来。
      await refreshPlan();
    } catch (cause) {
      // 409 的两种含义要做的事完全不同:版本失效要**重新预览**(用户看的那份已经
      // 旧了),写入失败要重试。后端给的中文 message 本来就是写给用户看的。
      setError(cause instanceof ApiError ? cause.message : '应用排期失败。');
    } finally {
      setBusy(false);
    }
  }, [preview, refreshPlan, workspaceId]);

  if (!isRealSpace) {
    return <div className={styles.view}>
      <div className={styles.empty} data-testid="schedule-empty">
        <CalendarRange size={22}/>
        <h2>还没有打开任何一个成长空间</h2>
        <p>排期要写进后端，所以得先有一个空间。在「全部空间」里新建一个，把目标拆成带预计工时的任务，就能在这里排出周计划和日计划。</p>
      </div>
    </div>;
  }

  return <div className={styles.view} data-testid="schedule-view">
    <div className={styles.head}>
      <div>
        <span className={styles.eyebrow}>MAKE A PLAN THAT FITS</span>
        <h2>这周做多少，哪天做。</h2>
        <p className={styles.note}>
          时间池按<strong>你这个人</strong>算，不按空间算——同一晚只能做一件事。
          所以一次排期会同时安排你全部活动空间里的任务。
        </p>
      </div>
      <div className={styles.headActions}>
        <button className={styles.primary} onClick={runPreview} disabled={busy} data-testid="schedule-preview">
          {busy ? <Loader2 size={14} className={styles.spin}/> : <Sparkles size={14}/>}
          {preview ? '重新预览' : '预览排期'}
        </button>
      </div>
    </div>

    {planLoading && <p className={styles.note}>正在读取计划…</p>}
    {(error ?? planError) && <p className={styles.error} role="alert">{error ?? planError}</p>}

    {/* ---------------------------------------------------------------- 预览 */}
    {preview && <section className={styles.preview} data-testid="schedule-preview-result">
      <header className={styles.previewHead}>
        <div>
          <h3>这份排期会这样安排</h3>
          <p className={styles.note}>
            覆盖 {preview.scopeWorkspaceIds.length} 个活动空间 ·
            每周预算 {Math.round(preview.weeklyBudgetMinutes / 60 * 10) / 10} 小时 ·
            往后算 {preview.horizonDays} 天
          </p>
        </div>
        {!applied && <button className={styles.apply} onClick={runApply} disabled={busy} data-testid="schedule-apply">
          {busy ? <Loader2 size={14} className={styles.spin}/> : <CalendarCheck size={14}/>}应用这份排期
        </button>}
      </header>

      <p className={styles.churn} data-testid="schedule-churn">{preview.churn.description}</p>
      <p className={styles.note}>
        共 {preview.sessions.length} 场，合计 {Math.round(preview.totalPlannedMinutes / 60 * 10) / 10} 小时。
        {preview.unscheduledMinutes > 0 && <>还有 <strong>{Math.round(preview.unscheduledMinutes / 60 * 10) / 10} 小时</strong>排不进去，原因见下。</>}
      </p>

      {/* 排不进去的部分。**不静默截断** —— 排不下的会一直显示在这里,直到用户
          真的做了点什么(减量、延期、加时间),而不是安静地消失。 */}
      {preview.gaps.length > 0 && <div className={styles.gaps} data-testid="schedule-gaps">
        <h4><AlertTriangle size={13}/>这些没能排进去</h4>
        {preview.gaps.map((gap, index) => <div className={styles.gap} key={`${gap.nodeId}-${gap.reasonCode}-${index}`}>
          <strong>{gap.nodeTitle || '（整个计划）'}</strong>
          <span>{gapReasonLabel(gap.reasonCode)}，还差 {Math.round(gap.unscheduledMinutes / 60 * 10) / 10} 小时</span>
          <em>卡在：{bindingConstraintLabel(gap.bindingConstraint)}</em>
        </div>)}
      </div>}

      {/* 三条出路。`resolvesGap` 是后端**重跑一遍算出来的** —— 标着"能补上"的
          选项是真的能补上,不是算法随口说的。 */}
      {preview.options.length > 0 && <div className={styles.options} data-testid="schedule-options">
        <h4>可以这样解决</h4>
        {preview.options.map(option => <div className={`${styles.option} ${option.resolvesGap ? styles.optionOk : ''}`} key={option.kind}>
          <div>
            <strong>{option.label}{option.resolvesGap && <span className={styles.badge}><Check size={10}/>能补上</span>}</strong>
            <span>{option.description}</span>
          </div>
          {!option.resolvesGap && option.remainingUnscheduledMinutes > 0
            && <em>试算之后还差 {Math.round(option.remainingUnscheduledMinutes / 60 * 10) / 10} 小时</em>}
        </div>)}
        <p className={styles.note}>这些是算法试算出来的方向，还没有改动你的计划。改完目标或时间预算之后，回到这里重新预览一次。</p>
      </div>}

      {/* 每天排了多少、本来有多少。**两个数字必须一起出现** —— 只给"排了 4 小时"
          的话,用户没法判断这是多是少,而他能做的三件事都取决于"是不是满了"。 */}
      {preview.dailyLoad.length > 0 && <div className={styles.load} data-testid="schedule-daily-load">
        <h4>每天的时间占用</h4>
        <div className={styles.loadList}>
          {(showAllDays ? preview.dailyLoad : preview.dailyLoad.slice(0, 14)).map(day => {
            const ratio = day.capacityMinutes > 0 ? Math.min(1, day.plannedMinutes / day.capacityMinutes) : 0;
            const over = day.plannedMinutes > day.capacityMinutes;
            return <div className={styles.loadDay} key={day.date} data-testid="daily-load-row">
              <span className={styles.loadDate}>{day.date.slice(5).replace('-', '/')}</span>
              <div className={styles.loadTrack}><span style={{ width: `${ratio * 100}%` }} className={over ? styles.loadOver : ''}/></div>
              <span className={styles.loadMinutes}>{day.plannedMinutes} / {day.capacityMinutes} 分</span>
            </div>;
          })}
        </div>
        {preview.dailyLoad.length > 14 && <button className={styles.link} onClick={() => setShowAllDays(!showAllDays)}>
          {showAllDays ? '只看到前两周' : `展开其余 ${preview.dailyLoad.length - 14} 天`}
        </button>}
      </div>}
    </section>}

    {/* ------------------------------------------------------------- 应用结果 */}
    {applied && <section className={styles.applied} data-testid="schedule-applied" role="status">
      <strong><Check size={13}/>{applied.replayed ? '这份排期之前已经应用过了' : '已经排进你的计划'}</strong>
      <span>
        新增 {applied.applied.created} 场 · 更新 {applied.applied.updated} 场 ·
        取消 {applied.applied.canceled} 场 · 涉及 {applied.applied.workspaces} 个空间
      </span>
      {applied.applied.unscheduledMinutes > 0 && <span>还有 {Math.round(applied.applied.unscheduledMinutes / 60 * 10) / 10} 小时排不进去。</span>}
      <span className={styles.note}>已经完成的场次原样不动——这一步只安排还没发生的事。</span>
    </section>}

    {/* --------------------------------------------------------- 现有的安排 */}
    <section className={styles.existing}>
      <div className={styles.existingHead}>
        <h3><Clock3 size={14}/>已经排好的安排</h3>
        <button className={styles.link} onClick={() => void refreshPlan()}><RefreshCw size={12}/>刷新</button>
      </div>

      {sessions.length === 0
        ? <p className={styles.empty2} data-testid="schedule-no-sessions">
            这个空间还没有排过任何安排。点上面的「预览排期」，看过之后再决定要不要应用——
            预览不会改动任何东西。
          </p>
        : <>
          <h4 className={styles.dayHead}>今天 · {today}</h4>
          {todaySessions.length === 0
            ? <p className={styles.empty2}>今天没有安排。这不代表你没做事——只是这一天没有排进去的东西。</p>
            : <ul className={styles.sessionList} data-testid="schedule-today">
                {todaySessions.map(session => <li key={session.id}>
                  <span className={styles.sessionTitle}>{session.nodeTitle}</span>
                  <span className={styles.sessionMeta}>
                    {session.plannedMinutes} 分钟{session.bufferMinutes > 0 && `（含 ${session.bufferMinutes} 分钟缓冲）`}
                    {session.startMinute !== null && ` · ${String(Math.floor(session.startMinute / 60)).padStart(2, '0')}:${String(session.startMinute % 60).padStart(2, '0')}`}
                    {session.locked && ' · 已锁定'}
                    {session.status === 'done' && ' · 已完成'}
                  </span>
                </li>)}
              </ul>}

          <h4 className={styles.dayHead}>本周 · {week.start} 至 {week.end}</h4>
          {weekDays.length === 0
            ? <p className={styles.empty2}>本周没有安排。</p>
            : <div className={styles.week} data-testid="schedule-week">
                {weekDays.map(([day, list]) => <div className={styles.weekDay} key={day}>
                  <span className={styles.weekDate}>{day.slice(5).replace('-', ' / ')}<em>{list.reduce((sum, session) => sum + session.plannedMinutes, 0)} 分</em></span>
                  <ul className={styles.sessionList}>
                    {list.map(session => <li key={session.id}>
                      <span className={styles.sessionTitle}>{session.nodeTitle}</span>
                      <span className={styles.sessionMeta}>{session.plannedMinutes} 分钟 · 第 {session.seq + 1} 次{session.status === 'done' && ' · 已完成'}</span>
                    </li>)}
                  </ul>
                </div>)}
              </div>}
          {laterCount > 0 && <p className={styles.note}>本周之后还有 {laterCount} 场安排，时间线上看得到。</p>}
        </>}
    </section>
  </div>;
}
