'use client';
import { useState } from 'react';
import type { V01TimelineItemView } from '@/lib/backend';
import styles from './V01TimelineAxis.module.css';

/**
 * 规划智能体 V0.1:把时间线投影**画进主轴本身**,而不是在时间线上方再摆一块说明面板。
 *
 * ## 数据只来自一处
 *
 * `reasoning.v01Timeline` 是服务端给出的**结构化**投影(周次或日期 + 成果 +
 * 完成标准 + draft/planned)。前端**不解析自然语言描述去猜日期** —— 猜出来的
 * 日期迟早和真实计划对不上。
 *
 * ## 相对周与绝对日期
 *
 * 没有日历日期时,主轴是“第 1 周、第 2 周…”的相对周标尺;`第 1–2 周`画成从
 * Week 1 到 Week 2 的真实区间,区间终点放一个里程碑点。有日期时用日期标尺。
 * 两种模式共用同一套百分比定位,只有刻度文案不同。
 *
 * ## 草案与确认
 *
 * 草案是虚线 + 低饱和度 + “待确认”;确认后是实线 + 正式颜色。确认/调整按钮在
 * **底部轻量操作区**,不占主轴上方的空间。
 */
export function V01TimelineAxis({
  items,
  proposalId,
  deciding,
  onConfirm,
  onReject,
}: {
  items: V01TimelineItemView[];
  proposalId: string | null;
  deciding: boolean;
  onConfirm: (id: string) => void;
  onReject: (id: string) => void;
}) {
  const [selected, setSelected] = useState<string | null>(items[0]?.id ?? null);
  const dated = items.some(item => item.startDate && item.endDate);
  const draft = items.some(item => item.status === 'draft');

  // 统一的百分比定位:相对周用周次,绝对日期用天数。
  let span = 1;
  let base = 0;
  const weekTick = (week: number) =>
    dated ? '' : `第 ${week} 周`;
  if (dated) {
    const dates = items
      .flatMap(item => [item.startDate, item.endDate])
      .filter((value): value is string => Boolean(value))
      .map(value => Date.parse(value));
    base = Math.min(...dates);
    span = Math.max(1, (Math.max(...dates) - base) / 86400000);
  } else {
    base = 1;
    span = Math.max(1, ...items.map(item => item.endWeek ?? 1));
  }
  const pct = (value: number) => ((value - base) / span) * 100;
  const left = (item: V01TimelineItemView) =>
    dated
      ? pct((Date.parse(item.startDate as string) - base) / 86400000)
      : pct((item.startWeek ?? 1) - 1);
  const right = (item: V01TimelineItemView) =>
    dated
      ? pct((Date.parse(item.endDate as string) - base) / 86400000)
      : pct(item.endWeek ?? item.startWeek ?? 1);
  const ticks = dated
    ? Array.from({ length: Math.min(8, Math.round(span / 7) + 1) }, (_, i) =>
        new Date(base + i * (span / Math.min(8, Math.round(span / 7) + 1)) * 86400000),
      ).map(date => `${date.getUTCMonth() + 1}.${date.getUTCDate()}`)
    : Array.from({ length: span }, (_, i) => weekTick(i + 1));

  const current = items.find(item => item.id === selected) ?? null;

  return (
    <div className={styles.root} data-testid="v01-timeline" data-mode={dated ? 'dated' : 'relative'} data-draft={draft}>
      <div className={styles.head}>
        <span className={styles.status} data-testid="v01-timeline-status">
          {dated ? (draft ? '时间架构草案，尚未写入计划' : '已确认时间线') : draft ? '相对周草案，日期待校准' : '相对周时间线'}
        </span>
      </div>

      <div className={styles.axisWrap}>
        <div className={styles.axis} aria-hidden="true" />
        {ticks.map((tick, index) => (
          <span key={index} className={styles.tick} style={{ left: `${(index / Math.max(1, ticks.length - 1)) * 100}%` }}>
            {tick}
          </span>
        ))}
        {items.map((item, index) => {
          const itemDraft = item.status === 'draft';
          return (
            <div key={item.id} className={styles.phaseRow} style={{ top: 26 + index * 44 }}>
              <button
                type="button"
                data-testid="v01-phase-bar"
                data-draft={itemDraft}
                data-kind={item.kind}
                data-status={item.status}
                data-start={item.startDate ?? `W${item.startWeek ?? ''}`}
                data-end={item.endDate ?? `W${item.endWeek ?? ''}`}
                aria-label={`${item.title}，${dated ? `${item.startDate} 至 ${item.endDate}` : `第 ${item.startWeek}–${item.endWeek} 周`}`}
                className={`${styles.bar} ${itemDraft ? styles.draftBar : styles.plannedBar} ${selected === item.id ? styles.selected : ''}`}
                style={{ left: `${left(item)}%`, width: `${Math.max(2, right(item) - left(item))}%` }}
                onClick={() => setSelected(item.id)}
              >
                <strong>{item.title}</strong>
                {item.deliverable && <small>{item.deliverable}</small>}
              </button>
              {/* 区间终点:里程碑 / 成果点。 */}
              <span
                className={`${styles.milestone} ${itemDraft ? styles.draftDot : styles.plannedDot}`}
                style={{ left: `${right(item)}%` }}
                title={item.deliverable || item.title}
                data-testid="v01-milestone"
                data-phase={item.id}
                aria-hidden="true"
              />
            </div>
          );
        })}
      </div>

      {current && (
        <div className={styles.detail} data-testid="v01-timeline-detail">
          <div className={styles.detailHead}>
            <strong>{current.title}</strong>
            <span>{dated ? `${current.startDate} → ${current.endDate}` : `第 ${current.startWeek}–${current.endWeek} 周`}</span>
          </div>
          {current.goal && <p>目标：{current.goal}</p>}
          {current.deliverable && <p>成果：{current.deliverable}</p>}
          {current.completionCriteria && <p>完成标准：{current.completionCriteria}</p>}
        </div>
      )}

      {draft && (
        <div className={styles.actions} data-testid="v01-timeline-actions">
          {proposalId ? (
            <>
              <button type="button" className={styles.confirm} disabled={deciding} onClick={() => onConfirm(proposalId)}>
                {deciding ? '处理中…' : '确认这条战略'}
              </button>
              <button type="button" className={styles.adjust} disabled={deciding} onClick={() => onReject(proposalId)}>
                调整战略
              </button>
            </>
          ) : (
            <span className={styles.note}>草案已在对话中待确认。</span>
          )}
        </div>
      )}
    </div>
  );
}
