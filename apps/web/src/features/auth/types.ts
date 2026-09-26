import type { UserProfile } from '@/lib/backend';

/**
 * 界面上用的账户档案。
 *
 * ## 与上一版的区别:没有 `passwordDigest` 了
 *
 * 上一版把口令的 SHA-256 摘要和账户信息一起存在 localStorage 里,靠它自己"验证密码"。
 * 那个方案有三个问题,而且都是结构性的:
 *
 * 1. 摘要就是一个无迭代、只加账户 id 当盐的 SHA-256 —— 拿到 localStorage 的副本,
 *    离线暴力破解的成本很低。
 * 2. "验证密码"发生在浏览器里,意味着**任何人都可以改自己 localStorage 里的那一条
 *    然后把密码设成任意值**。它不是一道门,是一张贴在门上的纸。
 * 3. 它让"这个账户是谁"这个问题在前端和后端有两个答案,而这两个答案迟早会不一致。
 *
 * 现在密码只在 `POST /api/auth/register` 与 `/login` 的请求体里出现一次,服务端用
 * scrypt 存哈希,浏览器这边**一个字都不留**。
 */
export interface AccountProfile {
  id: string;
  name: string;
  email: string;
  /** 用户所在时区。后端的"今天是哪一天"按它算,前端也用它显示日期。 */
  timezone: string;
  school: string;
  major: string;
  year: string;
  rank: number;
  targetYear: number;
  targetGoal: string;
  bio: string;
  createdAt: string;
}

export interface RegisterAccountInput {
  name: string;
  email: string;
  password: string;
  school: string;
  major: string;
  year: string;
  targetYear: number;
  targetGoal: string;
}

export type EditableProfile = Pick<
  AccountProfile,
  'name' | 'school' | 'major' | 'year' | 'rank' | 'targetYear' | 'targetGoal' | 'bio'
>;

/**
 * 后端的 `UserProfile` -> 界面用的 `AccountProfile`。
 *
 * ## 缺失的字段映射成空串,不映射成"看起来合理的默认值"
 *
 * 后端把未填写的档案字段存成 NULL。这里如果给 `targetGoal` 补一个"我的目标",
 * 用户会在界面上看到一句他从没写过的话 —— 和"新空间被灌入保研示例"是同一类错误。
 * 空串在界面上表现为空白,那是诚实的。
 *
 * `rank` 是唯一例外:`0` 在这个界面里**已经是"未填写"的意思**了
 * (Profile.tsx 里写的就是 `user.rank || '未填写'`),所以补 0 不改变既有语义。
 */
export function toAccountProfile(user: UserProfile): AccountProfile {
  return {
    id: user.id,
    name: user.displayName ?? '',
    email: user.email,
    timezone: user.timezone || 'Asia/Shanghai',
    school: user.school ?? '',
    major: user.major ?? '',
    year: user.year ?? '',
    rank: user.rank ?? 0,
    targetYear: user.targetYear ?? 0,
    targetGoal: user.targetGoal ?? '',
    bio: user.bio ?? '',
    createdAt: user.createdAt,
  };
}
