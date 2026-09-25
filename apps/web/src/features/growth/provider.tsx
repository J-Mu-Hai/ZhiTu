'use client';
import { createContext, useContext, useEffect, useRef, useReducer, useState, type ReactNode } from 'react';
import { DEMO_TODAY, initialGrowth } from '@/mock/growth-state';
import { initialConversation } from '@/mock/conversations';
import type { AISettings, Conversation, FileAsset, GrowthNode, GrowthState, JournalEntry, Message, PlanAction, Proposal } from '@/types/growth';
import { historyConversations, initialJournals } from '@/mock/life';
import { collectNodeBranch, growthReducer, daysBetween, shiftDate } from './reducer';
import { useAuth } from '@/features/auth/provider';
import type { AccountProfile } from '@/features/auth/types';

type WorkspaceSnapshot = {
  growth: GrowthState;
  journals: JournalEntry[];
  conversations: Conversation[];
  settings: AISettings;
  messages: Message[];
  proposals: Proposal[];
  positions: Record<string, { x: number; y: number }>;
};

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const workspaceKey = (userId: string) => `zhitu.workspace.${userId}.v1`;
const exampleMessageIds = new Set(['m1', 'm2', 'm3', 'm4', 't1', 't2', 'c1', 'c2', 'f1', 'f2']);
const exampleConversationIds = new Set(['transformer', 'career', 'future']);
const exampleJournalIds = new Set(['journal-1', 'journal-2']);

function getBeijingDate(now = new Date()): string {
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now);
  const value = (type: Intl.DateTimeFormatPartTypes) => parts.find(part => part.type === type)?.value ?? '';
  return `${value('year')}-${value('month')}-${value('day')}`;
}

function restoreExampleMarkers(snapshot: WorkspaceSnapshot): WorkspaceSnapshot {
  const markMessage = (message: Message): Message => exampleMessageIds.has(message.id) ? { ...message, isExample: true } : message;
  const markedConversations = snapshot.conversations.map(conversation => ({
    ...conversation,
    isExample: exampleConversationIds.has(conversation.id) || conversation.isExample,
    messages: conversation.messages.map(markMessage),
  }));
  const conversations = markedConversations.some(conversation => !conversation.isExample)
    ? markedConversations.filter(conversation => !conversation.isExample)
    : markedConversations;
  return {
    ...snapshot,
    messages: snapshot.messages.map(markMessage),
    conversations,
    journals: snapshot.journals.map(journal => {
      if (exampleJournalIds.has(journal.id)) return { ...journal, isExample: true };
      if (!journal.isExample && journal.date === DEMO_TODAY) return { ...journal, date: getBeijingDate() };
      return journal;
    }),
  };
}

function freshWorkspace(user: AccountProfile | null): WorkspaceSnapshot {
  const growth = clone(initialGrowth);
  if (user) {
    growth.id = `growth-${user.id}`;
    growth.title = `${user.targetGoal}计划`;
    growth.nodes.goal = {
      ...growth.nodes.goal,
      title: user.targetGoal,
      description: `${user.targetYear} · ${user.school} · ${user.major}`,
    };
  }
  return {
    growth,
    journals: clone(initialJournals),
    conversations: clone(historyConversations),
    settings: { mode: '教练', frequency: '中', proactive: true, adjust: true, critique: true, rest: true },
    messages: clone(initialConversation.messages),
    proposals: [],
    positions: {},
  };
}

function loadWorkspace(user: AccountProfile | null): WorkspaceSnapshot {
  const fallback = freshWorkspace(user);
  if (!user || typeof window === 'undefined') return fallback;
  try {
    const stored = localStorage.getItem(workspaceKey(user.id));
    if (!stored) return fallback;
    const parsed = JSON.parse(stored) as Partial<WorkspaceSnapshot>;
    if (!parsed.growth?.nodes?.goal) return fallback;
    return restoreExampleMarkers({
      growth: parsed.growth,
      journals: parsed.journals ?? fallback.journals,
      conversations: parsed.conversations ?? fallback.conversations,
      settings: parsed.settings ?? fallback.settings,
      messages: parsed.messages ?? fallback.messages,
      proposals: parsed.proposals ?? [],
      positions: parsed.positions ?? {},
    });
  } catch {
    return fallback;
  }
}

