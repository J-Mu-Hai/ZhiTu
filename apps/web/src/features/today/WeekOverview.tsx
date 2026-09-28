'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react';
import * as backend from '@/lib/backend';
import { todayInTimeZone } from '@/features/growth/timeline';

type Entry = backend.ScheduledSessionPayload & { workspaceTitle: string };
function shift(date: string, days: number) {
  const value = new Date(`${date}T12:00:00`);
  value.setDate(value.getDate() + days);
  return todayInTimeZone(value);
}

/** Same persisted sessions as ScheduleView. No invented clock times or task statuses. */
export function WeekOverview({ revision }: { revision: unknown }) {
  const [today] = useState(() => todayInTimeZone(new Date()));
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(today);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    void backend.listWorkspaces().then(async spaces => Promise.all(spaces.map(async space => {
      const plan = await backend.getPlan(space.id);
      return plan.sessions.map(session => ({ ...session, workspaceTitle: space.title }));
    }))).then(groups => { if (active) { setEntries(groups.flat()); setError(false); } })
      .catch(() => { if (active) setError(true); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [revision, retry]);
  const weekday = new Date(`${today}T12:00:00`).getDay();
  const monday = shift(today, -((weekday + 6) % 7) + offset * 7);
  const days = Array.from({ length: 7 }, (_, index) => shift(monday, index));
  const dayEntries = entries.filter(entry => entry.scheduledDate === selected).sort((a, b) => (a.startMinute ?? 1440) - (b.startMinute ?? 1440));
  function move(delta: number) { setOffset(offset + delta); setSelected(shift(monday, delta * 7)); }
  return <section className="week-overview" aria-label="本周时间线">
    <header><h2><CalendarDays size={20}/>本周时间线 <small>{days[0].slice(5)} — {days[6].slice(5)}</small></h2><div><button aria-label="上一周" onClick={() => move(-1)}><ChevronLeft size={17}/></button><button aria-label="下一周" onClick={() => move(1)}><ChevronRight size={17}/></button><button onClick={() => { setOffset(0); setSelected(today); }}>本周</button></div></header>
    {error ? <p role="alert">本周安排未能完整读取。<button onClick={() => setRetry(retry + 1)}>重试</button></p> : <>
      <div className="week-day-strip">{days.map((date, index) => <button key={date} aria-pressed={selected === date} className={selected === date ? 'selected' : ''} onClick={() => setSelected(date)}><span>{date.slice(5).replace('-', '.')}</span><small>周{'一二三四五六日'[index]}{date === today ? ' · 今天' : ''}</small><i/><span>{loading ? '读取中' : `${entries.filter(entry => entry.scheduledDate === date).length} 场安排`}</span></button>)}</div>
      <div className="week-selected-list"><h3>{selected === today ? '今日' : selected.slice(5)}安排 <small>点击前往对应空间排期</small></h3>{loading ? <p>正在读取…</p> : dayEntries.length ? dayEntries.map(entry => <Link key={entry.id} href={`/workbench?workspace=${encodeURIComponent(entry.workspaceId)}&view=schedule`}><span>{entry.nodeTitle}<small>{entry.workspaceTitle}</small></span><span>{entry.status === 'done' ? '已完成' : entry.status === 'skipped' ? '已跳过' : `${entry.plannedMinutes} 分钟`}<ChevronRight size={14}/></span></Link>) : <p>这一天还没有排期，留一点空间给自己。</p>}</div>
    </>}
  </section>;
}
