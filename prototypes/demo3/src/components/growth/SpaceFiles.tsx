'use client';
import { useEffect, useState } from 'react';
import Image from 'next/image';
import { Download, FileText, Trash2, Upload, Eye } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { Dialog } from '@/components/ui/Dialog';
import type { FileAsset } from '@/types/growth';
export function FilePreview({asset,onClose}:{asset:FileAsset;onClose:()=>void}) {
  const [text,setText]=useState('');
  const isText=asset.mime.startsWith('text/')||/\.(md|txt|csv|json)$/i.test(asset.name);
  useEffect(()=>{let active=true;if(isText)asset.file.slice(0,100000).text().then(t=>{if(active)setText(t);});return()=>{active=false;};},[asset,isText]);
  return <Dialog title={asset.name} onClose={onClose}><div className="file-preview">{asset.mime.startsWith('image/')?<Image unoptimized src={asset.url} width={800} height={600} alt={asset.name}/>:isText?<pre>{text}</pre>:<div className="file-placeholder"><FileText size={40}/><p>此格式可下载后用本地应用打开。</p></div>}</div><a className="primary-button" href={asset.url} download={asset.name}><Download size={14}/>下载文件</a></Dialog>;
}
export function SpaceFiles({ownerId}:{ownerId:string}) {
  const {files,addFiles,removeFile}=useDemo(); const [preview,setPreview]=useState<FileAsset|null>(null);
  const assets=files.filter(f=>f.ownerId===ownerId);
  return <div className="space-files"><label className="file-drop" onDragOver={e=>e.preventDefault()} onDrop={e=>{e.preventDefault();addFiles(ownerId,Array.from(e.dataTransfer.files));}}><Upload size={20}/><strong>把资料放进这个空间</strong><span>点击选择，或拖入文件 · 仅本次会话保留</span><input type="file" multiple aria-label="添加空间文件" onChange={e=>{addFiles(ownerId,Array.from(e.target.files??[]));e.target.value='';}}/></label>{assets.length===0?<p className="empty-note">论文、图片、笔记……让想法和资料待在一起。</p>:<div className="file-list">{assets.map(f=><div key={f.id}><FileText size={18}/><button onClick={()=>setPreview(f)}><strong>{f.name}</strong><small>{(f.size/1024).toFixed(1)} KB</small></button><button className="icon-button" aria-label={`预览 ${f.name}`} onClick={()=>setPreview(f)}><Eye size={15}/></button><a className="icon-button" href={f.url} download={f.name} aria-label={`下载 ${f.name}`}><Download size={15}/></a><button className="icon-button" aria-label={`移除 ${f.name}`} onClick={()=>removeFile(f.id)}><Trash2 size={15}/></button></div>)}</div>}{preview&&<FilePreview asset={preview} onClose={()=>setPreview(null)}/>}</div>;
}
