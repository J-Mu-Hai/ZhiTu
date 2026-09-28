'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowUpRight, Check, Clock3, Sparkles } from 'lucide-react';
import type { ExecutionResult, TodayItemView } from '@/lib/backend';
import { Reminders } from './Reminders';
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

/** 今天的一场安排。自己管自己那份"展开记录表单"的状态,父组件不必按 sessionId 存一堆布尔。 */
function TodayItem({ item, saving, onRecord }: { item: TodayItemView; saving: boolean; onRecord: RecordFn }) {
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
    <div className={`today-item${item.recorded ? ' recorded' : ''}`}>
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

  const items = data?.workspaces.flatMap(workspace => workspace.items) ?? [];
  // 「最重要的事」= 还没记录的第一件。已经记过的那些不再是"待办",把它们留在
  // 最显眼的位置会挤掉真正还没交代的那一件。
  const focusItem = items.find(item => !item.recorded) ?? null;
  const rest = items.filter(item => item.sessionId !== focusItem?.sessionId);
  const questions = data?.checkInQuestions ?? [];

  return (
    <>
      <WeekOverview revision={data} />
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
              <TodayItem item={focusItem} saving={savingSession === focusItem.sessionId} onRecord={record} />
            </div>
          )}

          {data && items.length > 0 && !focusItem && (
            <div className="empty-note">今天的安排都记过了。剩下的交给明天。</div>
          )}

          {rest.length > 0 && (
            <section className="up-next">
              <div className="section-label"><span>接下来</span><span>按自己的节奏</span></div>
              {rest.map(item => (
                <TodayItem key={item.sessionId} item={item} saving={savingSession === item.sessionId} onRecord={record} />
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
