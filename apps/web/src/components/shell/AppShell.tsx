'use client';
import { BrandMark } from '@/components/ui/BrandMark';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { Blocks, House, NotebookPen, MessagesSquare, UserRound, ArrowUpRight, LoaderCircle, Layers3 } from 'lucide-react';
import { useEffect, type ReactNode } from 'react';
import { profileEntries } from '@/features/profile/content';
import { useAuth } from '@/features/auth/provider';
const links = [['/spaces', '成长空间', Layers3], ['/workbench', '工作台', Blocks], ['/today', '首页', House], ['/journal', '随笔', NotebookPen], ['/conversations', '对话', MessagesSquare], ['/me', '我的', UserRound]] as const;
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname(); const router = useRouter(); const { ready, user, restoreProblem, retryRestore } = useAuth(); const isLogin = pathname === '/login';
  /*
   * `restoreProblem` 非空时**不跳登录页**。
   *
   * 那种状态的准确含义是"这台浏览器有令牌,但这一次没能拿回档案"(见
   * `features/auth/provider.tsx` 顶部那段)。跳到登录页等于告诉用户"你没登录",
   * 而事实是他的令牌还在、后端只是暂时够不着 —— 他照着登录页重新输一次密码,
   * 反而会多出一条会话。所以这里显示的是"连不上,重试"。
   */
  const blocked = !user && !isLogin && !!restoreProblem;
  useEffect(() => {
    if (!ready || blocked) return;
    if (!user && !isLogin) router.replace('/login');
    if (user && isLogin) router.replace('/spaces');
  }, [blocked, isLogin, ready, router, user]);
  /*
   * 标签页不可见 / 窗口失焦时,把环境光晕停下来(规范 3.1)。
   *
   * 判据用的是 `visibilitychange` 加 `blur`/`focus` 这两个**事件**,不是
   * `document.hasFocus()` 轮询:轮询要么白跑一个定时器,要么在"页面刚打开、
   * 焦点还没落下来"的那一瞬间误判成失焦 —— 而误判的表现是背景看起来不動,
   * 用户没有任何办法知道是自己错了。
   *
   * 写的是 `<html>` 上的一个类,不是 React state:这件事和渲染没有关系,
   * 而且动画的启停本来就不该经过 React。
   */
  useEffect(() => {
    const root = document.documentElement;
    const hide = () => root.classList.add('is-backgrounded');
    const show = () => root.classList.remove('is-backgrounded');
    const sync = () => (document.visibilityState === 'hidden' ? hide() : show());
    sync();
    document.addEventListener('visibilitychange', sync);
    window.addEventListener('blur', hide);
    window.addEventListener('focus', show);
    return () => {
      document.removeEventListener('visibilitychange', sync);
      window.removeEventListener('blur', hide);
      window.removeEventListener('focus', show);
      show();
    };
  }, []);
  if (!ready) return <div className="boot-screen"><BrandMark size={28}/><LoaderCircle className="auth-spinner" size={20}/><span>正在打开你的成长空间…</span></div>;
  if (blocked) return <div className="boot-screen is-blocked"><BrandMark size={28}/><strong>暂时连不上后端,你的登录状态还在。</strong><small className="boot-detail">{restoreProblem}</small><button className="primary-button" onClick={() => void retryRestore()}>重试</button></div>;
  if (!user && !isLogin) return <div className="boot-screen"><BrandMark size={28}/><LoaderCircle className="auth-spinner" size={20}/><span>正在打开你的成长空间…</span></div>;
  if (isLogin) return <main className="auth-main">{children}</main>;
  if (!user) return null;
  return <div className="app-shell"><header className="top-navigation">
    <Link href="/spaces" className="top-brand" aria-label="知途成长空间"><BrandMark size={28}/><strong>知途</strong><small>GROWTH SPACE</small></Link>
    <nav aria-label="主导航">{links.map(([href,label,Icon])=><Link key={href} href={href} className={pathname.startsWith(href)?'active':''}><Icon size={15}/><span>{label}</span></Link>)}</nav>
    {/* targetYear 未填写时是 0、targetGoal 是空串。以前直接渲染就变成"0 · " ——
        一个由两个空值拼出来的东西,看起来像数据,其实什么都不是。 */}
    <Link href="/workbench" className="top-goal">{user.targetGoal ? <>{user.targetYear ? `${user.targetYear} · ` : ''}{user.targetGoal}</> : '还没有设定目标'}<ArrowUpRight size={12}/></Link>
    <Link href="/me" className="top-profile" aria-label={`${user.name}的个人档案`}><span>{user.name.slice(0, 1)}</span></Link>
  </header>{pathname.startsWith('/me')&&<nav className="top-profile-nav" aria-label="个人中心导航">{profileEntries.map(e=><Link key={e.slug} href={`/me/${e.slug}`} className={pathname===`/me/${e.slug}`?'active':''}>{e.title}</Link>)}</nav>}<main className="app-main">{children}</main></div>;
}
