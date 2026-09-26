'use client';
import { BrandMark } from '@/components/ui/BrandMark';

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowUp, ArrowUpRight, MessageCircle } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { degradedHint, sourceLabel } from '@/lib/backend';

/**
 * 「对话」页。
 *
 * ## 列表里为什么只有一条
 *
 * 因为**后端只有一条**:每个空间恰好一条 `kind='primary'` 会话,`uq_conversations_primary`
 * 这个唯一约束就是这条规则的落点。
 *
 * 上一版这里是一个多对话中心 —— 左侧一列对话、新建、编辑标签、按标题和标签搜索。
 * 那一列对话**全部来自示例空间的演示数据**(一份本地数组,存在浏览器里),而真实空间
 * 那一条是用 `isRealSpace` 分支硬塞进同一个列表的。于是"新建对话"在真实空间里点了
 * 没有地方存,只能藏起来;用户看到的是一个**按钮藏起来、列表却还在**的界面,
 * 他会以为自己的对话丢了。列表里还写着"今天/昨天" —— 那是照演示数据的位置写死的,
 * 而唯一那条真实会话可能装着几周前的消息。
 *
 * 现在只列出**真实存在的那一条**。后端支持多会话的那一天,这个列表会长出第二行 ——
 * 到那时它列的是后端真的返回的几行,而不是一份本地数组。
 */
export function ConversationHub() {
  const { growth, messages, send, sending, enterSpace } = useDemo();
  const router = useRouter();
  const [input, setInput] = useState('');
  const bottom = useRef<HTMLDivElement>(null);
  // 「关联内容」里的节点。它原来是示例数据里写死的 `linkedNodeIds` 数组,
  // 现在用**这条对话里真实出现过的 `contextId`** —— 这是唯一有真实来源的一份。
  const linkedNodes = [...new Set(messages.map(message => message.contextId).filter(Boolean))]
    .map(id => growth.nodes[id!])
    .filter(Boolean)
    .slice(-4);

  useEffect(() => { bottom.current?.scrollIntoView({ block: 'end' }); }, [messages.length]);

  function submitMessage() {
    const text = input.trim();
    if (!text) return;
    void send(text);
    setInput('');
  }

  /** 点关联节点:进它所在的那一层再回工作台 —— 和工作台里的"进入子空间"是同一条路。 */
  function goToNode(nodeId: string) {
    enterSpace(nodeId);
    router.push('/workbench');
  }

  return (
    <div className="conversation-hub">
      <aside className="hub-list">
        <header>
          <span className="eyebrow">THINKING TOGETHER</span>
          <h1>对话</h1>
        </header>
        <div className="hub-list-items">
          <div>
            <button className="hub-item active">
              <MessageCircle size={15} />
              <div>
                <div className="hub-item-title"><strong>{growth.title}</strong></div>
                <p>{messages.at(-1)?.text.slice(0, 32) || '开启一段新的思考'}</p>
              </div>
            </button>
          </div>
        </div>
        <footer>有些答案，来自持续的对话。</footer>
      </aside>

      <section className="hub-thread">
        <header>
          <div>
            <span className="eyebrow">CONVERSATION</span>
            <div className="hub-thread-heading">
              <div>
                <div className="hub-thread-title"><h2>{growth.title}</h2></div>
              </div>
            </div>
          </div>
        </header>
        <div className="hub-messages">
          {messages.length === 0 && <div className="empty-note">从一个问题开始。不用急着有答案。</div>}
          {messages.map(message => (
            <article className={`message ${message.role}${message.pending ? ' pending' : ''}${message.failed ? ' failed' : ''}`} key={message.id}>
              <div className="message-byline">
                {message.role === 'assistant' ? <BrandMark size={22} /> : <span className="user-dot">我</span>}
                <strong>{message.role === 'assistant' ? '知途' : '我'}</strong>
              </div>
              <div className="message-text">{message.text}</div>
              {/* 这里和工作台显示同一批消息,来源徽标也必须一样 ——
                  否则用户在工作台看到"模型不可用",翻到"对话"页就看不见了。 */}
              {message.role === 'assistant' && message.source && (
                <div className={`source-badge${message.degraded ? ' degraded' : ''}`}>
                  {sourceLabel(message.source)}
                  {message.degraded && message.degradedReason ? ` · ${degradedHint(message.degradedReason)}` : ''}
                </div>
              )}
            </article>
          ))}
          <div ref={bottom} />
        </div>
        {linkedNodes.length > 0 && (
          <div className="hub-linked">
            <span>关联内容</span>
            {linkedNodes.map(node => (
              <button key={node.id} onClick={() => goToNode(node.id)}>{node.title}<ArrowUpRight size={12} /></button>
            ))}
          </div>
        )}
        <form className="hub-composer" onSubmit={event => { event.preventDefault(); submitMessage(); }}>
          <textarea aria-label="继续历史对话" placeholder="继续思考，或提出新的问题……" value={input} onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); submitMessage(); } }} />
          <button className="send-button" disabled={!input.trim() || sending} aria-label="发送历史对话"><ArrowUp size={19} /></button>
        </form>
      </section>
    </div>
  );
}
