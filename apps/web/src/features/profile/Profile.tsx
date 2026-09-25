'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowUpRight, Check, ChevronRight, LogOut, Pencil, Sparkles, X } from 'lucide-react';
import { useEffect, useState, type FormEvent } from 'react';
import { useAuth } from '@/features/auth/provider';
import type { AccountProfile, EditableProfile } from '@/features/auth/types';
import { useDemo } from '@/features/growth/provider';
import { profileEntries } from '@/mock/life';

function editableProfile(user: AccountProfile | null): EditableProfile {
  return {
    name: user?.name ?? '',
    school: user?.school ?? '',
    major: user?.major ?? '',
    year: user?.year ?? '大一',
    rank: user?.rank ?? 0,
    targetYear: user?.targetYear ?? 2027,
    targetGoal: user?.targetGoal ?? '',
    bio: user?.bio ?? '',
  };
}

export function Profile() {
  const router = useRouter();
  const { user, updateProfile, logout } = useAuth();
  const { updatePlanMeta } = useDemo();
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState(false);
  const [draft, setDraft] = useState<EditableProfile>(() => editableProfile(user));

  useEffect(() => setDraft(editableProfile(user)), [user]);
  if (!user) return null;

  function update<K extends keyof EditableProfile>(key: K, value: EditableProfile[K]) {
    setDraft((old) => ({ ...old, [key]: value }));
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    updateProfile(draft);
    updatePlanMeta(draft.targetGoal, draft.targetYear, draft.school, draft.major);
    setEditing(false);
    setSaved(true);
  }

  function cancel() {
    setDraft(editableProfile(user));
    setEditing(false);
  }

  function signOut() {
    logout();
    router.replace('/login');
  }

  return <div className="editorial-page profile-page">
    <header className="profile-header">
      <span className="profile-avatar">{user.name.slice(0, 1)}</span>
      <div><span className="eyebrow">A LITTLE MORE LIKE YOURSELF</span><h1>你好，{user.name}。</h1><p>{user.bio}</p></div>
      <div className="profile-header-actions">
        <button className="profile-action" onClick={() => { setEditing(true); setSaved(false); }}><Pencil size={14}/>编辑个人资料</button>
        <button className="profile-action profile-logout" onClick={signOut}><LogOut size={14}/>退出当前账户</button>
      </div>
    </header>

    {saved && <div className="profile-saved" role="status"><Check size={14}/>个人资料和计划标题已经更新。</div>}

    {editing && <form className="profile-edit-form" onSubmit={save}>
      <header><div><span className="eyebrow">PERSONAL PROFILE</span><h2>编辑你的个人资料</h2></div><button className="icon-button" type="button" aria-label="取消编辑" onClick={cancel}><X size={17}/></button></header>
      <div className="profile-form-grid">
        <label>姓名<input aria-label="姓名" required maxLength={50} value={draft.name} onChange={(event) => update('name', event.target.value)}/></label>
        <label>邮箱<input aria-label="邮箱" value={user.email} disabled/></label>
        <label>学校<input aria-label="学校" required maxLength={80} value={draft.school} onChange={(event) => update('school', event.target.value)}/></label>
        <label>专业<input aria-label="专业" required maxLength={80} value={draft.major} onChange={(event) => update('major', event.target.value)}/></label>
        <label>年级<select aria-label="年级" value={draft.year} onChange={(event) => update('year', event.target.value)}>{['大一','大二','大三','大四','研究生'].map((year) => <option key={year}>{year}</option>)}</select></label>
        <label>专业排名<input aria-label="专业排名" type="number" min={0} max={9999} value={draft.rank} onChange={(event) => update('rank', Number(event.target.value))}/></label>
        <label>目标方向<input aria-label="目标方向" required maxLength={50} value={draft.targetGoal} onChange={(event) => update('targetGoal', event.target.value)}/></label>
        <label>目标年份<input aria-label="目标年份" required type="number" min={2026} max={2040} value={draft.targetYear} onChange={(event) => update('targetYear', Number(event.target.value))}/></label>
        <label className="profile-bio-field">个人介绍<textarea aria-label="个人介绍" maxLength={180} value={draft.bio} onChange={(event) => update('bio', event.target.value)} placeholder="写下你现在最在意的方向。"/></label>
      </div>
      <footer><p>修改目标方向后，工作台中心计划会同步更新。</p><div><button type="button" className="profile-cancel" onClick={cancel}>取消</button><button className="primary-button">保存个人资料</button></div></footer>
    </form>}

    <div className="profile-facts">
      <div><span>现在的你</span><strong>{user.school} · {user.major}专业 · {user.year}</strong><small>专业排名 {user.rank || '未填写'}</small></div>
      <div><span>正在前往</span><strong>{user.targetGoal}<small>{user.targetYear}</small></strong><small>这是属于 {user.name} 的独立计划</small></div>
      <div><span>账户</span><strong>{user.email}</strong><small>本地账户 · 计划已隔离保存</small></div>
    </div>

    <section className="portrait"><div className="section-label"><span>我眼中的你</span><span><Sparkles size={13}/> AI</span></div><p>你正在为「{user.targetGoal}」建立自己的路径，<br/>也在学习根据真实行动调整节奏。</p><div className="portrait-trends"><span>执行稳定性 <b>↑</b></span><span>主动探索 <b>↑</b></span><span>时间焦虑 <b>↓</b></span></div><Link href="/me/profile" className="text-button">查看完整用户画像<ArrowUpRight size={14}/></Link></section>
    <div className="profile-links">{profileEntries.map((entry, index) => <Link key={entry.slug} href={`/me/${entry.slug}`}><span className="entry-number">0{index + 1}</span><div><strong>{entry.title}</strong><small>{entry.detail}</small></div><ChevronRight size={16}/></Link>)}</div>
    <p className="subtle-disclaimer">账户、资料和计划当前保存在本机浏览器。画像内容仍是演示信息，成长的定义始终由你决定。</p>
  </div>;
}
