'use client';

import { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowUpRight, Check, Clock3, Sparkles } from 'lucide-react';
import type { ExecutionResult, TodayItemView } from '@/lib/backend';
import { Reminders } from './Reminders';
import { TodayPlanTabs } from './TodayPlanTabs';
import { useToday } from './useToday';
import { WeekOverview } from './WeekOverview';

/**
 * 真实空间的「今天」。
 *
 * 和示例空间那一版最根本的区别:**这里的每一次勾选都会真的写到服务端**。
 * 示例空间整份状态都在浏览器里,勾一下就是改内存;真实空间如果也这么干,界面
 * 会说"完成了"而库里什么都没有 —— 刷新就回来,而"根据执行情况持续调整"这条
 * 闭环的起点正是"用户报告了实际发生了什么"。
 *
 * ## 界面上必须守住的一件事:没记录 ≠ 没完成
 *
 * 一场过去了的安排如果没有执行记录,我们**不知道**发生了什么 —— 可能做了没说,
 * 可能没做,可能做了一半。所以这里不把它画成"未完成",而是把它变成一个**问题**
 * (`checkInQuestions` 是服务端拼好的那句问话)。把它显示成"没完成",就是在替
 * 用户断言一件我们并不知道的事,而这个断言会一路影响后面的偏差判定和重规划。
 *
 * ## 三个卡片已删除
 *
 * 原来的「当前阶段 / 下一里程碑 / 本周重点」三张卡(以及那条黄色"尚未生成工作块"
 * 横幅)没有了 —— 它们和这里的执行清单说的是同一件事。取而代之的是 `TodayPlanTabs`
 * 的两个页签(本周计划 / 本日计划),数据来自跨空间的 `GET /api/today/plans`。
 * 下面的执行清单不删:它是**记录实际发生了什么**的那一环(做了一部分 / 跳过 / 失败),
 * 与"计划是什么"是两件事。
 */

const RESULT_LABEL: Record<ExecutionResult, string> = {
  completed: '完成了',
  partial: '做了一部分',
  skipped: '这次跳过',
  failed: '做了但没成',
};

