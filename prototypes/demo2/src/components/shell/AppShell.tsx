'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Blocks, House, NotebookPen, MessagesSquare, UserRound, Sprout, ArrowUpRight } from 'lucide-react';
import type { ReactNode } from 'react';
import { profileEntries } from '@/mock/life';
const links = [['/workbench', '工作台', Blocks], ['/today', '首页', House], ['/journal', '随笔', NotebookPen], ['/conversations', '对话', MessagesSquare], ['/me', '我的', UserRound]] as const;
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return <div className="app-shell"><header className="top-navigation">
    <Link href="/workbench" className="top-brand" aria-label="知途工作台"><Sprout size={19}/><strong>知途</strong><small>DEMO 2</small></Link>
    <nav aria-label="主导航">{links.map(([href,label,Icon])=><Link key={href} href={href} className={pathname.startsWith(href)?'active':''}><Icon size={15}/><span>{label}</span></Link>)}</nav>
    <Link href="/workbench" className="top-goal">2027 · 保研<ArrowUpRight size={12}/></Link>
    <Link href="/me" className="top-profile" aria-label="同路人 · 个人档案"><UserRound size={15}/></Link>
  </header>{pathname.startsWith('/me')&&<nav className="top-profile-nav" aria-label="个人中心导航">{profileEntries.map(e=><Link key={e.slug} href={`/me/${e.slug}`} className={pathname===`/me/${e.slug}`?'active':''}>{e.title}</Link>)}</nav>}<main className="app-main">{children}</main></div>;
}
