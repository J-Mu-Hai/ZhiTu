'use client';

/**
 * 节点详情里那一块折叠的「AI 分析」。
 *
 * ## 它为什么折叠,而且默认是收起的
 *
 * 分析是一段**给用户挑错**的判断,不是节点的一部分。展开着放,它会被当成本节点的
 * 说明读 —— 而它既不是用户写的正文,也不是已确认的约束(这一批的一条硬边界:
 * 「分析草稿不能冒充正式约束」)。收起时它只占一行,那一行上带着**过期徽标** ——
 * 于是"我改过正文之后旧分析作废了"这件事不用展开就能看见。
 *
 * ## 徽标为什么在收起状态也要准
 *
 * 因为验收路径就是「改正文 → 徽标变过期 → 重新分析」。徽标藏在折叠区里面的话,
 * 用户得先展开才知道自己的修改让什么作废了。所以这个组件**一挂载就读一次**
 * (只读最近一条),而不是等展开才读。
 *
 * ## 过期是**读的时候**算出来的
 *
 * 这里没有"把徽标设成过期"这回事。服务端每次读都比一遍"当时看到的输入"和"现在库里的
 * 样子",所以正文一存成功,这里重读一次徽标就变了 —— 靠的是 `refreshToken`
 * (正文保存成功的时间戳)重新拉一次,而不是本地推一个状态。
 */

import { useCallback, useEffect, useState } from 'react';

import { useDemo } from '@/features/growth/provider';
import { getAnalyses, type AnalysisView } from '@/lib/backend';

/** 七栏的中文名。**顺序就是服务端 `SECTION_FIELDS` 的顺序** —— 与契约、控制层共用
 *  同一份顺序,三处各写一遍的话某天加了一栏就总会漏掉一处。 */
const SECTION_LABELS: [keyof AnalysisView, string, string][] = [
  ['known', '已知', '模型这一轮真的读到的事实'],
  ['unknowns', '还缺什么', '要判断得更准,还需要什么'],
  ['evidence', '依据', '用户给的、带来源与日期的材料'],
  ['assumptions', '假设', '模型在没有依据时替你假设的 —— 这一栏最值得挑错'],
  ['diagnosis', '判断', '模型的推理与结论'],
  ['strategyOptions', '可选的走法', '每种走法的代价'],
  ['risks', '风险', '可能出问题的地方'],
];

/**
 * 这条判断是谁给的。
 *
 * **不复用 `sourceLabel`**,因为它说的是"AI 规划 · DeepSeek" —— 那是对话徽标的说法。
 * 这里要说的是**一条判断的出处**,而降级时那句话必须一眼看出"不是模型说的":
 * 一份不是模型给的判断长得和真的一模一样,是这一层最容易骗到人的地方。
 */
function judgementSource(modelSource: string | null): string {
  switch (modelSource) {
    case 'openjiuwen':
      return '模型给的判断（openJiuwen）';
    case 'direct_llm':
      return '模型给的判断';
    case 'rule_fallback':
      return '本地规则给的 —— 这一条不是模型的判断';
    case 'unavailable':
      return '当时模型不可用 —— 这一条不含模型的判断';
    default:
      return '来源未知';
  }
}

