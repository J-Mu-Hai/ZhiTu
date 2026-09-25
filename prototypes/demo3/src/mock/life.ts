import type { Conversation, JournalEntry } from '@/types/growth';
export const initialJournals: JournalEntry[] = [
  { id: 'journal-1', date: '2026-09-16', content: '第一次认真看了实验室主页\n\n今天第一次认真看了实验室主页。以前一直觉得科研离我很远，但是看了一圈发现，好像也没有想象中那么遥不可及。', linkedNodeIds: ['research'], tags: ['科研', '思考'], insight: '科研焦虑 ↓　行动意愿 ↑' },
  { id: 'journal-2', date: '2026-09-13', content: '最近有点焦虑\n\n感觉身边的人都开始准备科研了，而我还没有找到自己的方向。也许不用一下子想清楚全部，先试着去了解一位老师。', linkedNodeIds: ['contact'], tags: ['情绪'], insight: '你开始把模糊的担忧，变成一个具体的小行动。' },
];
export const historyConversations: Conversation[] = [
  { id: 'transformer', title: 'Transformer 学习', linkedNodeIds: ['attention'], messages: [
    { id: 't1', role: 'user', text: 'Attention 看起来很抽象，应该从哪里开始？' },
    { id: 't2', role: 'assistant', text: '先不急着看完整模型。今天用 90 分钟理解 Query、Key、Value，再手算一个简单的注意力分数。\n\n把问题缩小，你会更容易开始。', contextId: 'attention' },
  ] },
  { id: 'career', title: '职业方向', linkedNodeIds: ['research', 'practice'], messages: [
    { id: 'c1', role: 'user', text: 'AI Research 和工程方向，我应该怎么选择？' },
    { id: 'c2', role: 'assistant', text: '现在不必给自己一个最终答案。可以用一次小型论文复现和一个可用的项目，分别体验研究与工程。\n\n留意让你好奇、愿意继续投入的部分。' },
  ] },
  { id: 'future', title: '关于未来', linkedNodeIds: ['goal', 'long-term'], messages: [
    { id: 'f1', role: 'user', text: '我到底想成为什么样的人？' },
    { id: 'f2', role: 'assistant', text: '这个问题可以慢慢回答。保研是眼前的方向，持续学习、敢于尝试，以及照顾好自己，是你可以从今天开始积累的能力。\n\n我们可以把答案留在一次次真实的行动里。' },
  ] },
];
export const profileEntries = [
  { slug: 'archive', title: '成长档案', detail: '把每一步，连成自己的轨迹', icon: 'archive' },
  { slug: 'profile', title: '用户画像', detail: '认识行动背后的自己', icon: 'profile' },
  { slug: 'reports', title: '成长报告', detail: '停下来，看见变化', icon: 'report' },
  { slug: 'assets', title: '成长资产', detail: '你真正留下来的东西', icon: 'assets' },
  { slug: 'behavior', title: '行为数据', detail: '发现自己的节奏', icon: 'behavior' },
  { slug: 'memory', title: 'AI记忆', detail: '那些被认真记住的细节', icon: 'memory' },
  { slug: 'settings', title: 'AI设置', detail: '决定我们如何一起前进', icon: 'settings' },
];
export const memories = ['你更适合上午处理高认知任务。', '你在计划过多时容易降低执行率。', '你对科研方向存在兴趣，但目前仍处于探索阶段。'];
export const assetSummaries = [{title:'项目',count:3},{title:'论文 / 阅读记录',count:12},{title:'比赛',count:2},{title:'证书',count:4},{title:'成长里程碑',count:8}];
export const reports = [
  { id: 'week', title: '周报', date: '09.14 — 09.20', summary: '从「不知道怎么开始」，到第一次认真了解实验室。', sections: ['本周，你完成了方向探索，开始关注真实的科研入口。', '接下来只保留一个科研重点：整理导师资料。把更多精力留给核心课程。'] },
  { id: 'month', title: '月报', date: '2026 年 9 月', summary: '目标正在变清晰，行动也开始有了自己的节奏。', sections: ['学业、科研与生活不再是孤立的待办，而是同一条成长路径。', '下个月，尝试建立第一次导师联系。不以回复结果评价自己，以完成尝试作为进步。'] },
  { id: 'stage', title: '阶段报告', date: '2026 SEP — DEC', summary: '建立学业优势与科研入口。', sections: ['这一阶段的重点是稳住专业成绩，并走进一个真实的科研场景。', '阶段结束时再回看方向选择，允许根据课程与导师反馈调整计划。'] },
];
