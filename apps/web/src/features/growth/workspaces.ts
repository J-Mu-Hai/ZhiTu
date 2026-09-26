/**
 * 空间目录。
 *
 * ## 上一版是什么
 *
 * 上一版把空间列表存在 `localStorage['zhitu.workspaces.<用户>.v1']` 里,
 * 而且 `readWorkspaces` 有个**读的时候就写**的行为:如果列表不存在,它会就地伪造一条
 * `{id: 'primary', title: '${targetGoal}计划'}`,写回去,再返回。所以"用户有哪些空间"
 * 这个问题的答案,是在浏览器里被发明出来的。
 *
 * 现在空间是后端的行:`GET /api/workspaces` 返回真实存在的空间,`POST /api/workspaces`
 * 真的建一行。前端不再有"默认空间"这个概念 —— 一个刚注册的用户就是**没有空间**,
 * 界面该显示的是引导他建一个,而不是替他编一个。
 *
 * 这个文件只留下一个东西:本地计划图快照的存储键。计划图本身在阶段 5 之前仍然是
 * 前端本地状态,但它已经不再假装自己是"空间目录"了。
 */

export const workspaceStorageKey = (userId: string, workspaceId: string) =>
  `zhitu.workspace.${userId}.${workspaceId}.v2`;
