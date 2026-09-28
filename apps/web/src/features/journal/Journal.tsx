'use client';
import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ImagePlus, Link2, Tag, ArrowUpRight, X, PenLine, ArrowRight } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { FilePreview } from '@/components/growth/SpaceFiles';
import type { FileAsset } from '@/types/growth';

export function Journal() {
  const { growth, journals, publishJournal, files, enterSpace } = useDemo();
  const router = useRouter();
  const [content, setContent] = useState('');
  const [tag, setTag] = useState('');
  const [linked, setLinked] = useState('');
  const [images, setImages] = useState<File[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [tools, setTools] = useState(false);
  const [filter, setFilter] = useState('全部');
  const [selected, setSelected] = useState<string | null>(null);
  const [preview, setPreview] = useState<FileAsset | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const visible = journals.filter(j => filter === '全部' || j.tags?.includes(filter));
  const current = visible.find(j => j.id === selected) ?? visible[0];
  const filters = Array.from(new Set(['全部', '科研', '思考', '情绪', ...journals.flatMap(j => j.tags ?? [])]));

  /*
   * 哪几条是**这一帧才出现**的 —— 只有它们播那一次淡入。
   *
   * 为什么不能直接给 `.journal-list-item` 挂动画:整张列表会因为切筛选、切页面、
   * 发布一篇而重算,挂在"条目存在"上的动画会让**整个列表**一起重播一遍淡入,
   * 看起来像列表被刷新了。所以这里按 id 集合做差:上一次没有、这一次有的,才是新的。
   *
   * 用 id 做差而不是"第一条第几条":位置会变,id 不会。也不看内容 —— 标题长得一样
   * 的两篇随笔是两个不同的 id,而按标题认的那一套(规范 4.2 明令禁止用在关系上)
   * 在这里同样会把第二篇认成第一篇。
   *
   * 首次挂载时 `knownIds` 是空的,**不把已有的都算成新的** —— 那不是"刚发生的事"。
   */
  const [fresh, setFresh] = useState<string[]>([]);
  const knownIds = useRef<Set<string> | null>(null);
  useEffect(() => {
    const ids = journals.map(j => j.id);
    if (knownIds.current === null) { knownIds.current = new Set(ids); return; }
    const added = ids.filter(id => !knownIds.current!.has(id));
    knownIds.current = new Set(ids);
    // 已经不在列表里的 id 顺手清掉;没变就原样交回去,不制造一次多余的渲染。
    setFresh(old => {
      const kept = old.filter(id => ids.includes(id));
      return added.length ? [...kept, ...added] : kept.length === old.length ? old : kept;
    });
  }, [journals]);
  return <div className="editorial-page journal-page">
    <header className="editorial-header"><span className="eyebrow">LITTLE THINGS, REAL GROWTH</span><h1>随笔</h1><p>留下一点想法，也给自己一点慢下来的空间。</p></header>
    {!expanded ? <button className="journal-compose-trigger" onClick={() => setExpanded(true)}><PenLine size={19}/><span>{content ? '继续刚才未发布的文字…' : '写下一点此刻的想法……'}</span><ArrowRight size={18}/></button> :
      <form className="journal-composer" onSubmit={e => { e.preventDefault(); if (!content.trim()) return; publishJournal(content, tag.trim() ? [tag.trim()] : [], linked ? [linked] : [], images); setContent(''); setImages([]); setTag(''); setLinked(''); setExpanded(false); setFilter('全部'); setSelected(null); }}>
        <div className="journal-compose-heading"><span>此刻的记录</span><button type="button" aria-label="收起随笔输入框" onClick={() => setExpanded(false)}><X size={17}/></button></div>
        <textarea autoFocus aria-label="此刻的想法" placeholder="此刻的想法……" value={content} onChange={e => setContent(e.target.value)} maxLength={10000}/>
        {images.length > 0 && <div className="attachment-chips">{images.map((f, i) => <span key={i}>{f.name}<button type="button" aria-label={`移除图片 ${f.name}`} onClick={() => setImages(old => old.filter((_, index) => index !== i))}><X size={12}/></button></span>)}</div>}
        {tools && <div className="journal-options"><label>标签<input value={tag} placeholder="例如：科研" maxLength={16} onChange={e => setTag(e.target.value)}/></label><label>关联计划<select aria-label="关联计划" value={linked} onChange={e => setLinked(e.target.value)}><option value="">不关联</option>{Object.values(growth.nodes).map(n => <option value={n.id} key={n.id}>{n.title}</option>)}</select></label></div>}
        <div className="journal-composer-footer"><div><button type="button" onClick={() => input.current?.click()}><ImagePlus size={15}/>图片</button><button type="button" aria-expanded={tools} onClick={() => setTools(!tools)}><Tag size={15}/>标签</button><button type="button" aria-expanded={tools} onClick={() => setTools(!tools)}><Link2 size={15}/>关联计划</button><input hidden ref={input} type="file" accept="image/*" multiple aria-label="随笔图片" onChange={e => { setImages(old => [...old, ...Array.from(e.target.files ?? []).filter(f => f.type.startsWith('image/'))]); e.target.value = ''; }}/></div><button className="primary-button" disabled={!content.trim()}>发布</button></div>
      </form>}
    <p className="journal-storage-note">当前记录仅在本次会话保留，刷新前请另存重要内容。收起输入框不会清空文字。</p>
    <div className="journal-feed-header"><h2>最近 <small>{journals.length} 篇</small></h2><div className="filter-row">{filters.map(t => <button key={t} className={filter === t ? 'active' : ''} onClick={() => setFilter(t)}>{t}</button>)}</div></div>
    <div className="journal-reading-layout"><section className="journal-list" aria-label="随笔列表">
      {visible.length === 0 && <p className="empty-note">这里还没有记录，写下第一篇随笔吧。</p>}
      {visible.map(j => <button className={`journal-list-item ${current?.id === j.id ? 'selected' : ''} ${fresh.includes(j.id) ? 'is-new' : ''}`} key={j.id} onAnimationEnd={() => setFresh(old => old.filter(id => id !== j.id))} onClick={() => setSelected(j.id)} aria-pressed={current?.id === j.id}><time>{j.date.slice(5, 10).replace('-', '月')}日</time><div><h2>{j.content.split('\n')[0]}</h2><p>{j.content}</p><div className="journal-tags">{j.tags?.map(t => <span key={t}>{t}</span>)}<small>{Array.from(j.content).length} 字</small></div></div></button>)}
    </section>{/* `key` 换一次 = 换一个阅读对象时重挂一次,于是正文那 160ms 淡入重放。
        必须重挂:同一个元素上改内容不会重放 CSS 动画,而"换成另一篇"正是要看的
        那一下。左右滑动被明确禁止(规范 8),所以这里只有透明度。 */}
    <article className="journal-reader" key={current?.id ?? 'empty'} aria-label="随笔全文">
      {current ? <><time>{current.date}</time><h2>{current.content.split('\n')[0]}</h2><p className="journal-full-text">{current.content.includes('\n') ? current.content.slice(current.content.indexOf('\n')).trim() : current.content}</p><div className="journal-tags">{current.linkedNodeIds.filter(id => growth.nodes[id]).map(id => <button key={id} onClick={() => { enterSpace(id); router.push('/workbench'); }}>{growth.nodes[id].title}<ArrowUpRight size={12}/></button>)}</div>{files.filter(f => f.ownerId === current.id).map(f => <button className="attachment-link" onClick={() => setPreview(f)} key={f.id}><ImagePlus size={14}/>{f.name} · 查看</button>)}</> : <div className="journal-reader-empty"><PenLine size={28}/><h2>让想法有个落点</h2><p>在左侧选择随笔，在这里安静阅读。</p></div>}
    </article></div>{preview && <FilePreview asset={preview} onClose={() => setPreview(null)}/>}
  </div>;
}
