'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowUpRight, Check, ChevronRight, LogOut, Pencil, Sparkles, X } from 'lucide-react';
import { useEffect, useState, type FormEvent } from 'react';
import { useAuth } from '@/features/auth/provider';
import type { AccountProfile, EditableProfile } from '@/features/auth/types';
import { useDemo } from '@/features/growth/provider';
import { profileEntries } from '@/mock/life';

/**
 * 缺的字段整段换掉,不留空位。
 *
 * 后端把没填的档案存成 NULL,归一化之后是空串 / `0`(见 `auth/types.ts`,那里的
 * 意图是"空白是诚实的")。但空白**直接拼进句子里**并不诚实,只是看起来坏了:
 * `{school} · {major}专业 · {year}` 会剩下"· 专业 ·",`「{targetGoal}」` 会剩下"「」",
 * `{targetYear}` 会剩下一个孤零零的 `0`。所以判断放在**句子**这一层。
 */
function filled(parts: (string | number | false)[]): string {
  return parts.filter((part) => part && String(part).trim()).join(' · ');
}

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
        {/*
          `value` 用 `|| ''` 而不是直接给 `draft.targetYear`。

          后端把没填的年份存成 NULL,归一化之后是 `0`(见 `auth/types.ts`,以及
          `Profile.tsx` 下面那句 `user.targetYear > 0` —— 这一版里 `0` 就是"没填")。
          直接渲染的话,一个从没填过目标年份的账户打开编辑表单,会看到输入框里坐着
          一个他从没打过的 `0`,而 `min={2026}` 让这个 0 立刻变成"不合法":
          点保存毫无反应,浏览器把焦点跳到这个框上,提示"值必须大于或等于 2026"。

          注册表单(`AuthScreen.tsx`)对同一个 0 用的就是 `|| ''`。两个表单显示同一份
          数据,不该一个显示空白、另一个显示一个用户没输入过的数字。
        */}
        <label>目标年份<input aria-label="目标年份" required type="number" min={2026} max={2040} value={draft.targetYear || ''} onChange={(event) => update('targetYear', Number(event.target.value))}/></label>
        <label className="profile-bio-field">个人介绍<textarea aria-label="个人介绍" maxLength={180} value={draft.bio} onChange={(event) => update('bio', event.target.value)} placeholder="写下你现在最在意的方向。"/></label>
      </div>
      <footer><p>修改目标方向后，工作台中心计划会同步更新。</p><div><button type="button" className="profile-cancel" onClick={cancel}>取消</button><button className="primary-button">保存个人资料</button></div></footer>
    </form>}

    <div className="profile-facts">
      <div><span>现在的你</span><strong>{filled([user.school, user.major && `${user.major}专业`, user.year]) || '还没有填写学校和专业'}</strong><small>专业排名 {user.rank || '未填写'}</small></div>
      <div><span>正在前往</span><strong>{user.targetGoal || '还没有设定目标'}{user.targetYear > 0 && <small>{user.targetYear}</small>}</strong><small>这是属于 {user.name} 的独立计划</small></div>
      <div><span>账户</span><strong>{user.email}</strong><small>后端账户 · 计划按空间隔离保存</small></div>
    </div>

    <section className="portrait"><div className="section-label"><span>我眼中的你</span><span><Sparkles size={13}/> AI</span></div><p>{user.targetGoal
      ? <>你正在为「{user.targetGoal}」建立自己的路径，<br/>也在学习根据真实行动调整节奏。</>
      : <>你还没有写下要去的方向。<br/>新建一个成长空间，把它说清楚，路径会从那里长出来。</>}</p><div className="portrait-trends"><span>执行稳定性 <b>↑</b></span><span>主动探索 <b>↑</b></span><span>时间焦虑 <b>↓</b></span></div><Link href="/me/profile" className="text-button">查看完整用户画像<ArrowUpRight size={14}/></Link></section>
    <div className="profile-links">{profileEntries.map((entry, index) => <Link key={entry.slug} href={`/me/${entry.slug}`}><span className="entry-number">0{index + 1}</span><div><strong>{entry.title}</strong><small>{entry.detail}</small></div><ChevronRight size={16}/></Link>)}</div>
    <p className="subtle-disclaimer">账户、资料和计划保存在后端数据库，换一台机器登录也在。画像内容仍是演示信息，成长的定义始终由你决定。</p>
  </div>;
}
