'use client';
import { BrandMark } from '@/components/ui/BrandMark';
import { useEffect, useRef, useState } from 'react';
import { ArrowUp, Plus, X, CornerDownLeft, AlertCircle, RotateCcw, RefreshCw } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { degradedHint, fieldLabel, sourceLabel } from '@/lib/backend';

/**
 * 提案落下之后,卡片上显示的状态。
 *
 * 不能直接印 `remote.status` —— `pending_confirmation` 不是给人看的词。而且
 * **每一个状态都要有一句话**,包括 `failed`:`?? remote.status` 会在界面上留下
 * 一个英文枚举值,而用户没法判断那到底是"成了"还是"没成"。
 */
const PROPOSAL_STATUS_LABEL: Record<string, string> = {
  applied: '✓ 已写入计划',
  rejected: '已放弃这次变更，计划未改动',
  stale: '计划已经变了，这份提议作废',
  failed: '写入没有成功，计划未改动',
};

/**
 * 与 AI 的对话面板。
 *
 * ## 这一版为什么长这样
 *
 * 上一版没有"失败"这个状态。`send()` 在请求失败后吞掉异常、编一条中文回复,
 * 界面上看起来和真模型没有区别。用户没有任何办法知道那句话不是 AI 说的。
 *
 * 所以这里有四件以前没有的事:
 *
 * 1. **每条助手消息带来源徽标**。"AI 规划 · DeepSeek" / "本地规则 · 模型不可用"。
 *    历史消息也带 —— 往上翻的时候同样看得出来。
 * 2. **降级时说明原因**(额度用尽?超时?密钥无效?),而不是只给一个通用错误。
 * 3. **失败就是失败**:错误行 + 重试按钮。`retryable=false` 时按钮灰掉 ——
 *    让用户点一个注定失败的按钮比不给他按钮更糟。
 * 4. **还缺哪些规划条件**由服务端算,显示在输入框上方。模型挂了也该显示 ——
 *    它跟模型能不能用没关系。
 */
