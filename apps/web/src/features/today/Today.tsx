'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowUpRight, Check, Clock3, Pause, Play, Sparkles } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { todayInTimeZone } from '@/features/growth/timeline';
import { categories } from '@/mock/growth-state';
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

/**
 * 示例空间的「今天」。
 *
 * 整份状态都在浏览器里,所以勾选只改本地 reducer —— 这对示例空间是**成立的**,
 * 它本来就没有服务端那一份。真实空间绝不能走这条路:那样界面会说"完成了"而
 * 库里什么都没有,而"根据执行情况持续调整"的起点就是这条记录。两者按
 * `isRealSpace` 分开,不是风格选择,是数据在哪里的问题。
 */
function DemoToday() {
  const { growth, apply, enterSpace, focus, setFocus } = useDemo();
  const router = useRouter();
  const [why, setWhy] = useState(false);

  const now = new Date();
  const today = Object.values(growth.nodes).filter(node => node.type === 'task' && node.scheduledDate === todayInTimeZone(now));
  const main = today.find(node => node.id === 'attention') ?? today[0];
  const remaining = today.filter(node => node.id !== main?.id);
  const minutes = Math.floor(focus.seconds / 60).toString().padStart(2, '0');
  const seconds = (focus.seconds % 60).toString().padStart(2, '0');

  return (
    <div className="today-columns">
      <section>
        <div className="section-label"><span>今天最重要的事</span><span>01 / FOCUS</span></div>
        {main
          ? <div className="focus-card">
              <div className="focus-category"><span className="tiny-dot" />{categories.find(c => c.id === main.category)?.title ?? '今天的事'}</div>
              <button className="focus-title" onClick={() => { enterSpace(main.id); router.push('/workbench'); }}>{main.title}<ArrowUpRight size={20} /></button>
              <p>{main.description}</p>
              <div className="focus-meta">
                <span><Clock3 size={14} />预计 {Math.round((main.estimatedHours ?? 1.5) * 60)} min</span>
                <span>一次只做一件事</span>
              </div>
              <div className="focus-bottom">
                {main.status === 'completed'
                  ? <span className="completed-label"><Check size={17} />今天的这一步，完成了。</span>
                  : <>
                      <button className="primary-button" onClick={() => setFocus(f => ({ ...f, nodeId: main.id, running: !f.running }))}>
                        {focus.running ? <Pause size={14} /> : <Play size={14} />} {focus.running ? '暂停专注' : focus.seconds ? '继续专注' : '开始专注'}
                      </button>
                      {focus.seconds > 0 && <>
                        <time className="focus-timer">{minutes}:{seconds}</time>
                        <button className="text-button" onClick={() => { setFocus(f => ({ ...f, running: false })); apply({ type: 'UPDATE_STATUS', nodeId: main.id, status: 'completed' }); }}>完成学习<Check size={13} /></button>
                      </>}
                    </>}
              </div>
            </div>
          : <div className="empty-note">今天没有安排任务。去工作台为今天留一个小行动。</div>}

        <section className="up-next">
          <div className="section-label"><span>接下来</span><span>按自己的节奏</span></div>
          {remaining.map(node => (
            <div className="today-task" key={node.id}>
              <button
                className="task-check"
                aria-label={`完成${node.title}`}
                aria-pressed={node.status === 'completed'}
                onClick={() => apply({ type: 'UPDATE_STATUS', nodeId: node.id, status: node.status === 'completed' ? 'pending' : 'completed' })}
              >
                {node.status === 'completed' && <Check size={13} />}
              </button>
              <button onClick={() => { enterSpace(node.id); router.push('/workbench'); }}>
                {node.title}<small>{node.description}</small>
              </button>
              <span>{node.estimatedHours ? `${node.estimatedHours * 60} min` : '今天'}</span>
            </div>
          ))}
        </section>
      </section>

      {/* **这段"观察"是写死的演示文案。** 它说"你今天课程安排比较满""我把科研任务
          降低到了一个" —— 对示例空间成立,对一个真实新建的空间就是凭空捏造:知途
          从来没有拿到过用户的课表,也没有替谁做过这个决定。真实空间走 `RealToday`,
          那里只说它真的知道的事。 */}
      <aside className="today-aside">
        <Sparkles size={23} />
        <span className="eyebrow">来自知途的观察</span>
        <h2>给今天，留一点余地。</h2>
        <p>你今天课程安排比较满。<br /><br />我把科研任务降低到了一个，优先保证课程任务和 Attention 学习。</p>
        <button className="text-button" aria-expanded={why} onClick={() => setWhy(!why)}>{why ? '收起说明' : '为什么这样安排？'}<ArrowUpRight size={13} /></button>
        {why && <div className="insight-explanation">这是演示中的预设建议：长时间学习 Attention 已经需要较多精力，导师资料整理仅安排 30 分钟。减少切换，比塞满今天更重要。</div>}
        <div className="quiet-note">成长不只发生在<br />完成任务的那一刻。</div>
      </aside>
    </div>
  );
}

export function Today() {
  const { isRealSpace } = useDemo();

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
      {isRealSpace ? <RealToday /> : <DemoToday />}
    </div>
  );
}
