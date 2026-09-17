import type { Conversation } from '@/types/growth';
export const initialConversation: Conversation = {
  id: 'admission', title: '保研计划', linkedNodeIds: ['goal', 'research', 'contact'],
  messages: [
    { id: 'm1', role: 'user', text: '我想保研，但是我现在大二，也不知道应该怎么准备。' },
    { id: 'm2', role: 'assistant', text: '可以。我们可以一步一步梳理。\n\n为了更好地帮你制定计划，我想先了解：\n\n1. 你目前的专业和成绩情况如何？\n2. 是否有科研经历或感兴趣的方向？\n3. 你期望的目标院校或专业方向是什么？' },
    { id: 'm3', role: 'user', text: '我是人工智能专业，专业排名大概 30 名，还没有科研经历，但对大模型方向比较感兴趣，目标是本校保研或者去更好的学校。' },
    { id: 'm4', role: 'assistant', text: '根据这些信息，我先为你建立了一个初步的成长框架。\n\n学业成绩 · 稳住成绩，逐步提升排名\n科研能力 · 探索方向，建立导师联系\n综合经历 · 用项目和竞赛积累实践\n个人成长 · 找到可持续的行动节奏\n\n其中科研能力是目前最大的不确定项。我们可以优先从方向探索和导师联系开始。\n\n试着在左侧选择一个节点，我们一起把它变得更具体。' },
  ],
};
