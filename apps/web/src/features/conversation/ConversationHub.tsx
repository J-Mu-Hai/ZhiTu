'use client';

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowUp, ArrowUpRight, MessageCircle, PencilLine, Plus, Search, Sparkles } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { Dialog } from '@/components/ui/Dialog';
import { degradedHint, sourceLabel } from '@/lib/backend';
import type { Conversation } from '@/types/growth';

type EditorMode = 'create' | 'edit' | null;

function parseTags(value: string): string[] {
  return [...new Set(value.split(/[,，、]/).map(tag => tag.trim()).filter(Boolean))].slice(0, 5);
}

export function ConversationHub() {
  const { growth, messages, conversations, setConversations, sendHistory, send, enterSpace, isRealSpace } = useDemo();
  const router = useRouter();
  const [active, setActive] = useState('admission');
  const [search, setSearch] = useState('');
  const [input, setInput] = useState('');
  const [editor, setEditor] = useState<EditorMode>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftTags, setDraftTags] = useState('');
  const bottom = useRef<HTMLDivElement>(null);

  const personalConversations = conversations.filter(conversation => !conversation.isExample);
  const showingExamples = personalConversations.length === 0;

  /**
   * 真实空间里**只有一条对话** —— 后端每个空间恰好一条 `kind='primary'` 的会话
   * (`uq_conversations_primary`)。所以这里不摆一个对话列表,而是把那条唯一的会话
   * 按真实的空间名显示出来。
   *
   * 上一版不管在哪个空间都先摆一条叫「保研计划」的对话,再把工作台的消息挂上去 ——
   * 于是一个叫「Python 学习」的空间里,对话列表的第一条写着"保研计划"。
   * 那个标题不是任何地方的记录,是这里现编的。
   */
  const admission: Conversation = {
    id: 'admission',
    title: '保研计划',
    linkedNodeIds: ['goal', 'research', 'contact'],
    tags: ['升学规划'],
    messages,
    isExample: true,
  };
  const primary: Conversation = {
    id: 'primary',
    title: growth.title,
    linkedNodeIds: [growth.goalId],
    messages,
  };
  const all = isRealSpace
    ? [primary]
    : showingExamples
      ? [admission, ...conversations.filter(conversation => conversation.isExample)]
      : personalConversations;
  const activeId = all.some(conversation => conversation.id === active) ? active : all[0].id;
  const current = all.find(conversation => conversation.id === activeId) ?? all[0];
  const filtered = all.filter(conversation =>
    conversation.title.includes(search)
    || conversation.tags?.some(tag => tag.includes(search))
    || conversation.messages.some(message => message.text.includes(search)),
  );

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' });
  }, [activeId, current.messages.length]);

  function openCreate() {
    setDraftTitle(''); setDraftTags(''); setEditor('create');
  }

  function openEdit() {
    setDraftTitle(current.title);
    setDraftTags(current.tags?.join('，') ?? '');
    setEditor('edit');
  }

  function saveConversation() {
    const title = draftTitle.trim();
    if (!title) return;
    const tags = parseTags(draftTags);
    if (editor === 'create') {
      const id = crypto.randomUUID();
      const conversation: Conversation = { id, title, tags, linkedNodeIds: ['goal'], messages: [] };
      setConversations(old => [conversation, ...old.filter(item => !item.isExample)]);
      setActive(id);
    } else if (editor === 'edit') {
      setConversations(old => old.map(conversation => conversation.id === activeId
        ? { ...conversation, title, tags }
        : conversation));
    }
    setEditor(null); setDraftTitle(''); setDraftTags('');
  }

  function submitMessage() {
    const text = input.trim();
    if (!text) return;
    // 真实空间直接走 send();它会把这一轮发给后端。sendHistory 是示例空间里
    // 那套"本地多对话"的写法,真实空间没有多对话可写。
    if (isRealSpace) void send(text); else sendHistory(activeId, text);
    setInput('');
  }

  return (
    <div className="conversation-hub">
      <aside className="hub-list">
        <header>
          <span className="eyebrow">THINKING TOGETHER</span>
          {/* 真实空间只有一条对话,"新建对话"点了也没有地方存 —— 后端每个空间
              恰好一条 primary 会话。藏在示例空间里,等后端支持多会话再放出来。 */}
          <h1>对话{!isRealSpace && <button className="icon-button" aria-label="新建对话" onClick={openCreate}><Plus size={19} /></button>}</h1>
          <label className="hub-search"><Search size={14} /><input aria-label="搜索历史对话" placeholder="搜索标题、标签与想法…" value={search} onChange={event => setSearch(event.target.value)} /></label>
        </header>
        <div className="hub-list-items">
          {filtered.map((conversation, index) => (
            <div key={conversation.id}>
              {/* "今天/昨天/更早"是照示例数据的位置写死的。真实空间里只有一条对话,
                  它可能包含几周前的消息,标成"今天"是错的,所以不显示。 */}
              {!isRealSpace && (index === 0 || showingExamples && (index === 2 || index === 3)) && <span className="hub-day">{index === 0 ? '今天' : index === 2 ? '昨天' : '更早'}</span>}
              <button className={`hub-item ${activeId === conversation.id ? 'active' : ''}`} onClick={() => setActive(conversation.id)}>
                <MessageCircle size={15} />
                <div>
                  <div className="hub-item-title"><strong>{conversation.title}</strong>{conversation.isExample && <span className="example-badge" title="系统预置的示例内容">示例</span>}</div>
                  {Boolean(conversation.tags?.length) && <div className="hub-item-tags">{conversation.tags?.map(tag => <span className="conversation-tag" key={tag}>{tag}</span>)}</div>}
                  <p>{conversation.messages.at(-1)?.text.slice(0, 32) || '开启一段新的思考'}</p>
                </div>
              </button>
            </div>
          ))}
          {!filtered.length && <p className="empty-note">没有找到相关对话。</p>}
        </div>
        <footer>有些答案，来自持续的对话。</footer>
      </aside>

      <section className="hub-thread">
        <header>
          <div>
            <span className="eyebrow">CONVERSATION</span>
            <div className="hub-thread-heading">
              <div>
                <div className="hub-thread-title"><h2>{current.title}</h2>{current.isExample && <span className="example-badge" title="系统预置的示例内容">示例</span>}</div>
                {Boolean(current.tags?.length) && <div className="conversation-tags">{current.tags?.map(tag => <span className="conversation-tag" key={tag}>{tag}</span>)}</div>}
              </div>
              {!current.isExample && !isRealSpace && <button className="hub-edit-button" aria-label="编辑对话" onClick={openEdit}><PencilLine size={14} />编辑</button>}
            </div>
          </div>
          {!isRealSpace && <span className="local-label">本地演示</span>}
        </header>
        <div className="hub-messages">
          {current.messages.length === 0 && <div className="empty-note">从一个问题开始。不用急着有答案。</div>}
          {current.messages.map(message => (
            <article className={`message ${message.role}${message.pending ? ' pending' : ''}${message.failed ? ' failed' : ''}`} key={message.id}>
              <div className="message-byline">
                {message.role === 'assistant' ? <Sparkles size={15} /> : <span className="user-dot">我</span>}
                <strong>{message.role === 'assistant' ? '知途' : '我'}</strong>
                {message.isExample && <span className="example-badge" title="系统预置的示例内容">示例</span>}
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
        <div className="hub-linked"><span>关联内容</span>{current.linkedNodeIds.map(id => <button onClick={() => { enterSpace(id); router.push('/workbench'); }} key={id}>{growth.nodes[id]?.title}<ArrowUpRight size={12} /></button>)}</div>
        <form className="hub-composer" onSubmit={event => { event.preventDefault(); submitMessage(); }}>
          <textarea aria-label="继续历史对话" placeholder="继续思考，或提出新的问题……" value={input} onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); submitMessage(); } }} />
          <button className="send-button" disabled={!input.trim()} aria-label="发送历史对话"><ArrowUp size={19} /></button>
        </form>
      </section>

      {editor && (
        <Dialog title={editor === 'create' ? '开启新的对话' : '编辑对话'} onClose={() => setEditor(null)}>
          <form className="node-form" onSubmit={event => { event.preventDefault(); saveConversation(); }}>
            <label>对话名称<input autoFocus value={draftTitle} maxLength={60} onChange={event => setDraftTitle(event.target.value)} placeholder="例如：期末学习安排" /></label>
            <label>对话标签<input value={draftTags} maxLength={100} onChange={event => setDraftTags(event.target.value)} placeholder="例如：学习计划，分数" /></label>
            <p>可用逗号或顿号分隔，最多保留 5 个标签。标签可以帮助你快速找到同类对话。</p>
            <button className="primary-button" disabled={!draftTitle.trim()}>{editor === 'create' ? '开始对话' : '保存修改'}</button>
          </form>
        </Dialog>
      )}
    </div>
  );
}
