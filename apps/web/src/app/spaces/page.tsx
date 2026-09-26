'use client';

import { useCallback, useEffect, useState } from 'react';
import { AlertCircle, ArrowRight, Bot, CheckCircle2, FolderPlus, Layers3, ListTodo, Plus } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { Dialog } from '@/components/ui/Dialog';
import { useAuth } from '@/features/auth/provider';
import * as backend from '@/lib/backend';
import { ApiError } from '@/lib/api';

/**
 * 成长空间列表。
 *
 * ## 之前的"统计"是假的,这一版换成真的
 *
 * 上一版写的是 `spaces.length * 8` 当"计划任务数"、`spaces.length * 3 - 1` 当"已完成",
 * 而且卡片上直接标着"(演示)"。问题不在于数字不对,在于**这种数字会让用户对系统失去
 * 信任**:他明明只建了 3 个任务,界面说 24 个;他要做的第一个判断("我这个空间里
 * 到底有多少事")被一句假话回答了。
 *
 * 现在的数字来自 `GET /api/workspaces/{id}` 的 `counts` —— 后端直接数行数。
 * 空间多的时候会有 N 次请求,但对一个 MVP 的个人空间数量来说这完全不是瓶颈;
 * 真正不能接受的是显示一个编出来的数。
 */
export default function SpacesPage() {
  const { user } = useAuth();
  const router = useRouter();

  const [spaces, setSpaces] = useState<backend.WorkspaceSummary[]>([]);
  const [counts, setCounts] = useState<Record<string, backend.WorkspaceCounts>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [intent, setIntent] = useState('');
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await backend.listWorkspaces();
      setSpaces(list);
      const details = await Promise.all(
        list.map((space) =>
          backend
            .getWorkspace(space.id)
            .then((detail) => [space.id, detail.counts] as const)
            // 单个空间的详情拿不到不该让整页变成错误页 —— 列表本身是好的。
            .catch(() => null),
        ),
      );
      setCounts(Object.fromEntries(details.filter((item) => item !== null)));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '加载成长空间失败。');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (user) void load();
  }, [user, load]);

  async function create() {
    if (!title.trim() || creating) return;
    setCreating(true);
    setError(null);
    try {
      const created = await backend.createWorkspace({ title, intent });
      setOpen(false);
      setTitle('');
      setIntent('');
      // 直接进新空间 —— 用户在这里的意图就是"建一个然后开始聊"。
      router.push(`/workbench?workspace=${encodeURIComponent(created.workspace.id)}`);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '创建失败。');
      setCreating(false);
    }
  }

  if (!user) return null;

  const totalNodes = Object.values(counts).reduce((sum, item) => sum + item.nodes, 0);
  const totalNodesKnown = Object.keys(counts).length === spaces.length;

  return (
    <section className="spaces-page">
      <header className="spaces-hero">
        <div>
          <span className="eyebrow">MY GROWTH SPACES</span>
          <h1>选择一个成长空间</h1>
          <p>每个空间拥有独立的路径、时间线、任务和 AI 对话。</p>
          {/* 示例空间从"没选空间时的默认"改成"主动进去看"。里面的数据是本地写好的,
              所以入口上就写着"示例",进去之后界面上也一直标着。 */}
          <button
            type="button"
            className="text-button"
            onClick={() => router.push('/workbench?workspace=primary')}
          >
            先看看示例空间（本地预置数据，不经过模型）
          </button>
        </div>
        <button className="primary-button" onClick={() => setOpen(true)}>
          <Plus size={16} />创建成长空间
        </button>
      </header>

      {error && (
        <p className="spaces-error" role="alert">
          <AlertCircle size={14} /> {error}
          <button type="button" className="text-button" onClick={() => void load()}>
            重试
          </button>
        </p>
      )}

      <div className="space-stats">
        <div>
          <Layers3 size={17} />
          <strong>{spaces.length}</strong>
          <span>成长空间</span>
        </div>
        <div>
          <ListTodo size={17} />
          <strong>{loading || !totalNodesKnown ? '—' : totalNodes}</strong>
          <span>计划节点</span>
        </div>
        <div>
          <CheckCircle2 size={17} />
          <strong>{loading ? '—' : Object.values(counts).reduce((sum, item) => sum + item.conversations, 0)}</strong>
          <span>对话</span>
        </div>
      </div>

      <div className="spaces-grid">
        {spaces.map((space, index) => (
          <article className="space-card" key={space.id}>
            <div className="space-card-icon">{index === 1 ? <Bot size={19} /> : <Layers3 size={19} />}</div>
            <span className="eyebrow">GROWTH SPACE</span>
            <h2>{space.title}</h2>
            <p>{space.intent}</p>
            <div className="space-card-meta">
              <span>路径 · 时间线 · 任务</span>
              <span>
                {counts[space.id] ? `${counts[space.id].nodes} 个节点` : '…'}
              </span>
            </div>
            <button onClick={() => router.push(`/workbench?workspace=${encodeURIComponent(space.id)}`)}>
              进入工作台 <ArrowRight size={15} />
            </button>
          </article>
        ))}

        {/* 一个空间都没有时,给的是"建一个"的引导,而不是替他编一个默认空间。 */}
        <button className="new-space-card" onClick={() => setOpen(true)}>
          <FolderPlus size={22} />
          <strong>创建新的成长空间</strong>
          <span>{spaces.length ? '从一个目标或现实问题开始' : '还没有空间 —— 从一个目标开始'}</span>
        </button>
      </div>

      {open && (
        <Dialog title="创建成长空间" onClose={() => setOpen(false)}>
          <form
            className="node-form"
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
          >
            <label>
              空间名称
              <input
                autoFocus
                value={title}
                maxLength={60}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="例如：2027 保研计划"
              />
            </label>
            <label>
              想在这里推进什么？
              <textarea
                value={intent}
                maxLength={240}
                onChange={(event) => setIntent(event.target.value)}
                placeholder="例如：系统准备科研、课程和夏令营"
              />
            </label>
            <p>创建后可在工作台中与 AI 一起建立路径、排入时间线并确认调整。</p>
            <button className="primary-button" disabled={!title.trim() || creating}>
              {creating ? '正在创建…' : '创建并进入'}
            </button>
          </form>
        </Dialog>
      )}
    </section>
  );
}
