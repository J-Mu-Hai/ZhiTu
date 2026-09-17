import { ReportDetail } from '@/features/profile/ProfileDetail';
export default async function Page({params}:{params:Promise<{period:string}>}){const {period}=await params;return <ReportDetail period={period}/>;}
