'use client';
import { useMemo, useState } from 'react';
import { ReactFlow, ReactFlowProvider, Background, Controls, MiniMap, Handle, Position, useReactFlow, type Node, type NodeProps, type Edge } from '@xyflow/react';
import { Flag, GraduationCap, FlaskConical, Package, Sprout, Check, Focus, Plus, FolderOpen, ArrowUpRight, FileText } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { Dialog } from '@/components/ui/Dialog';
import { SpaceFiles } from './SpaceFiles';
import type { GrowthNode } from '@/types/growth';
type FlowNode = Node<{ object: GrowthNode; root: boolean; children: number; files: number }, 'growth'>;
const icons = { academic: GraduationCap, research: FlaskConical, experience: Package, personal: Sprout };
const colors = { academic: '#749ce1', research: '#61ad9e', experience: '#c7a06e', personal: '#a294ce' };
function GrowthNodeComponent({ data, selected }: NodeProps<FlowNode>) {
  const {enterSpace}=useDemo(); const n=data.object; const Icon=n.category?icons[n.category]:Flag;
  return <div onDoubleClick={e=>{e.stopPropagation();enterSpace(n.id);}} className={`growth-node ${data.root?'goal':n.type} ${n.category??''} ${selected?'is-selected':''} ${n.status==='completed'?'is-complete':''}`}>
    {!data.root&&<Handle type="target" position={Position.Left}/>} 
    {data.root?<><Flag size={23}/><strong>{n.title}</strong><span>{n.id==='goal'?'2027 · AI 方向':'当前空间 · 母节点'}</span></>:n.type==='capability'?<><Icon size={22}/><div><strong>{n.title}</strong><span>{n.description||`${data.children} 个子节点`}</span></div></>:<><span className="node-bullet">{n.type==='milestone'?'◇':n.status==='completed'?<Check size={12}/>:'·'}</span><span>{n.title}</span>{n.status==='doing'&&<span className="node-doing"/>}</>}
    {!data.root&&<button className="node-enter nodrag" aria-label={`进入${n.title}空间`} onClick={e=>{e.stopPropagation();enterSpace(n.id);}}><ArrowUpRight size={12}/></button>}
    {data.files>0&&<span className="node-file-count"><FileText size={10}/>{data.files}</span>}
    <Handle type="source" position={Position.Right}/>
  </div>;
}
const nodeTypes={growth:GrowthNodeComponent};
function Canvas() {
  const {growth,selectedId,select,positions,setPositions,spaceId,enterSpace,addNode,files}=useDemo();
  const {fitView}=useReactFlow(); const [measurements,setMeasurements]=useState<Record<string,{width:number;height:number}>>({}); const [dragging,setDragging]=useState<Record<string,{x:number;y:number}>>({});
  const [dialog,setDialog]=useState<'node'|'files'|null>(null); const [title,setTitle]=useState(''); const [type,setType]=useState<GrowthNode['type']>('task');
  const direct=Object.values(growth.nodes).filter(n=>n.parentId===spaceId&&n.type!=='stage');
  const {nodes,edges}=useMemo(()=>{
    const nodes:FlowNode[]=[];const edges:Edge[]=[];const all=Object.values(growth.nodes);
    const direct=all.filter(n=>n.parentId===spaceId&&n.type!=='stage');
    function add(n:GrowthNode,x:number,y:number,root=false){const key=`${spaceId}:${n.id}`;nodes.push({id:n.id,measured:measurements[n.id],type:'growth',data:{object:n,root,children:all.filter(c=>c.parentId===n.id).length,files:files.filter(f=>f.ownerId===n.id).length},position:dragging[key]??positions[key]??{x,y},selected:selectedId===n.id,ariaLabel:n.title});}
    function connect(parent:string,n:GrowthNode){edges.push({id:`${parent}-${n.id}`,source:parent,target:n.id,type:'smoothstep',style:{stroke:colors[n.category??'academic'],opacity:selectedId===n.id ? .7:.4,strokeWidth:1}});}
    add(growth.nodes[spaceId],0,Math.max(20,(direct.length-1)*(spaceId==='goal'?250:100)/2),true);
    direct.forEach((n,i)=>{const x=260;const y=20+i*(spaceId==='goal'?250:100);add(n,x,y);connect(spaceId,n);
      if(spaceId==='goal'&&n.type==='capability')all.filter(c=>c.parentId===n.id).filter(c=>!['attention','screen'].includes(c.id)).slice(0,4).forEach((c,j)=>{add(c,x+235,y-20+j*54);connect(n.id,c);});
    });
    growth.edges.forEach(edge=>{if(nodes.some(n=>n.id===edge.source)&&nodes.some(n=>n.id===edge.target))edges.push({...edge,type:'default',style:{stroke:'#78a89a',strokeDasharray:'4 5',opacity:.45}});});
    return{nodes,edges};
  },[growth,spaceId,selectedId,positions,dragging,files,measurements]);
  return <div className="path-canvas"><div className="space-floating-tools"><span>单击讨论 · 双击进入空间</span><button onClick={()=>setDialog('node')}><Plus size={15}/>新建节点</button><button onClick={()=>setDialog('files')}><FolderOpen size={15}/>空间文件 <small>{files.filter(f=>f.ownerId===spaceId).length||''}</small></button></div>
    <ReactFlow<FlowNode> nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{padding:.18,maxZoom:1}} zoomOnDoubleClick={false} minZoom={.25} maxZoom={1.7} onNodeClick={(_,n)=>select(n.id)} onNodeDoubleClick={(_,n)=>enterSpace(n.id)} onPaneClick={()=>select(null)} onNodesChange={changes=>{for(const c of changes){if(c.type==='position'&&c.position)setDragging(old=>({...old,[`${spaceId}:${c.id}`]:c.position!}));if(c.type==='dimensions'&&c.dimensions){const d=c.dimensions;setMeasurements(old=>old[c.id]?.width===d.width&&old[c.id]?.height===d.height?old:{...old,[c.id]:d});}}}} onNodeDragStop={(_,n)=>setPositions(old=>({...old,[`${spaceId}:${n.id}`]:n.position}))} nodesConnectable={false} deleteKeyCode={null} colorMode="light" proOptions={{hideAttribution:true}}>
      <Background gap={28} size={1} color="#dfe6ef"/><MiniMap position="bottom-left" style={{width:120,height:78}} nodeColor={n=>colors[(n.data.object as GrowthNode).category??'academic']} maskColor="rgba(245,248,252,.72)" pannable zoomable/><Controls position="bottom-right" showInteractive={false}/>
    </ReactFlow>
    {direct.length===0&&<div className="empty-space-note"><span>这里，还可以长出更多可能。</span><button onClick={()=>setDialog('node')}><Plus size={14}/>添加第一个子节点</button></div>}
    <button className="focus-button" disabled={!selectedId||!nodes.some(n=>n.id===selectedId)} onClick={()=>{if(selectedId)void fitView({nodes:[{id:selectedId}],duration:220,maxZoom:1.2,padding:.8});}}><Focus size={15}/>聚焦所选</button>
    {dialog==='node'&&<Dialog title={`在「${growth.nodes[spaceId].title}」中新建节点`} onClose={()=>setDialog(null)}><form className="node-form" onSubmit={e=>{e.preventDefault();if(title.trim()){addNode(title,type);setTitle('');setDialog(null);setTimeout(()=>void fitView({duration:200,padding:.2,maxZoom:1}),80);}}}><label>节点名称<input autoFocus maxLength={80} value={title} onChange={e=>setTitle(e.target.value)} placeholder="一个想法、一个行动，或新的方向"/></label><label>节点类型<select value={type} onChange={e=>setType(e.target.value as GrowthNode['type'])}><option value="task">任务</option><option value="capability">能力 / 方向</option><option value="milestone">里程碑</option></select></label><p>每个新节点也可以继续进入，成为自己的空间。</p><button className="primary-button" disabled={!title.trim()}>创建节点</button></form></Dialog>}
    {dialog==='files'&&<Dialog title={`${growth.nodes[spaceId].title} · 空间文件`} onClose={()=>setDialog(null)}><SpaceFiles ownerId={spaceId}/></Dialog>}
  </div>;
}
export function PathView(){return <ReactFlowProvider><Canvas/></ReactFlowProvider>;}
