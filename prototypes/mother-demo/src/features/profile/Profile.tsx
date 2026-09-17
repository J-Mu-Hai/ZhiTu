'use client';
import Link from 'next/link';
import { ArrowUpRight, ChevronRight, Sparkles } from 'lucide-react';
import { user } from '@/mock/growth-state';
import { profileEntries } from '@/mock/life';
export function Profile() {
  return <div className="editorial-page profile-page"><header className="profile-header"><span className="profile-avatar">同</span><div><span className="eyebrow">A LITTLE MORE LIKE YOURSELF</span><h1>你好，{user.name}。</h1><p>AI 已经陪你成长 47 天。</p></div></header><div className="profile-facts"><div><span>现在的你</span><strong>{user.major}专业 · {user.year}</strong></div><div><span>正在前往</span><strong>保研 · AI 方向 <small>2027</small></strong></div></div><section className="portrait"><div className="section-label"><span>我眼中的你</span><span><Sparkles size={13}/> AI</span></div><p>你是一个目标感正在逐渐形成，<br/>但容易因为同时关注过多方向<br/>而产生行动分散的人。</p><div className="portrait-trends"><span>执行稳定性 <b>↑</b></span><span>科研主动性 <b>↑</b></span><span>时间焦虑 <b>↓</b></span></div><Link href="/me/profile" className="text-button">查看完整用户画像<ArrowUpRight size={14}/></Link></section><div className="profile-links">{profileEntries.map((entry,i)=><Link key={entry.slug} href={`/me/${entry.slug}`}><span className="entry-number">0{i+1}</span><div><strong>{entry.title}</strong><small>{entry.detail}</small></div><ChevronRight size={16}/></Link>)}</div><p className="subtle-disclaimer">画像与陪伴天数为演示内容。成长的定义，始终由你决定。</p></div>;
}
