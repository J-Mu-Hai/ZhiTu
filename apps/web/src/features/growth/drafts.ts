'use client';

import { useCallback, useEffect, useState } from 'react';
import type { GrowthNode, GrowthRelationType } from '@/types/growth';

/**
 * 画布上**还没提交的输入**。
 *
 * ## 为什么它必须住在 React 组件外面
 *
 * 弹窗里打了一半的节点名、详情里改了一半的正文与工时,现在都是 `PathView` 里的一组
 * `useState`。而"整棵画布子树被重建"这件事在这套界面里**每次切视图都在发生**:
 * `Workbench.tsx` 的四个视图是一个三元表达式,切一下页签,`PathView` 连同 ReactFlow
 * 内部的视口一起被卸载。组件里的 `useState` 在那一刻什么都没了 —— 用户看到的是
 * "我打了一半的字自己没了",而没有任何一处会报错。
 *
 * 更远一点还有一层:`WorkspaceRouter` 用 `key={space.id}` 换一份空间状态。
 * 换空间**本来就该**换一份状态,但"换得干净"不等于"把上一个空间里正在写的东西
 * 一起带走"。所以这个存储既不在画布里,也不在 Provider 里 —— 后者会跟着空间一起重挂。
 *
 * ## 键为什么是"空间 + 层级",而不是"空间"
 *
 * 因为草稿必须**跟人走的那一层走**。同一个空间里,根画布上正在新建的节点和某个子空间
 * 里正在改的正文是两件互不相干的事,合成一份会让它们互相覆盖。而键里带上空间 id
 * 是隔离的另一半:层级 id 是节点 UUID,基本不会撞,但**占位根节点的 `'goal'`
 * 是所有空间共用的哨兵值** —— 少了空间这一半,一个还没拉到计划的空间会和另一个
 * 共享同一份草稿。
 *
 * ## 为什么只活在内存里
 *
 * 它存的是"还没提交的东西",而**没提交**在这里是有意义的:留着它去跨一次整页刷新,
 * 等于把一份用户已经离开、可能早就不想要的输入在下次打开时重新摆到他面前 ——
 * 那时他看到的弹窗和记忆里的对不上,而正确做法是让他重新说一次。所以边界画在
 * "这一次会话里的组件重建"上:切视图、切层级、离开工作台再回来都在内,刷新不在。
 *
 * 跨刷新、跨设备的那一份留给步骤 3 的后端 `scope_viewports` / 正文草稿,不在这里
 * 用一个半成品顶替。
 *
 * ## 什么时候丢
 *
 * 只有一种:**用户明确关掉弹窗**(× 或 Esc)。那是"我不写了"的意思,不是"你别丢"。
 * 卸载不同 —— 卸载对用户来说什么都没发生。
 */

/** 一次编辑会话里全部还没提交的输入。字段与画布上那几个弹窗一一对应。 */
export interface CanvasDraft {
  /** 现在开着哪个弹窗。`null` 是都没开。**它也要留住** —— "误关编辑器"指的是关掉这个,不是关掉输入框。 */
  dialog: 'node' | 'files' | 'relation' | null;
  /** 新建节点表单。 */
  title: string;
  description: string;
  type: GrowthNode['type'];
  estimate: string;
  /** 节点详情编辑器(`detailNodeId` 为空就是没开)。 */
  detailNodeId: string | null;
  detailTitle: string;
  detailDescription: string;
  detailPriority: GrowthNode['priority'];
  /** 真实空间里编辑的是截止时间;占位/演示数据里是开始与结束日期。两个不同的东西。 */
  detailDeadline: string;
  detailEstimate: string;
  detailStart: string;
  detailEnd: string;
  /**
   * 关系编辑器。**两种模式共用这一组字段**,靠 `relationId` 区分:
   *
   * - `relationId` 为空 = 「建立关系」表单:起点、终点、类型都是**还没提交**的选择,
   *   提交前库里什么都没有。
   * - `relationId` 非空 = 编辑一条已存在的边:改的是类型与说明。
   *
   * 合成一组而不是两组,是因为它们在界面上同一个弹窗、同一时刻只会有一个开着;
   * 分成两组的话"关掉它"要清两遍,而漏清一遍就是下一次打开时残留一个幽灵终点。
   */
  relationId: string | null;
  relationType: GrowthRelationType;
  relationNote: string;
  relationSource: string;
  relationTarget: string;
}

/**
 * 空草稿。**冻结的一个常量**,不是每次新建一个对象。
 *
 * 读一份不存在的草稿时会返回它,而 `useState` 拿它当初始值 —— 每次返回新对象的话,
 * "有没有草稿"这件事在 React 眼里会变成"每次都不一样",无谓地触发重渲染。
 */
export const EMPTY_DRAFT: CanvasDraft = Object.freeze({
  dialog: null,
  title: '',
  description: '',
  type: 'task',
  estimate: '',
  detailNodeId: null,
  detailTitle: '',
  detailDescription: '',
  detailPriority: 'medium',
  detailDeadline: '',
  detailEstimate: '',
  detailStart: '',
  detailEnd: '',
  relationId: null,
  relationType: 'related_to',
  relationNote: '',
  relationSource: '',
  relationTarget: '',
});

const drafts = new Map<string, CanvasDraft>();

/** 草稿的键:空间 + 层级。理由见文件头。 */
export function draftKeyFor(workspaceId: string, scopeId: string): string {
  return `${workspaceId}:${scopeId}`;
}

export function readDraft(key: string): CanvasDraft {
  return drafts.get(key) ?? EMPTY_DRAFT;
}

/** 每个字段都回到默认值 = 这份草稿已经没有内容了,不必在表里留一个空壳。 */
function isEmpty(draft: CanvasDraft): boolean {
  return (Object.keys(EMPTY_DRAFT) as (keyof CanvasDraft)[])
    .every((field) => draft[field] === EMPTY_DRAFT[field]);
}

/** 写一部分字段。没提到的字段保持原样。写空之后这一份会被收走。 */
export function writeDraft(key: string, patch: Partial<CanvasDraft>): CanvasDraft {
  const next = { ...readDraft(key), ...patch };
  if (isEmpty(next)) {
    drafts.delete(key);
    return EMPTY_DRAFT;
  }
  drafts.set(key, next);
  return next;
}

/**
 * 把这份草稿接到一个组件上。
 *
 * 组件重挂载之后 `useState` 会重新执行一次初始化,所以"卸载了再回来"这一步不需要
 * 额外的订阅机制 —— 挂载那一刻读到的就是它自己那一层上次留下的东西。
 * `key` 变化(换空间、换层级)时靠下面那个 effect 跟上,那是为了应付"键变了但组件
 * 没重挂"的那种过渡帧(计划刚到、`spaceId` 还在哨兵值上的那一两帧)。
 */
export function useCanvasDraft(workspaceId: string, scopeId: string) {
  const key = draftKeyFor(workspaceId, scopeId);
  const [draft, setDraft] = useState<CanvasDraft>(() => readDraft(key));

  useEffect(() => { setDraft(readDraft(key)); }, [key]);

  /**
   * 改这份草稿。
   *
   * 把弹窗那些字段一起写回默认值就是"关掉它并丢弃" —— 用户明确关掉编辑器时该这么做
   * (×、Esc、"取消")。**卸载不调它**,那是这次修复的全部要点。
   */
  const patch = useCallback((fields: Partial<CanvasDraft>) => { setDraft(writeDraft(key, fields)); }, [key]);

  return { draft, patch };
}
