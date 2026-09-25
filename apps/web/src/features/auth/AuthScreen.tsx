'use client';

import { useEffect, useState, type FormEvent } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowRight, Flag, LoaderCircle, Sprout } from 'lucide-react';
import { useAuth } from './provider';

export function AuthScreen() {
  const router = useRouter();
  const { ready, user, login, register } = useAuth();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [form, setForm] = useState({
    name: '', email: '', password: '', school: '', major: '', year: '大二', targetYear: 2027, targetGoal: '保研',
  });

  useEffect(() => {
    if (ready && user) router.replace('/workbench');
  }, [ready, router, user]);

  function update(key: keyof typeof form, value: string | number) {
    setForm((old) => ({ ...old, [key]: value }));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError('');
    try {
      if (mode === 'login') await login(form.email, form.password);
      else await register(form);
      router.replace('/workbench');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '暂时无法完成，请稍后重试。');
    } finally {
      setPending(false);
    }
  }

  return <div className="auth-page">
    <section className="auth-story" aria-label="知途产品介绍">
      <div className="auth-brand"><span><Sprout size={23}/></span><div><strong>知途</strong><small>GROWTH SPACE</small></div></div>
      <div className="auth-story-copy"><span className="eyebrow">GROW AT YOUR OWN PACE</span><h1>从一个想法，<br/>走向更大的自己。</h1><p>每个人都有自己的成长节奏。把目标、路径、行动和记录留在属于你的空间里。</p></div>
      <div className="auth-plan-preview"><Flag size={22}/><div><small>你的专属计划</small><strong>登录后继续上一次的路径</strong></div><i/><i/><i/></div>
      <p className="auth-local-note">当前为本地账户演示 · 数据只保存在这台浏览器</p>
    </section>
    <form className="auth-card" onSubmit={submit}>
      <header><span className="eyebrow">欢迎来到知途</span><h2>{mode === 'login' ? '继续你的旅程' : '开启你的成长旅程'}</h2><p>{mode === 'login' ? '回到你的计划，从今天可以完成的一步继续。' : '先告诉我们你是谁，知途会为这个账户建立独立计划。'}</p></header>
      {mode === 'register' && <div className="auth-register-grid">
        <label>怎么称呼你<input aria-label="怎么称呼你" required maxLength={50} autoComplete="name" value={form.name} onChange={(event) => update('name', event.target.value)} placeholder="你的名字"/></label>
        <label>学校<input aria-label="学校" required maxLength={80} value={form.school} onChange={(event) => update('school', event.target.value)} placeholder="所在学校"/></label>
        <label>专业<input aria-label="专业" required maxLength={80} value={form.major} onChange={(event) => update('major', event.target.value)} placeholder="所学专业"/></label>
        <label>年级<select aria-label="年级" value={form.year} onChange={(event) => update('year', event.target.value)}>{['大一','大二','大三','大四','研究生'].map((year) => <option key={year}>{year}</option>)}</select></label>
        <label>目标方向<input aria-label="目标方向" required maxLength={50} value={form.targetGoal} onChange={(event) => update('targetGoal', event.target.value)} placeholder="例如：保研 / 就业 / 留学"/></label>
        <label>目标年份<input aria-label="目标年份" required type="number" min={2026} max={2040} value={form.targetYear} onChange={(event) => update('targetYear', Number(event.target.value))}/></label>
      </div>}
      <label>邮箱<input aria-label="邮箱" required type="email" autoComplete="email" value={form.email} onChange={(event) => update('email', event.target.value)} placeholder="you@example.com"/></label>
      <label>密码<input aria-label="密码" required type="password" minLength={8} maxLength={128} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} value={form.password} onChange={(event) => update('password', event.target.value)} placeholder="至少 8 个字符"/></label>
      {error && <p role="alert" className="auth-error">{error}</p>}
      <button className="auth-submit" disabled={pending}>{pending ? <LoaderCircle className="auth-spinner" size={17}/> : <ArrowRight size={17}/>} {mode === 'login' ? '登录知途' : '创建账户与计划'}</button>
      <button className="auth-switch" type="button" onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(''); }}>{mode === 'login' ? '还没有账户？注册' : '已有账户？登录'}</button>
      <p className="auth-disclaimer">本地演示不会上传账户和计划。正式上线时由后端身份服务接管。</p>
    </form>
  </div>;
}