function useDemoState(user: AccountProfile | null) {
  const [seed] = useState(() => loadWorkspace(user));
  const [growth, dispatch] = useReducer(growthReducer, seed.growth);
  const [spaceId, setSpaceId] = useState('goal');
  const [files, setFiles] = useState<FileAsset[]>([]);
  const objectUrls = useRef(new Set<string>());
  useEffect(() => () => { objectUrls.current.forEach(url => URL.revokeObjectURL(url)); }, []);
  const [journals, setJournals] = useState<JournalEntry[]>(seed.journals);
  const [conversations, setConversations] = useState(seed.conversations);
  const [settings, setSettings] = useState<AISettings>(seed.settings);
  const [focus, setFocus] = useState({ nodeId: 'attention', seconds: 0, running: false });
  useEffect(() => { if (!focus.running) return; const timer = setInterval(() => setFocus(f => ({ ...f, seconds: f.seconds + 1 })), 1000); return () => clearInterval(timer); }, [focus.running]);
  const [selectedId, select] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>(seed.messages);
  const [proposals, setProposals] = useState<Proposal[]>(seed.proposals);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>(seed.positions);
  const [impact, setImpact] = useState(false);
  const [previewProposalId, setPreviewProposalId] = useState<string | null>(null);
  useEffect(() => {
    if (!user) return;
    const snapshot: WorkspaceSnapshot = { growth, journals, conversations, settings, messages, proposals, positions };
    localStorage.setItem(workspaceKey(user.id), JSON.stringify(snapshot));
  }, [conversations, growth, journals, messages, positions, proposals, settings, user]);
  function previewProposal(id: string) {
    const proposal = proposals.find(p => p.id === id && p.status === 'pending');
    if (!proposal) return;
    setSpaceId('goal'); select(proposal.nodeId); setPreviewProposalId(id); setImpact(true);
  }
  function enterSpace(id: string) { if (!growth.nodes[id]) return; setSpaceId(id); select(id); }
  function updatePlanMeta(title: string, targetYear: number, school: string, major: string) {
    dispatch({ type: 'UPDATE_PLAN_META', title, description: `${targetYear} · ${school} · ${major}` });
  }
  function addNode(title: string, type: GrowthNode['type'] = 'task', description = '') {
    if (!title.trim()) return;
    const parent = growth.nodes[spaceId]; const id = crypto.randomUUID();
    dispatch({ type: 'CREATE_NODE', node: { id, title: title.trim(), description: description.trim() || undefined, type, parentId: spaceId, category: parent.category ?? 'personal', stageId: growth.currentStageId, status: 'pending', priority: 'medium', startDate: DEMO_TODAY, endDate: DEMO_TODAY, scheduledDate: DEMO_TODAY } });
    select(id); return id;
  }
  function deleteNode(nodeId: string) {
    const node = growth.nodes[nodeId];
    if (!node || nodeId === growth.goalId) return;
    const branch = collectNodeBranch(growth, nodeId);
    const parentId = node.parentId && growth.nodes[node.parentId] ? node.parentId : growth.goalId;
    dispatch({ type: 'DELETE_NODE', nodeId });
    if (selectedId && branch.has(selectedId)) select(null);
    if (branch.has(spaceId)) { setSpaceId(parentId); select(parentId); }
    if (branch.has(focus.nodeId)) setFocus(old => ({ ...old, nodeId: parentId, running: false }));
    setPositions(old => Object.fromEntries(Object.entries(old).filter(([key]) => !key.split(':').some(id => branch.has(id)))));
    setFiles(old => old.filter(asset => {
      if (!branch.has(asset.ownerId)) return true;
      URL.revokeObjectURL(asset.url); objectUrls.current.delete(asset.url); return false;
    }));
    setProposals(old => old.filter(proposal => !branch.has(proposal.nodeId)));
    setPreviewProposalId(null); setImpact(false);
    setJournals(old => old.map(journal => ({ ...journal, linkedNodeIds: journal.linkedNodeIds.filter(id => !branch.has(id)) })));
    setConversations(old => old.map(conversation => ({ ...conversation, linkedNodeIds: conversation.linkedNodeIds.filter(id => !branch.has(id)) })));
    setMessages(old => old.map(message => message.contextId && branch.has(message.contextId)
      ? { ...message, contextId: undefined, proposalId: undefined }
      : message));
  }
  function addFiles(ownerId: string, incoming: File[]) {
    const assets = incoming.map(file => { const url = URL.createObjectURL(file); objectUrls.current.add(url); return { id: crypto.randomUUID(), ownerId, name: file.name, size: file.size, mime: file.type, url, file }; });
    setFiles(old => [...old, ...assets]);
  }
  function removeFile(id: string) { const asset = files.find(f => f.id === id); if (asset) { URL.revokeObjectURL(asset.url); objectUrls.current.delete(asset.url); } setFiles(old => old.filter(f => f.id !== id)); }
  function publishJournal(content: string, tags: string[], linkedNodeIds: string[], images: File[]) {
    const id = crypto.randomUUID(); setJournals(old => [{ id, content: content.trim(), tags, linkedNodeIds, date: getBeijingDate() }, ...old]); addFiles(id, images);
  }
  function sendHistory(id: string, text: string) {
    if (id === 'admission') { send(text); return; }
    setConversations(old => old.map(c => c.id !== id ? c : { ...c, messages: [...c.messages,
      { id: crypto.randomUUID(), role: 'user', text },
      { id: crypto.randomUUID(), role: 'assistant', text: `我们可以继续聊「${c.title}」。先把你最在意的问题变成一个小行动，再回到关联空间安排它。\n\n这是本地演示回复，真实理解会在后续版本接入。` },
    ] }));
  }
  const append = (message: Omit<Message, 'id'>) => setMessages(old => [...old, { ...message, id: crypto.randomUUID() }]);
  function apply(action: PlanAction) {
    dispatch(action);
    if (action.type === 'UPDATE_TIME') {
      setProposals(old => old.map(p => p.nodeId === action.nodeId && p.status === 'pending' ? { ...p, status: 'outdated' } : p));
      const node = growth.nodes[action.nodeId];
      if (node.id === 'project' && action.startDate >= '2026-12-01' && action.startDate <= '2027-01-15') {
        const id = crypto.randomUUID();
        setProposals(old => [...old, { id, nodeId: node.id, originalStart: action.startDate, status: 'pending', actions: [{ type: 'UPDATE_TIME', nodeId: node.id, startDate: '2027-01-18', endDate: '2027-02-28' }] }]);
        append({ role: 'assistant', contextId: node.id, proposalId: id, text: `你把「科研项目」调整到了 ${Number(action.startDate.slice(5, 7))} 月。\n\n这可能与期末复习阶段产生时间冲突。\n\n我建议保留 10 月的导师联系，但将正式科研项目调整到寒假。` });
      } else append({ role: 'assistant', contextId: node.id, text: `已将「${node.title}」调整为 ${action.startDate} 至 ${action.endDate}。路径、时间线和任务会同步读取这次变更。` });
    }
  }
  function accept(id: string) {
    const proposal = proposals.find(p => p.id === id);
    if (!proposal || proposal.status !== 'pending') return;
    proposal.actions.forEach(dispatch);
    setProposals(old => old.map(p => p.id === id ? { ...p, status: 'accepted' } : p.nodeId === proposal.nodeId && p.status === 'pending' ? { ...p, status: 'outdated' } : p));
    setImpact(false);
    const change = proposal.actions.find(a => a.type === 'UPDATE_TIME');
    append({ role: 'assistant', text: change ? `已将「${growth.nodes[proposal.nodeId].title}」安排在 ${change.startDate} 至 ${change.endDate}。其他安排保持不变，路径、时间线和任务已同步。` : '已接受调整。', contextId: proposal.nodeId });
  }
  function send(text: string) {
    const node = selectedId ? growth.nodes[selectedId] : null;
    append({ role: 'user', text, contextId: selectedId ?? undefined });
    if (node?.startDate && node.endDate && /推迟|太早|晚一点|延后|往后/.test(text)) {
      const requestedFebruary = /2\s*月|二月/.test(text);
      const startDate = requestedFebruary && `${node.startDate.slice(0,4)}-02-01` > node.startDate
        ? `${node.startDate.slice(0,4)}-02-01`
        : requestedFebruary ? `${Number(node.startDate.slice(0,4)) + 1}-02-01` : shiftDate(node.startDate, 30);
      const endDate = shiftDate(node.endDate, daysBetween(node.startDate, startDate));
      const id = crypto.randomUUID();
      setProposals(old => [...old.map(p => p.nodeId === node.id && p.status === 'pending' ? { ...p, status: 'outdated' as const } : p), { id, nodeId: node.id, originalStart: node.startDate!, status: 'pending', actions: [{ type: 'UPDATE_TIME', nodeId: node.id, startDate, endDate }] }]);
      append({ role: 'assistant', contextId: node.id, proposalId: id, text: `可以先预览把「${node.title}」推迟到 ${startDate} 的安排，持续时间保持不变。\n\n${node.id === 'project' ? '科研启动延后可能压缩后续论文产出和夏令营准备时间，建议提前保留导师沟通与文献阅读。' : '延后可能压缩后续安排的准备时间，建议确认相关节点是否需要同步调整。'}其他任务暂不变动。\n\n这是本地演示建议；查看影响不会修改计划，接受后才会更新。` });
      return;
    }
    append({ role: 'assistant', contextId: selectedId ?? undefined, text: node
      ? `我们可以围绕「${node.title}」继续梳理。${node.description || '先确定一个足够小、可以开始的行动。'}\n\n${/推迟|调整|时间/.test(text) ? '你可以切换到时间线，拖动任务或编辑日期。将科研项目移到 12 月，还可以体验冲突分析与调整建议。' : '建议先写下当前最不确定的一点，再为它安排一次小规模尝试。你可以在任务视图勾选完成，查看各个视图的同步变化。'}\n\n这是一条基于当前上下文的本地演示回复。`
      : '当前计划围绕学业、科研、综合经历与个人成长展开。建议先选择「科研能力」或「联系导师」，我们就能围绕一个具体对象讨论。\n\n当前为本地 Mock 对话，尚未连接真实 AI。' });
  }
  return { growth, apply, selectedId, select, messages, proposals, accept, send, positions, setPositions, impact, setImpact, previewProposalId, previewProposal,
    spaceId, enterSpace, updatePlanMeta, addNode, deleteNode, files, addFiles, removeFile, journals, publishJournal, conversations, setConversations, sendHistory, settings, setSettings, focus, setFocus };
}
const Context = createContext<ReturnType<typeof useDemoState> | null>(null);
function UserWorkspaceProvider({ children, user }: { children: ReactNode; user: AccountProfile | null }) {
  const value = useDemoState(user);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export function DemoProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  return <UserWorkspaceProvider key={user?.id ?? 'guest'} user={user}>{children}</UserWorkspaceProvider>;
}
export function useDemo() { const context = useContext(Context); if (!context) throw new Error('DemoProvider missing'); return context; }