/** `startMinute` 是"当天第几分钟",不是"几点" —— 换算要过一遍,而且它可能不存在。 */
function clockLabel(startMinute: number | null, endMinute: number | null): string | null {
  if (startMinute === null || endMinute === null) return null;
  const at = (minute: number) => `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
  return `${at(startMinute)} – ${at(endMinute)}`;
}

type RecordFn = (sessionId: string, result: ExecutionResult, extra?: { actualMinutes?: number; delayReason?: string }) => Promise<boolean>;

/**
 * 今天的一场安排。自己管自己那份"展开记录表单"的状态,父组件不必按 sessionId 存一堆布尔。
 *
 * `justCompleted` 由父组件给,不由这里自己算 —— 理由见 `RealToday` 里那段说明:
 * 完成的那一刻这一行会换一个父容器,DOM 是重挂的,组件自己的 state 活不过去。
 */
function TodayItem({ item, saving, onRecord, justCompleted, onSettled }: {
  item: TodayItemView;
  saving: boolean;
  onRecord: RecordFn;
  justCompleted: boolean;
  onSettled: () => void;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [minutes, setMinutes] = useState('');
  const [note, setNote] = useState('');
  const clock = clockLabel(item.startMinute, item.endMinute);

  async function submit(result: ExecutionResult) {
    const parsed = Number(minutes);
    const ok = await onRecord(item.sessionId, result, {
      actualMinutes: minutes && Number.isFinite(parsed) && parsed > 0 ? Math.round(parsed) : undefined,
      delayReason: note.trim() || undefined,
    });
    if (ok) { setOpen(false); setMinutes(''); setNote(''); }
  }

  return (
    <div
      className={`today-item${item.recorded ? ' recorded' : ''}${justCompleted ? ' is-just-completed' : ''}`}
      /*
       * 收缩回弹 + 落进完成色这两段跑完就把标记摘掉。
       *
       * **不看"是不是 recorded"**,只看这一次写入。已经记过的那些在打开页面时
       * 不会重播任何东西 —— 它们没有任何"刚刚发生"可以表现。
       *
       * 按**动画名**判断,而不是"这个元素里有什么动画结束了"。`animationend` 会从
       * 子元素冒泡上来,而这一行里有两条动画:勾选框先跑(100ms),文字与结果那一段
       * 晚 100ms 才开始(`today-settle` 的 `animation-delay`)。不写名字的话,勾选框
       * 那一条结束时就把标记摘了 —— 文字的完成色**一次都不会落下**(它的延迟还没走完,
       * 动画就被取消了)。PathView 里 `node-create` 那处是同一个理由。
       */
      onAnimationEnd={justCompleted ? (event) => {
        if (event.animationName === 'today-settle') onSettled();
      } : undefined}
    >
      <div className="today-item-main">
        <button
          className="task-check"
          aria-label={`把「${item.nodeTitle}」标记为完成`}
          aria-pressed={item.result === 'completed'}
          disabled={saving}
          onClick={() => void onRecord(item.sessionId, 'completed')}
        >
          {item.result === 'completed' && <Check size={13} />}
        </button>
        <button className="today-item-title" onClick={() => router.push(`/workbench?workspace=${encodeURIComponent(item.workspaceId)}&view=schedule`)}>
          {item.nodeTitle}
          <small>{item.workspaceTitle}{item.seq > 1 && ` · 第 ${item.seq} 次`}</small>
        </button>
        <span className="today-item-meta">
          {clock ?? `${item.plannedMinutes} min`}
        </span>
      </div>

      {/* 记录过就如实显示记录,不显示"已完成" —— 跳过和没做成也是记录,它们
          都不是"完成了",而界面上把它们归成一句话就等于把用户说的话抹掉。 */}
      {item.recorded && (
        <p className="today-item-result">
          <Check size={12} />{RESULT_LABEL[item.result as ExecutionResult]}
          {item.actualMinutes ? `，用了 ${item.actualMinutes} 分钟` : ''}
          {item.delayReason ? ` · ${item.delayReason}` : ''}
        </p>
      )}

      <div className="today-item-actions">
        <button className="text-button" aria-expanded={open} onClick={() => setOpen(!open)} disabled={saving}>
          {open ? '收起' : '记一笔'}
        </button>
        {saving && <span className="today-item-saving">正在记录…</span>}
      </div>

      {open && (
        <div className="today-item-form">
          <label>
            实际用了多少分钟
            <input type="number" min={1} inputMode="numeric" value={minutes} onChange={e => setMinutes(e.target.value)} placeholder={String(item.plannedMinutes)} />
          </label>
          <label>
            想说的话（没做完的原因、卡在哪里）
            <textarea value={note} onChange={e => setNote(e.target.value)} rows={2} placeholder="可选。说得出原因，后面的调整才可能有用。" />
          </label>
          <div className="today-item-form-actions">
            <button disabled={saving} onClick={() => void submit('partial')}>做了一部分</button>
            <button disabled={saving} onClick={() => void submit('skipped')}>这次跳过</button>
            <button disabled={saving} onClick={() => void submit('failed')}>做了但没成</button>
            <button className="primary-button" disabled={saving} onClick={() => void submit('completed')}>完成了</button>
          </div>
        </div>
      )}
    </div>
  );
}

export function RealToday() {
  const { data, error, loading, savingSession, refresh, record, clearError } = useToday(true);
  const [answering, setAnswering] = useState<string | null>(null);

  /*
   * 哪一场刚刚**真的**写进去了。
   *
   * ## 为什么这份标记在父组件而不是在 `TodayItem` 里
   *
   * 完成的那一刻,这一行会从「今天最重要的事」(`.focus-card`)挪进「接下来」
   * (`rest`)—— 换父容器就是换 DOM 节点,React 会把它卸载再挂载一次,组件自己的
   * state 活不过去。标记放在这一层,重新挂载的那一个也能拿到它。
   *
   * ## 为什么不能直接看 `item.result === 'completed'`
   *
   * 那个条件对**所有**已完成的场次都成立,包括上一次打开页面时就记过的那些 ——
   * 拿它做动画条件,等于每次进首页都把历史记录当成刚刚发生的事重播一遍。
   *
   * ## 为什么它一定对应一次真实写入
   *
   * 唯一的赋值处是下面那个 `recordWithFeedback`,而且必须 `ok` 为真。`ok` 来自
   * `useToday.record`:它只在响应 `saved` 为真、并成功重新拉取数据之后才返回 `true`。
   * 写失败时这里什么都不会变,那一行也就不会有任何"完成"的样子,错误行照常在
   * 上面显示。
   */
  const [justCompleted, setJustCompleted] = useState<string | null>(null);
  const recordWithFeedback = useCallback<RecordFn>(async (sessionId, result, extra) => {
    const ok = await record(sessionId, result, extra);
    if (ok && result === 'completed') setJustCompleted(sessionId);
    return ok;
  }, [record]);
  const settle = useCallback((sessionId: string) => {
    setJustCompleted(current => (current === sessionId ? null : current));
  }, []);

  const items = data?.workspaces.flatMap(workspace => workspace.items) ?? [];
  // 「最重要的事」= 还没记录的第一件。已经记过的那些不再是"待办",把它们留在
  // 最显眼的位置会挤掉真正还没交代的那一件。
  const focusItem = items.find(item => !item.recorded) ?? null;
  const rest = items.filter(item => item.sessionId !== focusItem?.sessionId);
  const questions = data?.checkInQuestions ?? [];

  return (
    <>
      <WeekOverview revision={data} />
      {/* 两个页签是新的计划区。写入成功 → 刷新聚合,并让上面的时间线与下面的执行清单
          一起重取(`refresh` 更新 `data`,而 `data` 正是它们的 revision)。 */}
      <TodayPlanTabs onChanged={() => { void refresh(); }} />
      <Reminders />

      {error && (
        <div className="turn-error" role="alert">
          <span>{error}</span>
          <button onClick={() => { clearError(); void refresh(); }}>重试</button>
        </div>
      )}

      <div className="today-columns">
        <section>
          <div className="section-label"><span>今天最重要的事</span><span>01 / FOCUS</span></div>

          {loading && !data && <div className="empty-note">正在读取今天的安排…</div>}

          {data && items.length === 0 && (
            <div className="empty-note">
              今天没有排上具体的事。
              <br />
              {data.note}
            </div>
          )}

          {focusItem && (
            <div className="focus-card">
              <div className="focus-category"><span className="tiny-dot" />{focusItem.workspaceTitle}</div>
              <TodayItem
                item={focusItem}
                saving={savingSession === focusItem.sessionId}
                onRecord={recordWithFeedback}
                justCompleted={justCompleted === focusItem.sessionId}
                onSettled={() => settle(focusItem.sessionId)}
              />
            </div>
          )}

          {data && items.length > 0 && !focusItem && (
            <div className="empty-note">今天的安排都记过了。剩下的交给明天。</div>
          )}

          {rest.length > 0 && (
            <section className="up-next">
              <div className="section-label"><span>接下来</span><span>按自己的节奏</span></div>
              {rest.map(item => (
                <TodayItem
                  key={item.sessionId}
                  item={item}
                  saving={savingSession === item.sessionId}
                  onRecord={recordWithFeedback}
                  justCompleted={justCompleted === item.sessionId}
                  onSettled={() => settle(item.sessionId)}
                />
              ))}
            </section>
          )}

          {/* **提问,不是结论。** 这些场次过去了好几天而没有任何记录,我们不知道
              发生了什么 —— 所以只能问。这里说的每一句话都由服务端拼好。 */}
          {questions.length > 0 && (
            <section className="up-next">
              <div className="section-label"><span>这几天还没有记录</span><span>{questions.length} 件</span></div>
              {questions.map(question => (
                <div className="today-item" key={question.sessionId}>
                  <div className="today-item-main">
                    <Clock3 size={14} />
                    <span className="today-item-title">{question.question}</span>
                  </div>
                  <div className="today-item-actions">
                    <button className="text-button" disabled={answering === question.sessionId} onClick={async () => {
                      setAnswering(question.sessionId);
                      await record(question.sessionId, 'completed');
                      setAnswering(null);
                    }}>那天做了</button>
                    <button className="text-button" disabled={answering === question.sessionId} onClick={async () => {
                      setAnswering(question.sessionId);
                      await record(question.sessionId, 'skipped');
                      setAnswering(null);
                    }}>那天没做</button>
                  </div>
                </div>
              ))}
            </section>
          )}
        </section>

        <aside className="today-aside">
          <Sparkles size={23} />
          <span className="eyebrow">来自知途的观察</span>
          {/* 这里只说**真的知道的事**:今天有几项、来自哪个空间、报了多少分钟。
              以前这一段写着"你今天课程安排比较满""我把科研任务降低到了一个" ——
              知途从来没有拿到过用户的课表,也没有替谁做过这个决定。 */}
          {data ? (
            <>
              <h2>{items.length ? `今天有 ${items.length} 件事，合计约 ${data.plannedMinutes} 分钟。` : '今天还没有排上具体的事。'}</h2>
              <p>{data.note}</p>
              <p>
                已记下 {data.recordedCount} 件
                {data.recordedCount > 0 && `，实际投入 ${data.actualMinutes} 分钟`}。
                {items.length > data.recordedCount && ' 没记的那些，我不会替你猜。'}
              </p>
              <p>
                想改安排，就去工作台的「排期」看——它会按你的截止时间、每场时长和每周可投入时间重算一遍，
                你看过再确认。
              </p>
              <button className="text-button" onClick={() => void refresh()}>刷新今天的安排<ArrowUpRight size={13} /></button>
            </>
          ) : (
            <h2>正在读取今天的安排…</h2>
          )}
          <div className="quiet-note">成长不只发生在<br />完成任务的那一刻。</div>
        </aside>
      </div>
    </>
  );
}
