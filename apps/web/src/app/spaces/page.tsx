'use client';

import { useCallback, useEffect, useState } from 'react';
import { AlertCircle, Archive, ArrowRight, Bot, CheckCircle2, FolderPlus, Layers3, ListTodo, Plus, RotateCcw, Trash2 } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { Dialog } from '@/components/ui/Dialog';
import { AmbientGlow } from '@/components/ui/AmbientGlow';
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
  const [archived, setArchived] = useState<backend.WorkspaceSummary[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  /** 等着确认归档的那一个。 */
  const [pendingDelete, setPendingDelete] = useState<backend.WorkspaceSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [restoring, setRestoring] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // **不要等详情**。列表接口返回的 `WorkspaceSummary` 不带 status/archivedAt,
      // 但它默认就只给活动空间;再拿一次 `includeArchived=true`,两次的差集就是
      // 已归档的那些 —— 不需要为这一点去逐条读详情(那样会把整个列表拖在
      // `GET /workspaces/{id}` 后面,列表已经不依赖它了)。
      // 详情的唯一用处仍是 `counts`,它在卡片已经画出来之后才补上。
      const [active, all] = await Promise.all([
        backend.listWorkspaces(false),
        backend.listWorkspaces(true),
      ]);
      const activeIds = new Set(active.map((space) => space.id));
      setSpaces(active);
      setArchived(all.filter((space) => !activeIds.has(space.id)));
      const details = await Promise.all(
        active.map((space) =>
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

  /**
   * 「删除空间」= **可恢复归档**,与节点那条一模一样。
   *
   * 后端没有 `DELETE`(空间级联删除会连整棵计划树、全部历史版本与执行记录一起抹掉,
   * 而且不可恢复 —— 见 `api/routes/workspaces.py` 开头那段)。走的是
   * `PATCH status=archived`:空间从活动列表里消失,但可以在「已归档的空间」里原样恢复。
   * 所以按钮上写的也是「删除(可恢复)」,而不是一个会骗人的「彻底删除」。
   */
  async function archiveSpace() {
    if (!pendingDelete || deleting) return;
    setDeleting(true);
    setError(null);
    try {
      await backend.updateWorkspace(pendingDelete.id, { status: 'archived' });
      setPendingDelete(null);
      await load();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '归档失败,请重试。');
    } finally {
      setDeleting(false);
    }
  }

  async function restoreSpace(id: string) {
    if (restoring) return;
    setRestoring(id);
    setError(null);
    try {
      await backend.updateWorkspace(id, { status: 'active' });
      await load();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '恢复失败,请重试。');
    } finally {
      setRestoring(null);
    }
  }

  if (!user) return null;

  const totalNodes = Object.values(counts).reduce((sum, item) => sum + item.nodes, 0);
  const totalNodesKnown = Object.keys(counts).length === spaces.length;

  return (
    <section className="spaces-page">
      <AmbientGlow />
      <header className="spaces-hero">
        <div>
          <span className="eyebrow">MY GROWTH SPACES</span>
          <h1>选择一个成长空间</h1>
          <p>每个空间拥有独立的路径、时间线、任务和 AI 对话。</p>
          {/* 这里原来还有一个「先看看示例空间」的入口,指向 `?workspace=primary`。
              示例空间已经整个删掉了,那个 id 现在打不开任何东西 —— 留着它,用户点
              下去会看到一句"打不开这个成长空间",而他并没有做错任何事。 */}
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
            {/* 删除(可恢复)。绝对定位在右上角 —— 不挤走下面那行「进入工作台」。 */}
            <button
              type="button"
              className="space-card-delete"
              aria-label={`删除${space.title}(可恢复)`}
              title="删除(可恢复):移到「已归档的空间」,随时能恢复"
              onClick={() => setPendingDelete(space)}
            >
              <Trash2 size={14} />
            </button>
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

      {/* 已归档的空间。**恢复的入口必须在这里** —— 没有它,「可恢复」就是一句空话:
          用户删掉之后找不到任何地方能把空间拿回来。 */}
      {archived.length > 0 && (
        <section className="archived-spaces">
          <button type="button" className="text-button" onClick={() => setShowArchived((value) => !value)}>
            <Archive size={13} />
            已归档的空间（{archived.length}）{showArchived ? '收起' : '展开'}
          </button>
          {showArchived && (
            <ul>
              {archived.map((space) => (
                <li key={space.id}>
                  <div>
                    <strong>{space.title}</strong>
                    <small>已归档,可恢复</small>
                  </div>
                  <button
                    type="button"
                    disabled={restoring !== null}
                    onClick={() => void restoreSpace(space.id)}
                  >
                    <RotateCcw size={13} />{restoring === space.id ? '恢复中…' : '恢复'}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {pendingDelete && (
        <Dialog title={`归档「${pendingDelete.title}」？`} onClose={() => setPendingDelete(null)}>
          <div className="archive-panel">
            <p className="archive-hint">
              归档之后这个空间会从列表里消失，但<b>可以</b>在「已归档的空间」里恢复。
              空间里的路径、时间线、任务与对话都会保留。
            </p>
            {error && <p className="form-error" role="alert">{error}</p>}
            <div className="archive-actions">
              <button type="button" onClick={() => setPendingDelete(null)}>取消</button>
              <button className="primary-button" type="button" disabled={deleting} onClick={() => void archiveSpace()}>
                {deleting ? '归档中…' : '归档'}
              </button>
            </div>
          </div>
        </Dialog>
      )}

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
              />
            </label>
            <label>
              想在这里推进什么？
              <textarea
                value={intent}
                maxLength={240}
                onChange={(event) => setIntent(event.target.value)}
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
