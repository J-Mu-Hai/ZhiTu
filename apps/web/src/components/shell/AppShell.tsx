'use client';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { Blocks, House, NotebookPen, MessagesSquare, UserRound, Sprout, ArrowUpRight, LoaderCircle } from 'lucide-react';
import { useEffect, type ReactNode } from 'react';
import { profileEntries } from '@/mock/life';
import { useAuth } from '@/features/auth/provider';
const links = [['/workbench', '工作台', Blocks], ['/today', '首页', House], ['/journal', '随笔', NotebookPen], ['/conversations', '对话', MessagesSquare], ['/me', '我的', UserRound]] as const;
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname(); const router = useRouter(); const { ready, user } = useAuth(); const isLogin = pathname === '/login';
  useEffect(() => {
    if (!ready) return;
    if (!user && !isLogin) router.replace('/login');
    if (user && isLogin) router.replace('/workbench');
  }, [isLogin, ready, router, user]);
  if (!ready || (!user && !isLogin)) return <div className="boot-screen"><Sprout size={24}/><LoaderCircle className="auth-spinner" size={20}/><span>正在打开你的成长空间…</span></div>;
  if (isLogin) return <main className="auth-main">{children}</main>;
  if (!user) return null;
  return <div className="app-shell"><header className="top-navigation">
    <Link href="/workbench" className="top-brand" aria-label="知途工作台"><Sprout size={19}/><strong>知途</strong><small>GROWTH SPACE</small></Link>
    <nav aria-label="主导航">{links.map(([href,label,Icon])=><Link key={href} href={href} className={pathname.startsWith(href)?'active':''}><Icon size={15}/><span>{label}</span></Link>)}</nav>
    <Link href="/workbench" className="top-goal">{user.targetYear} · {user.targetGoal}<ArrowUpRight size={12}/></Link>
    <Link href="/me" className="top-profile" aria-label={`${user.name}的个人档案`}><span>{user.name.slice(0, 1)}</span></Link>
  </header>{pathname.startsWith('/me')&&<nav className="top-profile-nav" aria-label="个人中心导航">{profileEntries.map(e=><Link key={e.slug} href={`/me/${e.slug}`} className={pathname===`/me/${e.slug}`?'active':''}>{e.title}</Link>)}</nav>}<main className="app-main">{children}</main></div>;
}
