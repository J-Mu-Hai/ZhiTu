import type { Category, GrowthNode, GrowthState } from '@/types/growth';

export const DEMO_TODAY = '2026-09-16';
export const user = { id: 'student', name: '同路人', major: '人工智能', year: '大二', rank: 30, targetYear: 2027 };
export const categories: { id: Category; title: string; subtitle: string; number: string }[] = [
  { id: 'academic', title: '学业成绩', subtitle: '建立扎实的学业优势', number: '01' },
  { id: 'research', title: '科研能力', subtitle: '从好奇走向真实探索', number: '02' },
  { id: 'experience', title: '综合经历', subtitle: '让能力在实践中生长', number: '03' },
  { id: 'personal', title: '个人成长', subtitle: '找到可持续的节奏', number: '04' },
];
const task = (id: string, title: string, category: Category, startDate: string, endDate: string, extra: Partial<GrowthNode> = {}): GrowthNode => ({
  id, title, type: 'task', timelineLevel: ['attention', 'coursework-today', 'reading-today', 'screen'].includes(id) ? 'action' : ['gpa', 'contact', 'project', 'paper', 'competition', 'internship'].includes(id) ? 'major' : 'task', parentId: category, category, stageId: 'stage-1', status: 'pending', priority: 'medium', startDate, endDate, scheduledDate: startDate, ...extra,
});
const nodes: GrowthNode[] = [
  { id: 'goal', title: '保研', description: '2027 · 走向更广阔的 AI 世界', type: 'goal', status: 'doing', priority: 'high' },
  { id: 'stage-1', title: '建立学业优势与科研入口', type: 'stage', parentId: 'goal', status: 'doing', priority: 'high', startDate: '2026-09-01', endDate: '2026-12-31' },
  ...categories.map(c => ({ id: c.id, title: c.title, description: c.subtitle, type: 'capability' as const, parentId: 'goal', category: c.id, status: 'doing' as const, priority: 'high' as const })),
  task('gpa', 'GPA 提升', 'academic', '2026-09-01', '2027-06-15', { status: 'doing', description: '稳步提升专业排名，为保研建立学业优势。' }),
  task('courses', '核心课程', 'academic', '2026-09-20', '2027-01-10', { description: '保持核心课程成绩，优先完成课程作业。' }),
  task('specialize', '专业深化', 'academic', '2026-09-27', '2026-12-20'),
  task('course-plan', '完成开学课程规划', 'academic', '2026-09-01', '2026-09-14', { status: 'completed' }),
  task('coursework-today', '完成课程作业', 'academic', '2026-09-16', '2026-09-16', { estimatedHours: 1, description: '18:00 · 复习本周课程，完成课后练习。' }),
  task('explore', '方向探索', 'research', '2026-09-01', '2026-09-15', { status: 'completed' }),
  task('attention', 'Transformer · Attention', 'research', '2026-09-16', '2026-09-18', { status: 'doing', estimatedHours: 1.5, description: '完成 Attention 基础学习，理解 Query、Key 与 Value。', priority: 'high' }),
  task('reading-today', '阅读一篇研究方向介绍', 'research', '2026-09-16', '2026-09-16', { estimatedHours: 0.5, description: '20:00 · 轻量了解大模型研究方向，记录一个问题。' }),
  task('screen', '筛选潜在导师', 'research', '2026-09-21', '2026-10-05', { estimatedHours: 1 }),
  task('contact', '联系导师', 'research', '2026-10-06', '2026-10-16', { scheduledDate: '2026-09-16', estimatedHours: 0.5, description: '今天整理导师信息与邮件提纲，10 月正式联系。' }),
  task('project', '科研项目', 'research', '2026-10-18', '2026-11-28', { description: '参与导师的小型科研项目，从复现与实验记录开始。' }),
  task('paper', '论文产出', 'research', '2027-02-01', '2027-05-01', { stageId: 'stage-2' }),
  task('competition', '竞赛经历', 'experience', '2026-11-01', '2026-12-15'),
  task('practice', '项目实践', 'experience', '2026-10-01', '2026-11-20'),
  task('internship', '实习经历', 'experience', '2027-02-01', '2027-04-01', { stageId: 'stage-2' }),
  task('english', '英语能力', 'experience', '2026-09-20', '2027-03-01'),
  task('time', '时间管理', 'personal', '2026-09-01', '2026-12-31', { status: 'doing' }),
  task('mindset', '心态调整', 'personal', '2026-09-01', '2026-12-31'),
  task('health', '健康生活', 'personal', '2026-09-01', '2027-01-31'),
  task('long-term', '长期思维', 'personal', '2026-09-01', '2027-06-01'),
  { id: 'direction-set', title: '方向确定', timelineLevel: 'major', type: 'milestone', parentId: 'research', category: 'research', startDate: '2026-09-30', endDate: '2026-09-30', status: 'pending', priority: 'high' },
  { id: 'first-contact', title: '第一次联系', type: 'milestone', parentId: 'research', category: 'research', startDate: '2026-10-16', endDate: '2026-10-16', status: 'pending', priority: 'high' },
];
export const initialGrowth: GrowthState = {
  id: 'growth-1', title: '保研计划', goalId: 'goal', currentStageId: 'stage-1',
  nodes: Object.fromEntries(nodes.map(n => [n.id, n])),
  edges: [{ id: 'contact-project', source: 'contact', target: 'project', type: 'dependency' }],
};
export const pathTaskIds: Record<Category, string[]> = {
  academic: ['gpa', 'courses', 'specialize', 'course-plan'],
  research: ['explore', 'contact', 'project', 'paper'],
  experience: ['competition', 'practice', 'internship', 'english'],
  personal: ['time', 'mindset', 'health', 'long-term'],
};
