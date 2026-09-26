'use client';

import { todayInTimeZone } from '@/features/growth/timeline';
import { RealToday } from './RealToday';

/**
 * 页头那句日期以前是写死的 `WEDNESDAY, SEPTEMBER 16` —— 它印在**每一天**。
 * 一个每天都说"今天是 9 月 16 日星期三"的页面,比不显示日期更糟:用户会先怀疑
 * 自己看错了,再怀疑这个产品不知道今天几号。现在按浏览器时区取真实日期。
 */
const WEEKDAYS = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];
const WEEKDAYS_EN = ['SUNDAY', 'MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY'];
const MONTHS_EN = ['JANUARY', 'FEBRUARY', 'MARCH', 'APRIL', 'MAY', 'JUNE', 'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER', 'NOVEMBER', 'DECEMBER'];

function Header({ greeting, dateLabel, dateLabelEn }: { greeting: string; dateLabel: string; dateLabelEn: string }) {
  return (
    <header className="editorial-header">
      <span className="eyebrow">{dateLabelEn}</span>
      <span className="page-date">{dateLabel}</span>
      <h1>{greeting}，<br />继续向理想的自己前进。</h1>
      <p>不必一次走很远。今天，把这一件事做好。</p>
    </header>
  );
}

export function Today() {
  // 现在几点 —— 问候语跟着走。"晚上好"出现在早上八点同样是在说一件不真实的事。
  const now = new Date();
  const hour = now.getHours();
  const greeting = hour < 6 ? '夜深了' : hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好';
  // 中文那句从 `todayInTimeZone` 切出来(它是本地日期,和 `now.getDay()` 同一时区);
  // 英文那句直接用 `now`,两者说的是同一天。
  const dateParts = todayInTimeZone(now).split('-').map(Number);
  const dateLabel = `${dateParts[1]} 月 ${dateParts[2]} 日 · ${WEEKDAYS[now.getDay()]}`;
  const dateLabelEn = `${WEEKDAYS_EN[now.getDay()]}, ${MONTHS_EN[now.getMonth()]} ${now.getDate()}`;

  return (
    <div className="editorial-page today-page">
      <Header greeting={greeting} dateLabel={dateLabel} dateLabelEn={dateLabelEn} />
      {/* 这里原来还有一份 `DemoToday` —— 一份**完全不看后端**的"今天":它按本地
          计划的 `scheduledDate` 挑任务,旁边那段"来自知途的观察"是写死的文案
          ("你今天课程安排比较满""我把科研任务降低到了一个")。知途从来没有拿到过
          用户的课表,也没替谁做过那个决定,所以对任何一个真实空间它都是凭空捏造。
          现在只有 `RealToday`:它只说它真的从后端读到的事。 */}
      <RealToday />
    </div>
  );
}