/** 一个能给用户看的时间。ISO 串直接铺在界面上是给机器看的。 */
function when(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

interface Loaded {
  latest: AnalysisView | null;
  /** 更早还有几条。**必须说出来** —— 只显示最近一条而不说,用户会以为只分析过一次。 */
  earlier: number;
  note: string;
}

export function NodeAnalysisPanel({
  nodeId,
  refreshToken = '',
}: {
  nodeId: string;
  /** 变了就重读一次。正文保存成功时由调用方换一个值(见文件头)。 */
  refreshToken?: string;
}) {
  // **`workspaceId`,不是 `spaceId`。** 后者是"用户此刻点进了哪一层"的**节点** id
  // (见 provider 的 `currentSpaceId`),拿它当空间 id 去拼 URL 会得到一个 404 ——
  // 而那个 404 在这块面板上长得就是"没读到",看不出是拼错了。
  const { workspaceId, isRealSpace, reanalyze, sending } = useDemo();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<Loaded | null>(null);

  const load = useCallback(async () => {
    if (!isRealSpace || !workspaceId) return;
    setLoading(true);
    setError(null);
    try {
      // 多读几条只为了知道"更早还有几条"。**不展开那些** —— 逐条铺开会把"最近那一条
      // 还成不成立"这件事淹没在旧判断里,而用户此刻要判断的就是那个。
      const result = await getAnalyses(workspaceId, { focusNodeId: nodeId, limit: 5 });
      setLoaded({
        latest: result.analyses[0] ?? null,
        earlier: Math.max(0, result.analyses.length - 1),
        note: result.note,
      });
    } catch (cause) {
      // 读不到就说读不到。**不能**显示成"还没有分析过" —— 那两件事在界面上长得
      // 一模一样,而它们的下一步完全不同(一个是去找 AI 聊,一个是刷新重试)。
      setError(cause instanceof Error ? cause.message : '分析记录没读出来。');
      setLoaded(null);
    } finally {
      setLoading(false);
    }
  }, [isRealSpace, workspaceId, nodeId]);

  useEffect(() => {
    void load();
  }, [load, refreshToken]);

  async function refresh() {
    await reanalyze(nodeId);
    // **重新读一次,而不是本地把徽标改成"最新"。** 那一轮可能降级、可能没留下
    // 记录,本地改的话徽标会说一句没有依据的话。
    await load();
  }

  if (!isRealSpace) return null;

  const latest = loaded?.latest ?? null;
  const stale = latest?.freshness === 'stale';

  return (
    <section className="analysis-panel">
      <div className="analysis-head">
        <button
          type="button"
          className="text-button analysis-toggle"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          AI 分析
        </button>
        {loading && <span className="analysis-badge is-muted">读取中…</span>}
        {!loading && error && <span className="analysis-badge is-muted">没读到</span>}
        {!loading && !error && !latest && (
          <span className="analysis-badge is-muted">还没有分析过</span>
        )}
        {!loading && !error && latest && !stale && (
          <span className="analysis-badge is-fresh">基于当前内容</span>
        )}
        {!loading && !error && stale && (
          <span className="analysis-badge is-stale">内容已变，这份过期了</span>
        )}
      </div>

      {open && (
        <div className="analysis-body">
          {error && <p className="form-error" role="alert">{error}</p>}

          {!error && !latest && (
            <p className="analysis-hint">
              {/* 这一段是**服务端**给的(`AnalysisListResponse.note`),不是这里编的。
                  契约里那句"空列表时界面必须用它说清是「没有」还是「没查到」"指的就是
                  这一行 —— 自己写一段意思差不多的话,后端哪天改了口径这里就对不上了。 */}
              {loaded?.note}
              {/* 折叠区里也放一个入口:用户点开这一块的动作本身就说明了意图,
                  再让他回到对话里去说一句话是多余的一步。 */}
              <br />
              下面那个按钮就是那个入口 —— 它会替你说一句「根据最新内容重新分析一下这个节点」。
            </p>
          )}

          {!error && latest && (
            <>
              <p className="analysis-meta">
                {judgementSource(latest.modelSource)} · {when(latest.createdAt)}
                {latest.focusNodeTitle ? ` · 讨论的是「${latest.focusNodeTitle}」` : ''}
                {loaded && loaded.earlier > 0
                  ? ` · 更早还有 ${loaded.earlier} 条，这里显示最近一条`
                  : ''}
                {/* 服务端对**这一批**的交代("共 N 条,其中 M 条基于已经变过的内容")。
                    自己按界面上的数字重数一遍是不行的:这里只拿到了最近几条。 */}
                {loaded?.note ? ` · ${loaded.note}` : ''}
              </p>

              {stale && (
                <div className="analysis-stale" role="status">
                  <b>这条判断是在你说的情况变了之前做出的，现在不一定还成立。</b>
                  {latest.staleReasons.length > 0 ? (
                    <ul>
                      {latest.staleReasons.map(reason => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  ) : (
                    <p>
                      {/* 理由逐条给不出来时**也要说清是为什么**,不能只留一个"已过期"。
                          沉默会让人以为系统搞错了,而他没法反驳一个不说理由的结论。 */}
                      系统比对出输入变过，但逐条说不出是哪里变的 —— 这种情况下按「可能已经
                      不成立」处理，别拿它当依据。
                    </p>
                  )}
                </div>
              )}

              {latest.coverageNote && (
                <p className="analysis-coverage">
                  {/* 只读到一部分时必须写出来。不说的话，一份只读了前几十个节点的判断
                      看起来像读全了。 */}
                  {latest.coverageNote}
                </p>
              )}

              {SECTION_LABELS.map(([field, label, hint]) => {
                const items = (latest[field] as string[] | undefined) ?? [];
                if (items.length === 0) return null;
                return (
                  <div className="analysis-section" key={field}>
                    <h4>{label}</h4>
                    <ul>
                      {items.map(item => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                    <p className="analysis-hint">{hint}</p>
                  </div>
                );
              })}

              {latest.confidenceNote && (
                <p className="analysis-confidence">
                  模型对这份判断的把握：{latest.confidenceNote}
                </p>
              )}

              <p className="analysis-hint">
                {/* 这一句是这一块存在的边界,写在最显眼的地方:它是判断,不是正文,
                    也不是已确认的约束。用户看到一段条理清楚的分析时最容易忘掉这件事。 */}
                这是 AI 的判断，<b>不在你的正文里</b>，也没有变成排期或约束。要留住它请自己写进详细说明；
                要它作废就改这个节点的内容。
              </p>
            </>
          )}

          <div className="analysis-actions">
            <button type="button" className="text-button" disabled={sending} onClick={() => void refresh()}>
              {sending ? '正在重新分析…' : '根据最新内容重新分析'}
            </button>
            <span className="analysis-hint">
              它会替你说一句话并真的走一轮对话 —— 所以回复也会出现在对话里。
            </span>
          </div>
        </div>
      )}
    </section>
  );
}
