import { Suspense } from 'react';
import { Workbench } from '@/components/workbench/Workbench';
export default function Page() { return <Suspense fallback={<div className="loading">正在展开成长空间…</div>}><Workbench/></Suspense>; }
