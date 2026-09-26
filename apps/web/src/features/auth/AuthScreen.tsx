'use client';
import { BrandMark } from '@/components/ui/BrandMark';

import { useEffect, useState, type FormEvent } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowRight, Flag, LoaderCircle } from 'lucide-react';
import { useAuth } from './provider';

export function AuthScreen() {
  const router = useRouter();
  const { ready, user, login, register } = useAuth();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  /**
   * 这里以前预填的是 `year: '大二'`、`targetYear: 2027`、`targetGoal: '保研'`。
   *
   * 那是演示数据的主人公,不是这个注册的人。预填的问题在于它**会真的被保存** ——
   * 用户点了注册,档案里就写上了"目标:保研、2027 年",而他从没说过这句话。
   * 和"新空间被灌入保研计划"是同一个错误,只是发生在注册表单里。
   */
  const [form, setForm] = useState({
    name: '', email: '', password: '', school: '', major: '', year: '', targetYear: 0, targetGoal: '',
  });

  useEffect(() => {
    // 落到空间页,不是工作台。工作台需要一个具体空间,而刚注册的账户一个都没有 ——
    // 以前这里会落到示例空间,于是新用户看到的是一份别人的保研计划。
    if (ready && user) router.replace('/spaces');
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
      else await register({ ...form, name: '知途用户' });
      router.replace('/spaces');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '暂时无法完成，请稍后重试。');
    } finally {
      setPending(false);
    }
  }

  return <div className="auth-page">
    <section className="auth-story" aria-label="知途产品介绍">
      <div className="auth-brand"><span><BrandMark size={36}/></span><div><strong>知途</strong><small>GROWTH SPACE</small></div></div>
      <div className="auth-story-copy"><span className="eyebrow">GROW AT YOUR OWN PACE</span><h1>从一个想法，<br/>走向更大的自己。</h1><p>每个人都有自己的成长节奏。把目标、路径、行动和记录留在属于你的空间里。</p></div>
      <div className="auth-plan-preview"><Flag size={22}/><div><small>你的专属计划</small><strong>登录后继续上一次的路径</strong></div><i/><i/><i/></div>
      {/* 这两句以前写的是"本地账户演示 · 数据只保存在这台浏览器"。
          账户、会话、对话现在都真的在后端了,那句话已经变成假的 ——
          而一句假的"我们没上传"比不写更糟。计划视图的编辑能力确实还没做完,
          所以第二句如实说还在开发中,而不是含糊带过。 */}
      <p className="auth-local-note">从一个目标开始，和 AI 一起把计划安排进每一天。</p>
    </section>
    <form className="auth-card" onSubmit={submit}>
      <header><span className="eyebrow">欢迎来到知途</span><h2>{mode === 'login' ? '继续你的旅程' : '开启你的成长旅程'}</h2><p>{mode === 'login' ? '回到你的计划，从今天可以完成的一步继续。' : '只需手机号和密码，其他资料之后再慢慢完善。'}</p></header>
      <label>{mode === 'register' ? '手机号码' : '手机号 / 邮箱'}<input aria-label={mode === 'register' ? '手机号码' : '手机号 / 邮箱'} required type={mode === 'register' ? 'tel' : 'text'} pattern={mode === 'register' ? '1[3-9][0-9]{9}' : undefined} maxLength={mode === 'register' ? 11 : 320} autoComplete="username" value={form.email} onChange={(event) => update('email', event.target.value)} placeholder={mode === 'register' ? '请输入 11 位手机号码' : '手机号或原有邮箱账号'}/></label>
      <label>密码<input aria-label="密码" required type="password" minLength={8} maxLength={128} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} value={form.password} onChange={(event) => update('password', event.target.value)} placeholder="至少 8 个字符"/></label>
      {error && <p role="alert" className="auth-error">{error}</p>}
      <button className="auth-submit" disabled={pending}>{pending ? <LoaderCircle className="auth-spinner" size={17}/> : <ArrowRight size={17}/>} {mode === 'login' ? '登录知途' : '创建账户'}</button>
      <button className="auth-switch" disabled={pending} type="button" onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(''); }}>{mode === 'login' ? '还没有账户？注册' : '已有账户？登录'}</button>
      <p className="auth-disclaimer">本地预览版 · 暂无短信验证，手机号仅作为登录账号。<br/>请记住密码，后端仅保存密码哈希。</p>
    </form>
  </div>;
}
