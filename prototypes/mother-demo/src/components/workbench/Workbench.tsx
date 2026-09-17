'use client';
import { useRouter, useSearchParams } from 'next/navigation';
import { GitBranch, ChartNoAxesGantt, ListTodo, ChevronRight, PanelRightClose, PanelRightOpen, ArrowLeft } from 'lucide-react';
import { useState } from 'react';
import { PathView } from '@/components/growth/PathView';
import { TimelineView } from './TimelineView';
import { TaskView } from './TaskView';
import { ConversationPanel } from '@/components/conversation/ConversationPanel';
import { useDemo } from '@/features/growth/provider';
import { spaceTrail } from '@/features/growth/selectors';
export function Workbench() {
  const params = useSearchParams(); const router = useRouter();
  const value = params.get('view'); const view = value === 'timeline' || value === 'tasks' ? value : 'path';
  const [chatOpen, setChatOpen] = useState(true); const { growth, spaceId, enterSpace } = useDemo();
  const trail = spaceTrail(growth, spaceId);
  const tabs = [{ id: 'path', label: '路径', Icon: GitBranch }, { id: 'timeline', label: '时间线', Icon: ChartNoAxesGantt }, { id: 'tasks', label: '任务', Icon: ListTodo }];
  return <div className="workbench open-workbench"><div className={`workbench-body ${chatOpen ? '' : 'chat-hidden'}`}><section className="workspace">
    <div className="space-topbar"><div className="space-breadcrumb" aria-label="空间路径">{spaceId !== 'goal' && <button className="icon-button" aria-label="返回上级空间" onClick={() => enterSpace(growth.nodes[spaceId].parentId ?? 'goal')}><ArrowLeft size={15}/></button>}{trail.map((node,i)=><span key={node.id}>{i>0&&<ChevronRight size={12}/>}<button onClick={()=>enterSpace(node.id)}>{node.id === 'goal' ? '保研计划' : node.title}</button></span>)}</div><div className="view-tabs" role="tablist" aria-label="工作台视图">{tabs.map(({id,label,Icon}) => <button key={id} role="tab" aria-selected={view === id} className={view === id ? 'selected' : ''} onClick={() => router.replace(`/workbench?view=${id}`, { scroll: false })}><Icon size={15}/>{label}</button>)}</div><button className="icon-button" aria-label={chatOpen ? '收起对话' : '展开对话'} onClick={() => setChatOpen(!chatOpen)}>{chatOpen ? <PanelRightClose size={18}/> : <PanelRightOpen size={18}/>}</button></div>
    <div className="view-content">{view === 'path' ? <PathView key={spaceId}/> : view === 'timeline' ? <TimelineView/> : <TaskView/>}</div></section>{chatOpen && <ConversationPanel/>}</div></div>;
}
