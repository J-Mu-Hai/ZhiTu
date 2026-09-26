/**
 * 「我的」下面那几页的内容。
 *
 * ## 为什么从 `mock/life.ts` 搬过来
 *
 * `mock/` 是示例空间那份演示数据的家，而示例空间正在被整个删掉。但这个文件里的
 * 东西**分两类，处置不一样**，混在一起会一起被删掉：
 *
 * 1. **导航配置**（`profileEntries`）—— 「我的」页和顶栏都用它生成链接。它不是数据，
 *    删掉就是页面坏了，没有替代品。
 * 2. **写着"演示"的样例内容**（`memories` / `assetSummaries` / `reports`）——
 *    这几份是**硬编码的**，但它们和示例空间不同：界面上每一条都如实标着
 *    "演示记忆" / "以上为成长档案示例" / "报告为演示样例，尚未接入真实生成"
 *    （见 `ProfileDetail.tsx`）。也就是说它**没有假装成用户自己的数据**，
 *    而示例空间的假 AI 回复是假装了的 —— 那是它必须被删的原因，这里没有这个问题。
 *
 * 所以这一版只做搬迁，不动内容：它们不在"删除示例空间"的范围内。真要让「我的」
 * 这几页显示用户自己的东西，是接真实数据源的事，得单独做。
 */
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
