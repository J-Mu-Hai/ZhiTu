'use client';
import { useRef, useState } from 'react';
import { Grip, X } from 'lucide-react';
import { ConversationPanel } from './ConversationPanel';

export function FloatingConversation({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [offset, setOffset] = useState({x:0,y:0});
  const drag = useRef<{x:number;y:number;left:number;top:number}|null>(null);
  return <div className="conversation-overlay" hidden={!open}>
    <section className="floating-conversation" aria-label="浮动对话卡片群" style={{transform:`translate(${offset.x}px, ${offset.y}px)`}}>
      <div className="floating-title"><button className="conversation-move" aria-label="整体移动对话" onPointerDown={e=>{e.currentTarget.setPointerCapture(e.pointerId);drag.current={x:e.clientX,y:e.clientY,left:offset.x,top:offset.y};}} onPointerMove={e=>{const d=drag.current;if(d)setOffset({x:Math.max(-220,Math.min(20,d.left+e.clientX-d.x)),y:Math.max(-12,Math.min(32,d.top+e.clientY-d.y))});}} onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}} onKeyDown={e=>{if(e.key.startsWith('Arrow')){e.preventDefault();setOffset(p=>({x:Math.max(-220,Math.min(20,p.x+(e.key==='ArrowLeft'?-10:e.key==='ArrowRight'?10:0))),y:Math.max(-12,Math.min(32,p.y+(e.key==='ArrowUp'?-10:e.key==='ArrowDown'?10:0)))}));}}}><Grip size={15}/>一起思考 <small>拖动此处，整体移动</small></button><button className="icon-button" aria-label="让对话内容消失" onClick={onClose}><X size={15}/></button></div>
      <ConversationPanel/>
    </section>
  </div>;
}
