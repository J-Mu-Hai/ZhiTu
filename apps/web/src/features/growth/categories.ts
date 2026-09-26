import type { Category } from '@/types/growth';

/**
 * 四个固定成长的标签与展示顺序。
 *
 * ## 为什么它从 `mock/growth-state.ts` 搬到这里
 *
 * 它原来和示例空间的演示数据放在一起，于是看起来像"演示数据的一部分"。但它不是：
 * `TaskView` 拿它当**展示用的标签表和排序提示**，而任务视图读的是真实计划。
 * 示例空间删掉之后，这个文件如果还留在 `mock/` 里，会让人以为它也是可以一起删的
 * —— 删掉之后任务分组会退回"其他任务"，而那是个静默的显示退化，不会报错。
 *
 * ## 它的寿命到哪一步
 *
 * 后端计划里**没有 `category` 这个字段**（见 `backend/contracts/plan.py`），所以真实的
 * 节点全都没有分类，`TaskView` 是靠 `node.category ?? node.stageId` 兜底分组的 ——
 * 也就是说这里查得到的情况只可能出现在示例数据上。
 *
 * 真正该做的是让用户自己建的那张图取代"固定四分类"（A 批步骤 3）。到那时这个表
 * 连同 `Category` 类型一起删掉，而不是继续往里面加。
 */
export const categories: { id: Category; title: string; subtitle: string; number: string }[] = [
  { id: 'academic', title: '学业成绩', subtitle: '建立扎实的学业优势', number: '01' },
  { id: 'research', title: '科研能力', subtitle: '从好奇走向真实探索', number: '02' },
  { id: 'experience', title: '综合经历', subtitle: '让能力在实践中生长', number: '03' },
  { id: 'personal', title: '个人成长', subtitle: '找到可持续的节奏', number: '04' },
];