export function ConversationPanel() {
  const { growth, selectedId, select, messages, remoteProposals, proposalErrors, inputChanged, deciding, confirmRemote, rejectRemote, replan, replanState, send, retry, sending, sendError, retryable, brief, historyLoading, messagesTruncated, spaceId } = useDemo();
  const [input, setInput] = useState('');
  const [showContexts, setShowContexts] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const selected = selectedId ? growth.nodes[selectedId] : null;

  /**
   * 还没被决定、也没有哪条消息指向它的提案。
   *
   * 「没有消息指向」是关键条件,它挡住的是重复渲染:正常一轮对话产生的提案就
   * 挂在那条助手回复上(服务端把两者写在一起),再在末尾重复一遍是同一件事
   * 说两遍。
   *
   * 那么什么时候会真的出现"没有消息指向的提案"?**当那条消息不在这个列表里的时候。**
   * 后端每次只回最近的一批消息(`truncated` 为真就说明有更早的没回来),而提案是
   * 挂在消息上的 —— 一条两周前 AI 提过、用户一直没处理的调整,它的消息早就落在
   * 窗口之外了。这份渲染兜住的就是它:一份**还没被决定**的提案不应该因为用户
   * 聊了太多天就变得看不见、点不着。
   */
  const orphanProposals = remoteProposals.filter(
    proposal => proposal.status === 'validated' || proposal.status === 'pending_confirmation',
  ).filter(proposal => !messages.some(message => message.proposalId === proposal.id));

  // 提案也要跟着滚。`replan` 产出的那份提案是**追加在末尾**的,不滚过去的话
  // 用户点了「按执行情况调整」会看到界面毫无反应。
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }); }, [messages.length, sending, remoteProposals.length]);

  function submit() {
    if (!input.trim() || sending) return;
    void send(input.trim());
    setInput('');
  }

  return (
    <aside className="conversation-panel" aria-label="与 AI 一起思考">
      <header className="conversation-header">
        <div className="ai-symbol"><BrandMark size={28} /></div>
        <div>
          <h2>与 AI 一起思考</h2>
          <p>基于当前空间 · {growth.nodes[spaceId]?.title ?? growth.title}</p>
        </div>
        {/* 「示例空间」这个标签没有了 —— 因为它指的那个东西没有了。
            留在这里最坏的情况是它**永远不显示**,而那种"看不出来坏了"的控件
            比明着报错更难发现。 */}
        {/*
          等待回复的状态标识**不在这里**。

          它原来放在这个 `<header>` 里,而 `.floating-conversation .conversation-header`
          在 `demo2.css` 里是 `display:none` —— 也就是说,在这个面板**唯一被渲染的地方**
          (工作台那个浮动 Dock),它一次都不会显示。这不是"位置不够好",是一条
          永远不执行的路径:看起来做了,实际什么都没有发生。

          所以它挪到了 `.floating-title` —— Dock 上真正看得见的那一行(见
          `FloatingConversation`)。这里只留一句说明,免得下一个人又把它加回来。
        */}
      </header>

      <div className="conversation-history">
        {historyLoading && <p className="turn-loading">正在读取对话…</p>}

        {/* 空状态。它本来就锚在顶部(不是垂直居中),所以"下面一大片空白"的成因
            不是位置 —— 是**内容太薄**:一段小字加一句示例,撑不满下面那一大块。
            按这个面板真实能做的事列三条,把洞填上。

            这三条**全是面板里本来就存在的文案**,不是新编的能力:
            第 1 条来自这一段原来的那句话,第 2 条来自 `.replan-row` 的按钮,
            第 3 条来自 `.context-hint`。编一条做不到的事写在这里,比留一片空白更坏。 */}
        {!historyLoading && !messages.length && (
          <div className="conversation-empty">
            <BrandMark size={24} />
            <strong>说说你想推进什么</strong>
            <p>比如「我想在三个月内完成一个 Python 项目」。</p>
            <ul className="conversation-abilities">
              <li><span className="tiny-dot" />先问清楚截止时间、每周能投入多少时间、现在的水平，再动手排计划</li>
              <li><span className="tiny-dot" />按最近的执行情况调整计划</li>
              <li><span className="tiny-dot" />选择画布中的节点，让讨论更聚焦</li>
            </ul>
          </div>
        )}

        {/* 更早的消息没有一起返回。**必须说出来。**
            不说的话,列表的第一条就是一句没头没尾的话(「好的,那我按每周 4 小时
            排」),而用户会以为自己以前的记录丢了 —— 或者更糟,以为 AI 突然开始
            答非所问。 */}
        {messagesTruncated && (
          <p className="conversation-truncated">
            这里只显示了最近 {messages.length} 条。更早的对话还在，只是没有一起取回来。
          </p>
        )}

        {messages.map(m => {
          const remote = m.proposalId ? remoteProposals.find(p => p.id === m.proposalId) : undefined;
          return (
            <article className={`message ${m.role}${m.pending ? ' pending' : ''}${m.failed ? ' failed' : ''}`} key={m.id}>
              <div className="message-byline">
                {m.role === 'assistant'
                  ? <><BrandMark size={20} /><strong>知途</strong><span>与你一起</span></>
                  : <><span className="user-dot">我</span><strong>我</strong></>}
              </div>

              <div className="message-text">{m.text}</div>

              {/* 来源徽标。只在助手的回复上出现,而且历史消息同样显示。 */}
              {m.role === 'assistant' && m.source && (
                <div className={`source-badge${m.degraded ? ' degraded' : ''}`}>
                  {sourceLabel(m.source)}
                  {m.degraded && m.degradedReason ? ` · ${degradedHint(m.degradedReason)}` : ''}
                </div>
              )}
              {m.failed && <div className="message-note failed">这一条没有发出去。</div>}
              {m.pending && <div className="message-note">已记录，正在等 AI 回复…</div>}

              {/* 这里曾经还有一张"本地提案"卡片(`proposals` / `accept` /`previewProposal`)。
                  它和下面这张后端的提案卡片**不是同一个东西**,只是名字像:那个是示例
                  空间里本地编出来的"把某个节点挪一挪",确认了也只改浏览器内存。示例空间
                  删掉之后它没有生产者了,一并删掉 —— 留着两张长得像、坏得不一样的卡片,
                  比少一张更难查。 */}

              {/* 后端提案:AI 想对计划做的变更。**要用户点"确认"才写进计划** ——
                  模型不能替用户调这个接口,这是产品规则不是技术细节。 */}
              {remote && (
                <div className="proposal">
                  <span className="eyebrow">AI 提议的变更</span>
                  <strong>
                    {remote.itemCount > 0
                      ? `${remote.itemCount} 项变更`
                      : '这次没有可执行的变更'}
                  </strong>
                  {remote.items.length > 0 && (
                    <ul className="proposal-items">
                      {/* 摘要由**服务端**生成(它知道每一项到底指向哪个节点),
                          前端只负责显示。在前端把 op 和参数拼成一句话的话,
                          两个地方就得各维护一套"人话",迟早对不上。 */}
                      {remote.items.slice(0, 8).map(item => <li key={item.ordinal}>{item.summary}</li>)}
                      {remote.items.length > 8 && <li>…还有 {remote.items.length - 8} 项</li>}
                    </ul>
                  )}
                  {remote.status === 'validated' || remote.status === 'pending_confirmation'
                    ? <div>
                        <button disabled={deciding} onClick={() => void rejectRemote(remote.id)}>先不要</button>
                        <button className="primary-button" disabled={deciding} onClick={() => void confirmRemote(remote.id)}>
                          {deciding ? '处理中…' : '确认，写入计划'}
                        </button>
                      </div>
                    : <span className="proposal-status">{PROPOSAL_STATUS_LABEL[remote.status] ?? remote.status}</span>}
                </div>
              )}
            </article>
          );
        })}

        {/* 这一轮是在一份**已经过去的输入**上回答的。
            它必须单独说一句,而且必须在"没有提案"那一片空白之前说:
            `INPUT_CHANGED` 拦下来的那一轮通常什么都不提,于是界面上"这次没有提案"
            与"模型什么都没想出来"长得一模一样 —— 用户会以为自己白问了,而真正该做
            的是改完之后让 AI 重看一遍。
            **不给按钮。** 入口在那些内容的旁边(节点详情的「AI 分析」块里),
            在对话末尾再放一个的话,用户看不出它要重新分析的是哪个节点。 */}
        {inputChanged && (
          <div className="turn-error" role="status">
            <AlertCircle size={14} />
            <span>
              它回答的时候，你说的情况已经变了（正文、条件或计划被改过），所以这一轮
              没有给出可应用的变更。要看基于最新内容的判断，请到那个节点的「AI 分析」里
              点「根据最新内容重新分析」。
            </span>
          </div>
        )}

        {/* 模型提了变更、但校验没让过。**必须说出来。**
            不显示的话,用户看到的是"AI 回复了一段话,但计划什么都没变",而他会
            以为是自己没说清楚 —— 于是换个说法再说一遍,而问题不在他。 */}
        {proposalErrors.length > 0 && (
          <div className="turn-error" role="alert">
            <AlertCircle size={14} />
            <span>
              这次提议的变更没有通过检查，计划没有被改动：{proposalErrors.map(e => e.message).join('；')}
            </span>
          </div>
        )}

        {/* 还没有任何一条消息指向它的提案。
            **这份渲染不是锦上添花,是"按执行情况调整"能被用起来的前提。**
            上面那份渲染是靠 `m.proposalId` 找到提案的,只按消息找的话,一份
            消息已经落在窗口外的提案会静静地躺在库里 —— 用户看不到,也点不了
            确认,甚至连"系统提过调整"这件事都不知道。 */}
        {orphanProposals.map(proposal => (
          <article className="message assistant" key={proposal.id}>
            <div className="message-byline"><BrandMark size={20} /><strong>知途</strong><span>按你的执行情况</span></div>
            <div className="message-text">
              我看了最近的执行情况，提出下面这些调整。你看过之后再决定要不要写进计划。
            </div>
            <div className="proposal">
              <span className="eyebrow">AI 提议的变更</span>
              <strong>{proposal.itemCount > 0 ? `${proposal.itemCount} 项变更` : '这次没有可执行的变更'}</strong>
              {proposal.items.length > 0 && (
                <ul className="proposal-items">
                  {proposal.items.slice(0, 8).map(item => <li key={item.ordinal}>{item.summary}</li>)}
                  {proposal.items.length > 8 && <li>…还有 {proposal.items.length - 8} 项</li>}
                </ul>
              )}
              <div>
                <button disabled={deciding} onClick={() => void rejectRemote(proposal.id)}>先不要</button>
                <button className="primary-button" disabled={deciding} onClick={() => void confirmRemote(proposal.id)}>
                  {deciding ? '处理中…' : '确认，写入计划'}
                </button>
              </div>
            </div>
          </article>
        ))}

        {/* 失败是可见的状态,不是一句被吞掉的异常。 */}
        {sendError && (
          <div className="turn-error" role="alert">
            <AlertCircle size={14} />
            <span>{sendError}{!retryable && ' 这一条重试也不会成功。'}</span>
            <button type="button" disabled={!retryable || sending} onClick={() => void retry()}>
              <RotateCcw size={12} />重试
            </button>
          </div>
        )}

        <div ref={bottom} />
      </div>

      <div className="composer-area">
        {/* 「按执行情况调整」的入口。
            放在对话里而不是排期页,是因为它的产出是一份**要用户确认的提案**,
            而确认的界面就在这里 —— 换个地方发起、再让用户回来确认,中间那一步
            用户是会丢的。 */}
        <div className="replan-row">
          <button type="button" className="text-button" disabled={replanState.busy || sending} onClick={() => void replan()}>
            <RefreshCw size={12} />{replanState.busy ? '正在看最近的执行情况…' : '按最近的执行情况调整计划'}
          </button>
          {/* 降级时说"这次没能给出方案",不说"不需要调整" —— 前者要用户重试,
              后者要用户放心,这两句话差别很大,不能合成一句空白。 */}
          {replanState.message && (
            <p className={replanState.degraded ? 'replan-note degraded' : 'replan-note'} role="status">
              {replanState.message}
            </p>
          )}
        </div>

        {/* 还缺哪些条件由服务端算。它跟模型能不能用无关,所以模型挂了也要显示。 */}
        {brief && brief.missing.length > 0 && (
          <div className="brief-missing" title="AI 会先问清楚这些再排计划">
            <span className="tiny-dot" />
            还缺：{brief.missing.map(fieldLabel).join('、')}
          </div>
        )}

        {selected
          ? <div className="context-chip"><span className="tiny-dot" />正在讨论：{selected.title}<button aria-label="清除上下文" onClick={() => select(null)}><X size={12} /></button></div>
          : <div className="context-hint"><span className="tiny-dot" />选择画布中的节点，让讨论更聚焦</div>}

        {showContexts && (
          <div className="context-options">
            {Object.values(growth.nodes).filter(node => node.type === 'capability').map(node => (
              <button key={node.id} onClick={() => { select(node.id); setShowContexts(false); }}>{node.title}</button>
            ))}
          </div>
        )}

        <form className="composer" onSubmit={e => { e.preventDefault(); submit(); }}>
          <textarea
            aria-label="给 AI 的消息"
            placeholder={selected ? `关于「${selected.title}」，告诉 AI 你的想法……` : '我想在……之内完成……'}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }}
          />
          <div className="composer-actions">
            <button type="button" className="icon-button" aria-label="添加讨论对象" onClick={() => setShowContexts(!showContexts)}><Plus size={18} /></button>
            <span><CornerDownLeft size={11} />发送 · Shift + Enter 换行</span>
            <button type="submit" aria-label="发送消息" className="send-button" disabled={!input.trim() || sending}><ArrowUp size={18} /></button>
          </div>
        </form>
        {/* 这里不再写死"DeepSeek" —— 每一轮到底是谁生成的,由消息上方的徽标说。 */}
        <p className="composer-footnote">一起思考，由你决定。<span>回复会写明来源</span></p>
      </div>
    </aside>
  );
}
