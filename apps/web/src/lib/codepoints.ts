/**
 * 数一段文字有多少个**码点**。后端的长度上限全都按这个数。
 *
 * ## 为什么不是 `text.length`
 *
 * `String.prototype.length` 数的是 **UTF-16 码元**,而 Python 的 `len()` 数的是
 * **码点** —— 服务端那条 300 的上限就是 `len()`。两者对星平面字符(emoji、少数字体)
 * 给出的答案不一样:`'🛫'.length === 2`,而它只有一个码点。
 *
 * 于是"用 `text.length` 数"会在一个用户粘贴了 emoji 的时候**提前拦住**一段服务端
 * 本来接受的文字 —— 偏保守方向的错配仍然是错配,而且用户看不见:输入框里的计数
 * 说"已经 300 了",而服务端那边其实还有一半的余量。
 *
 * ## 为什么也不是 `Intl.Segmenter`
 *
 * 它数的是**字素**(用户感知的字符)。`'👨‍👩‍👧'` 是一个字素、五个码点,于是同一个串
 * 在两边的判断会差五倍 —— 而它恰好是这类输入里最常见的一个。只有码点这一条,
 * 前后端才能**按构造**算出同一个数,而不是"大多数情况下一致"。
 *
 * 权威定义在后端 `backend/contracts/plan.py`(`MAX_DESCRIPTION_CODEPOINTS` /
 * `MAX_NOTE_CODEPOINTS`)。这里只是把同一个规则搬到界面上,**提前告知** ——
 * 真正的执行在服务端,前端拦不住的(改了协议、换了客户端)它照样拒。
 */

/** 简述(`plan_nodes.description`)的码点上限。权威在 `backend/contracts/plan.py`。 */
export const MAX_DESCRIPTION_CODEPOINTS = 300;

/** 长正文(笔记)的码点上限。权威同上。 */
export const MAX_NOTE_CODEPOINTS = 20_000;

/** 这段文字有多少个码点。理由见文件头 —— **不要**退化成 `text.length`。 */
export function codePointLength(text: string): number {
  // `Array.from` 按码点切开一个字符串,`[...text]` 是它的展开写法。两者都会把
  // 星平面字符当成**一个**元素,而 `for (let i = 0; i < text.length; i++)` 会当成两个。
  return Array.from(text).length;
}

/**
 * 一份简述此刻受不受 300 码点那条上限的约束。
 *
 * 上限是一条**条件规则**(见后端 `description_length_error`):存量已经超过 300 的
 * 说明**继续想写多长写多长** —— 那些是上限出现之前用户唯一的表达方式,对它们收口
 * 等于追溯性地宣布他已经写下的东西不合法。所以界面上要分开说两种话:
 * 受限的显示 `{n}/300`,豁免的显示"这份正文超过 300,不受上限约束"。
 *
 * **判据是"存量",不是"此刻输入框里的字"。** 拿输入框里那个数判的话,用户把一个
 * 豁免节点的正文删到 200 字、计数器就从"不受限"翻成"还可以写 300 字" —— 而服务端
 * 那边仍然豁免(它比的是库里那一份)。于是界面承诺了一个它兑现不了的规则。
 */
export function isDescriptionExempt(existing: string | null | undefined): boolean {
  return Boolean(existing) && codePointLength(existing ?? '') > MAX_DESCRIPTION_CODEPOINTS;
}
