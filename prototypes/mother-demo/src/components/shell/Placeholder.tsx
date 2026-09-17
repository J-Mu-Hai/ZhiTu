import Link from 'next/link';
import { ArrowLeft, Sprout } from 'lucide-react';
export function Placeholder({title,description,phase}:{title:string;description:string;phase:string}) {return <section className="placeholder-page"><span className="eyebrow">知途 · {phase}</span><h1>{title}</h1><p>{description}</p><div className="placeholder-center"><Sprout size={40}/><h2>为成长，留一点空间。</h2><p>这个模块将在{phase}展开。<br/>当前可以完整体验工作台的三种视图与上下文对话。</p><Link className="primary-button" href="/workbench"><ArrowLeft size={15}/>回到工作台</Link></div></section>;}
