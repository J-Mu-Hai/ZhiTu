'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { ArrowUpRight, Blocks, House, NotebookPen, MessagesSquare, UserRound, Sprout, ChevronDown } from 'lucide-react';
import type { ReactNode } from 'react';
import { profileEntries } from '@/mock/life';
const links = [ ['/workbench', '工作台', Blocks], ['/today', '首页', House], ['/journal', '随笔', NotebookPen], ['/conversations', '对话', MessagesSquare], ['/me', '我的', UserRound] ] as const;
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return <div className="app-shell"><aside className="sidebar">
    <Link href="/workbench" className="brand"><span className="brand-symbol"><Sprout size={24}/></span><span>知途<small>MAKE ROOM TO GROW</small></span></Link>
    <div className="nav-caption">我的空间 <span>PERSONAL</span></div>
    <nav aria-label="主导航">{links.map(([href, label, Icon]) => <Link key={href} href={href} className={`nav-link ${pathname.startsWith(href) ? 'active' : ''}`}><Icon size={18}/><span>{label}</span>{href === '/workbench' ? <span className="nav-count">01</span> : href === '/me' ? <ChevronDown size={13}/> : null}</Link>)}</nav>
    {pathname.startsWith('/me')&&<div className="profile-subnav">{profileEntries.map(e=><Link key={e.slug} href={`/me/${e.slug}`} className={pathname===`/me/${e.slug}`?'active':''}>{e.title}</Link>)}</div>}
    <div className="sidebar-goal"><span className="eyebrow">正在前往</span><Link href="/workbench"><span className="tiny-dot"/>2027 · 保研<ArrowUpRight size={14}/></Link><p>一步一步，走向更大的世界。</p></div>
    <div className="sidebar-bottom"><p>更大的自己，<br/>正在路上。</p><div className="profile-mini"><span className="avatar">同</span><div>同路人<small>人工智能 · 大二</small></div><span className="online-dot"/></div><div className="demo-tag">EXPERIENCE DEMO <span>V0.1</span></div></div>
  </aside><main className="app-main">{children}</main></div>;
}
