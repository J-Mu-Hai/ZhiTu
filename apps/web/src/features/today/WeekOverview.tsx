'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react';
import * as backend from '@/lib/backend';
import { todayInTimeZone } from '@/features/growth/timeline';

type Entry = backend.AgendaWeekSessionView;
function shift(date: string, days: number) {
  const value = new Date(`${date}T12:00:00`);
  value.setDate(value.getDate() + days);
  return todayInTimeZone(value);
}

/**
 * 顶部「本周时间线」。**数据来自和两个页签同一份跨空间聚合**。
 *
 * 以前这里会 `listWorkspaces()` 之后逐空间 `getPlan()` —— 请求数随空间数增长,
 * 而首页控制不住它。现在只发一次 `GET /api/today/plans?weekStart=...`,翻周也只是一次
 * 请求,不是每翻一周把每个空间都拉一遍。
 *
 * 画出来的仍然是**已经写进库里的场次**:没有排期的日子诚实说"还没有排期",不编时间。
 */
export function WeekOverview({ revision }: { revision: unknown }) {
  const [today] = useState(() => todayInTimeZone(new Date()));
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(today);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const weekday = new Date(`${today}T12:00:00`).getDay();
  const monday = shift(today, -((weekday + 6) % 7) + offset * 7);
  const days = Array.from({ length: 7 }, (_, index) => shift(monday, index));
  useEffect(() => {
    let active = true;
    setLoading(true);
    void backend.fetchTodayPlans(monday)
      .then(data => { if (active) { setEntries(data.weekSessions); setError(false); } })
      .catch(() => { if (active) setError(true); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [monday, revision, retry]);
  const dayEntries = entries.filter(entry => entry.scheduledDate === selected).sort((a, b) => (a.startMinute ?? 1440) - (b.startMinute ?? 1440));
  function move(delta: number) { setOffset(offset + delta); setSelected(shift(monday, delta * 7)); }
  return <section className="week-overview" aria-label="本周时间线">
    <header><h2><CalendarDays size={20}/>本周时间线 <small>{days[0].slice(5)} — {days[6].slice(5)}</small></h2><div><button aria-label="上一周" onClick={() => move(-1)}><ChevronLeft size={17}/></button><button aria-label="下一周" onClick={() => move(1)}><ChevronRight size={17}/></button><button onClick={() => { setOffset(0); setSelected(today); }}>本周</button></div></header>
    {error ? <p role="alert">本周安排未能完整读取。<button onClick={() => setRetry(retry + 1)}>重试</button></p> : <>
      <div className="week-day-strip">{days.map((date, index) => <button key={date} aria-pressed={selected === date} className={selected === date ? 'selected' : ''} onClick={() => setSelected(date)}><span>{date.slice(5).replace('-', '.')}</span><small>周{'一二三四五六日'[index]}{date === today ? ' · 今天' : ''}</small><i/><span>{loading ? '读取中' : `${entries.filter(entry => entry.scheduledDate === date).length} 场安排`}</span></button>)}</div>
      {/* `key={selected}`:换一天就重挂一次,于是那一段淡入重放。
          用 key 而不是在内容上做过渡 —— 内容变了但元素没换,CSS 动画不会重放。
          焦点不在这个容器里(点的是上面那条日期带),所以重挂不会把焦点弄丢。 */}
      <div className="week-selected-list" key={selected}><h3>{selected === today ? '今日' : selected.slice(5)}安排 <small>点击前往对应空间排期</small></h3>{loading ? <p>正在读取…</p> : dayEntries.length ? dayEntries.map(entry => <Link key={entry.id} href={`/workbench?workspace=${encodeURIComponent(entry.workspaceId)}&view=schedule`}><span>{entry.nodeTitle}<small>{entry.workspaceTitle}</small></span><span>{entry.status === 'done' ? '已完成' : entry.status === 'skipped' ? '已跳过' : `${entry.plannedMinutes} 分钟`}<ChevronRight size={14}/></span></Link>) : <p>这一天还没有排期，留一点空间给自己。</p>}</div>
    </>}
  </section>;
}
