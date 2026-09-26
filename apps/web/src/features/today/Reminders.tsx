'use client';

import { BellRing, Clock } from 'lucide-react';
import { useReminders } from './useReminders';

/**
 * 提醒条。真实空间才挂 —— 示例空间整份数据都在浏览器里,给它接一条服务端提醒
 * 只会让两个空间的边界变得说不清楚。
 *
 * 三种状态都必须能被区分,这是这个组件存在的主要理由:
 *
 * 1. **有提醒** —— 逐条显示,可以「知道了」或「过会儿再说」。
 * 2. **没有提醒** —— 什么都不说。不要写"暂无提醒",那是一个空的仪表盘。
 * 3. **没有提醒,但有几条被免打扰压住了** —— **必须说出来**。它和状态 2 长得一样,
 *    含义却相反:前者是"没事",后者是"有事,但现在不吵你"。不说的话,用户会把
 *    后者读成前者。
 */
export function Reminders() {
  const { reminders, quietHours, suppressedCount, error, busyKey, dismiss, snooze } = useReminders(true);

  if (error) {
    return <div className="reminder-strip error" role="alert">{error}</div>;
  }

  if (reminders.length === 0) {
    // 被压住的条数如实报出来,否则"现在没有提醒"是一句会骗人的话。
    if (suppressedCount > 0 && quietHours) {
      return (
        <div className="reminder-strip muted">
          <Clock size={14} />
          <span>
            有 {suppressedCount} 条提醒被免打扰时段挡住了（{quietHours.description}），它不会在这段时间出现。
          </span>
        </div>
      );
    }
    return null;
  }

  return (
    <div className="reminder-strip">
      <BellRing size={14} />
      <div className="reminder-list">
        {reminders.map(reminder => (
          <div className="reminder" key={reminder.key}>
            <strong>{reminder.title}</strong>
            <p>{reminder.body}</p>
            <div className="reminder-actions">
              {/* 「过会儿再说」是一个真实的承诺:到点它会重新出现,不是被软化成关掉。 */}
              <button disabled={busyKey === reminder.key} onClick={() => void snooze(reminder.key)}>过会儿再说</button>
              <button disabled={busyKey === reminder.key} onClick={() => void dismiss(reminder.key)}>知道了</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
