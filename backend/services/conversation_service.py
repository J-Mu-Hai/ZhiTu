"""一次对话轮次的完整流程。

## 两次提交,模型调用夹在中间

```
① 落库用户消息          commit   ← 到这里为止,用户说的话已经安全了
② 调模型(可能超时)
③ 落库助手消息 + 更新简报  commit
```

这个顺序是这个模块唯一重要的设计。原来的做法是"调模型 -> 成功才写库",后果是:
**模型超时的时候,用户那句话也跟着没了**。用户看到的是一个转圈然后什么都没有,
重新打一遍。而他刚才可能认真写了三行字描述自己的情况。

把用户消息先提交,那三行字就在库里。模型失败时接口如实返回"这次没生成成功,可以重试",
用户点重试,`clientMessageId` 让系统认出"这条已经存过了",直接拿它重跑模型 ——
不会产生第二条一模一样的消息。

## 幂等靠的是 clientMessageId,不是"猜"

重试有两种情形,必须分开处理:

- 用户消息已存 + 助手回复已存  -> 这是**重复提交**,原样返回上次的结果(`replayed=True`)
- 用户消息已存 + 助手回复没有  -> 这是**上次失败了在重试**,拿这条用户消息重跑模型

第二种情形如果也当成"重复"直接返回,用户就永远卡在失败那一轮上 —— 重试按钮点了
没有任何效果。所以判断依据是"助手有没有回过",不是"用户消息在不在"。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.agent.runtime.base import Reasoner, ReasoningResult
from backend.contracts.proposal import ActionError
from backend.db.base import utcnow
from backend.db.models import Conversation, Message, PlanningBrief, Proposal
from backend.db.models.enums import (
    ConversationKind,
    ConversationStatus,
    MessageRole,
    ModelSource,
    PlanningWorkflowStage,
)
from backend.services import (
    agent_loop_service,
    analysis_service,
    node_service,
    proposal_service,
    question_service,
    strategy_review,
)
from backend.services.brief_service import apply_claims
from backend.services.context import WorkspaceContext
from backend.services.errors import InvalidInput
from backend.services.input_snapshot import InputSnapshot
from backend.services.turn_context import build_turn_context

logger = logging.getLogger(__name__)

#: 用户单条消息的长度上限。与前端 textarea 的 maxLength 一致,
#: 服务端这一层才是真正的边界 —— 前端的 maxLength 只是提示。
MAX_MESSAGE_CHARS = 4000

#: seq 冲突时的重试次数。冲突只可能来自同一空间的两个并发请求,
#: 正常用不会有;重试一次足够,再多就是在掩盖真正的 bug。
_SEQ_RETRIES = 3

#: 一次取多少条对话。超出部分靠 `truncated` 如实告知,不静默截断。
MESSAGE_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class MessagePage:
    """这个空间的一段对话消息,按时间正序。

    `truncated` 是页面必须知道的一件事:它为真时,列表里的第一条**不是**这场
    对话的开头。不说的话,用户看到的第一句话会是一句没头没尾的"好的,那我按
    每周 4 小时排"。
    """

    messages: list[Message]
    truncated: bool


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    user_message: Message
    assistant_message: Message
    result: ReasoningResult
    brief: PlanningBrief | None
    changed_fields: tuple[str, ...]
    replayed: bool

    #: 这一轮产生的提案。**还没有生效** —— 要等用户点确认。
    #: None 表示模型这一轮没提任何变更(绝大多数轮次都是这样)。
    proposal: Proposal | None = None
    #: 模型提了变更、但没能通过校验时,逐条的问题。
    #:
    #: 与 `proposal=None` 且这里为空是**两件不同的事**:前者是"这次说清楚了要改什么,
    #: 改不了",后者是"这次没打算改"。界面上前者要如实说出来,后者什么都不用显示 ——
    #: 把"AI 提了但我没执行"吞掉,用户会以为他说的调整已经生效了。
    proposal_errors: tuple[ActionError, ...] = ()

    #: **模型回答期间用户改了输入**,于是这一轮的结论建立在一份已经过去的输入上。
    #:
    #: 它和 `proposal_errors` 是两件事:这里的问题不在模型提了什么,而在**它看的
    #: 东西已经旧了**。所以界面要说的不是"这几条没执行",而是"这段分析基于改动前的
    #: 内容,重新分析一次才有用"。带上这个标记是因为界面上必须给得出那个入口 ——
    #: 只把提案变成空,用户看到的是模型说了半天、什么都没有,而原因无从得知。
    input_changed: bool = False


async def find_primary_conversation(db, ctx: WorkspaceContext) -> Conversation | None:
    """只查,不建。

    **读接口必须走这一条。** 如果 `GET /messages` 用下面那个"没有就建"的版本,
    那么用户只要打开一次工作台,库里就会多出一条他从没说过话的对话 —— 而"新空间是空的"
    恰恰是这一阶段要保证的事。读操作有副作用这件事不会报错,只会让计数悄悄地不对。
    """
    result = await db.execute(
        select(Conversation).where(
            Conversation.workspace_id == ctx.id,
            Conversation.kind == ConversationKind.PRIMARY,
            Conversation.status == ConversationStatus.ACTIVE,
        )
    )
    return result.scalar_one_or_none()


async def get_or_create_primary_conversation(db, ctx: WorkspaceContext) -> Conversation:
    """取这个空间的主对话,没有就建一条。**只有写路径可以调它。**

    **建空间时不会预置对话** —— 主对话在用户第一次说话时诞生。这是"新空间默认为空"
    的一部分:用户打开一个刚建的空间,对话区应该是空的,而不是已经有一句他没说过的话。
    """
    existing = await find_primary_conversation(db, ctx)
    if existing is not None:
        return existing

    conversation = Conversation(
        user_id=ctx.owner_id,
        workspace_id=ctx.id,
        kind=ConversationKind.PRIMARY,
        title="主对话",
        status=ConversationStatus.ACTIVE,
    )
    db.add(conversation)
    try:
        await db.flush()
    except IntegrityError:
        # 同一空间的两个请求同时首次发言。唯一索引挡住了第二条,回读第一条即可 ——
        # 用户看到的结果和顺序执行完全一样。
        await db.rollback()
        result = await db.execute(
            select(Conversation).where(
                Conversation.workspace_id == ctx.id,
                Conversation.kind == ConversationKind.PRIMARY,
                Conversation.status == ConversationStatus.ACTIVE,
            )
        )
        conversation = result.scalar_one()
    return conversation


async def _next_seq(db, conversation_id: uuid.UUID) -> int:
    current = await db.scalar(
        select(func.max(Message.seq)).where(Message.conversation_id == conversation_id)
    )
    return int(current or 0) + 1


async def _append_message_with_retry(db, conversation, build) -> Message:
    """给一条助手消息分配 seq 并落库,**撞 seq 时重试**。

    ## 为什么需要重试

    同一段对话**可能有多个写入者**:用户发消息/回答问题(`submit_turn`)、复盘与目标
    推理的追加回复(`append_reply`)。它们各自算 seq,而 `(conversation_id, seq)` 是唯一
    约束 —— 撞上时 SQLite 抛 `IntegrityError`,未处理的异常在跨域请求里会丢掉 CORS 头,
    浏览器把它读成“连不上后端服务”,而真正的原因是一次 seq 冲突。

    这里用 savepoint **只回滚这一条插入**,重算 seq 再试;调用方在此前写下的其它东西
    (推理状态、工具记录、地图节点)不受影响。
    """
    last_error: IntegrityError | None = None
    for _ in range(_SEQ_RETRIES):
        seq = await _next_seq(db, conversation.id)
        try:
            async with db.begin_nested():
                message = build(seq)
                db.add(message)
                await db.flush()
        except IntegrityError as exc:
            last_error = exc
            continue
        conversation.last_message_at = message.created_at
        return message
    raise last_error  # type: ignore[misc]


async def _find_user_message(
    db, conversation_id: uuid.UUID, client_message_id: str | None
) -> Message | None:
    if not client_message_id:
        return None
    result = await db.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.client_message_id == client_message_id,
        )
    )
    return result.scalar_one_or_none()


async def _find_reply_after(db, conversation_id: uuid.UUID, seq: int) -> Message | None:
    """紧跟在 seq 之后的那条消息。

    `seq` 是 (conversation_id, seq) 唯一约束保证的严格顺序,所以"下一条"是确定的。
    这里不再往后多找:阶段 3 的对话里只有用户与助手两种消息,下一条不是助手回复,
    就说明上一轮确实没写完。
    """
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.seq > seq)
        .order_by(Message.seq.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def find_reply_after(
    db, conversation_id: uuid.UUID, seq: int
) -> Message | None:
    """`_find_reply_after` 的公开入口。

    阶段 12 的 intake 回答路径要自己判断“这条用户消息是不是已经处理过”,
    用它做幂等重放;把私有函数公开出来,而不是让别的模块去访问下划线。
    """
    return await _find_reply_after(db, conversation_id, seq)


def _snapshot_for_analysis(
    *,
    snapshot: InputSnapshot,
    brief: PlanningBrief | None,
    input_changed: bool,
) -> InputSnapshot:
    """交给分析记录的那份快照:把"这一轮自己记下的条件"从基线里挪走。

    ## 为什么不能直接用 `turn.input_snapshot`

    那份快照是模型**读之前**取的(`build_turn_context` 里),而 `apply_claims` 在它
    之后才把这一轮读出的条件落到简报上 —— `brief.version` 与
    `weekly_available_minutes` 两个都在快照里。原样存下去的话,一次"一边给判断、
    一边记条件"的回合会让它**刚给出的分析一出生就报过期**,理由是「已知条件变了」,
    而用户什么都没改。这不是假想的边角:**"我每周能投 10 小时,帮我拆一下"正是最
    常见的那一轮**(见 `test_the_models_own_claims_do_not_kill_its_own_proposal`)。

    那一条用例修的是同一个缺陷的另一半 —— 它把 `input_changed` 的比较挪到了
    `apply_claims` **之前**。而过期是**读的时候**现算的(存下的快照 vs 此刻的库),
    借不到那次挪动,只能在**存**的时候把这一轮自己的写入挪出基线。

    ## 为什么只在 `input_changed` 为假时才挪

    这一轮自己记了哪些条件,库里没有单独的账;`apply_claims` 返回的是"现在的简报",
    它**同样包含别人在这一轮期间写进去的东西**。所以无条件地拿它当基线,会把
    "用户同时在另一个标签页改了每周时长"这件事一起抹掉 —— 那正是必须报出来的。

    `input_changed` 恰好是那个判据:它在 `apply_claims` 之前比过一次,为假就说明
    这一轮期间**没有别人的写入**,此刻简报与快照的差全部来自模型自己。

    为真时原样保留旧快照:那份分析本来就要标成"基于旧版本",而别人的写入必须继续
    留在可见的差异里。代价是那种情况下会多出一条「已知条件变了」—— 那时的过期标记
    本来就已经是对的,而多一句不一定准的理由,比在**最常见的那一轮**上说一句
    假的"用户改了口径"要好得多。
    """
    if input_changed or brief is None:
        return snapshot
    return replace(
        snapshot,
        brief_version=brief.version,
        weekly_available_minutes=brief.weekly_available_minutes,
    )


async def submit_turn(
    db,
    ctx: WorkspaceContext,
    reasoner: Reasoner,
    *,
    content: str,
    client_message_id: str | None = None,
    context_node_id: uuid.UUID | None = None,
    current_view: str | None = None,
    scope_root_id: uuid.UUID | None = None,
    trigger: str = "user_message",
    #: 阶段 11:这一轮是在回答 **intake 关键问题**。它只用来更新简报,不生成画布问题、
    #: 也不提具体计划变更 —— 战略还没收敛。
    intake_mode: bool = False,
) -> TurnOutcome:
    text = (content or "").strip()
    if not text:
        raise InvalidInput("消息不能是空的。")
    if len(text) > MAX_MESSAGE_CHARS:
        raise InvalidInput(f"消息太长了,请控制在 {MAX_MESSAGE_CHARS} 字以内。")

    # **先确认这个节点真的是这个空间的,再决定要不要处理这条消息。**
    #
    # 顺序是这一段存在的全部理由。`context_node_id` 来自请求体,而它一路都被当成
    # 事实在用:它会被写进 `messages.context_node_id` 那一行、决定这一轮的范围与焦点、
    # 还决定分析记录的 `focusNodeId`。
    #
    # 不校验的后果不是崩溃,是**静默错位** —— 这才是它难被发现的原因。`load_scope`
    # 只把落在范围内的焦点认下来,范围外的**悄悄当成没给**(见那里的注释:它的职责是
    # 防止"上一处的选中"被当成"这一轮在聊什么",不是鉴权)。于是拿别人空间的节点 id
    # 发消息,拿到的是 200、一条记着那个 id 的用户消息、和一轮**没有焦点**的对话:
    # 没有任何迹象说明那个 id 被丢掉了。用户以为自己说的是那个节点,模型以为他没指。
    #
    # 所以这里不交给 `load_scope` 兜底:**兜底的语义是"忽略",而正确的语义是"拒绝"**。
    # 拿 `scope_root_id` 比一下就更清楚 —— 那个 id 给错了是 `InvalidInput`,因为
    # "基于我没读到的东西去做范围判断,才是真正危险的那件事"。
    #
    # **外键不是这道校验的替代品,这一点值得写下来**:`messages.context_node_id` 上
    # 确实有个 `ForeignKey("plan_nodes.id")`,但它拦的只有"这个 id 从没存在过"那一种 ——
    # 而且拦下来的方式是在 `_insert_user_message` 里抛 IntegrityError,被那一层的重试
    # 循环当成 seq 冲突再试几次,最后从一个错误路径上炸出去。**跨空间和已归档的 id
    # 外键是拦不住的**:那些行真的在 `plan_nodes` 里(归档只是打 `deleted_at`),
    # 于是写入成功、响应 200、id 原样回显 —— 一条记着别的空间节点的用户消息就这么
    # 落了库。外键管的是"这一行在不在",管不了"这一行属不属于你"。
    #
    # 归属条件写在 `load_node` 的 WHERE 里(`workspace_id` 不匹配就查不到),所以
    # 跨用户、跨空间、不存在、已归档这四种情况在这里是**同一个** `NodeNotFound`。
    # 分开返回的话,拿 id 逐个试就能问出"这个 id 有没有存在过"(理由见 `NodeNotFound`)。
    # 也正因为条件在 WHERE 里,非法的 id **一行都取不出来** —— 别人节点的正文
    # 在这一步没有被读到,不是因为"读完再判断",而是因为它根本没进过查询结果。
    #
    # **位置在两次提交之前。** 上面只有纯文本校验(不碰库);下面
    # `get_or_create_primary_conversation` 就会建对话、`_insert_user_message` 会写消息、
    # `build_turn_context` 会把节点正文读进提示词、`reasoner.reason` 会调模型。
    # 非法请求在这里停住,四件事一件都不会发生。
    if context_node_id is not None:
        await node_service.load_node(db, ctx, context_node_id)

    # 规划智能体重构 V1(P1):阶段一“初步思考”与节点讨论**不走普通规划回合**,也不
    # 触发模型。它只写 reasoning 层的分析节点,不生成任务 / 时间线 / 周计划。
    from backend.services import (  # 延迟 import,避免循环依赖
        reasoning_service,
        v1_service,
    )

    v1_session = await reasoning_service.get_session(db, ctx)
    if (
        v1_service.is_v1(v1_session)
        and v1_session.v1_stage == v1_service.V1_INITIAL_THINKING
    ):
        # 只有“初步思考”这一档拦截;提交之后 V1 空间回到普通对话节点讨论。
        return await v1_service.answer_v1_in_conversation(
            db,
            ctx,
            v1_session,
            content=text,
            client_message_id=client_message_id,
            context_node_id=context_node_id,
        )

    # 阶段 12:战略 intake 正在等回答时,用户这条消息**就是那轮回答**。
    # 交给推理服务在 `strategic_intake` 模式下处理 —— 它只问下一个关键问题,
    # 不创建任何 `agent_questions` / 画布问题节点,也不走普通规划回合。
    if await reasoning_service.is_awaiting_intake_answer(db, ctx):
        return await reasoning_service.answer_intake_in_conversation(
            db,
            ctx,
            reasoner,
            content=text,
            client_message_id=client_message_id,
            context_node_id=context_node_id,
        )

    # 规划智能体 V0.1:DISCOVERY 阶段里用户这条消息**就是那次回答**。
    from backend.services import v01_service  # 延迟 import,避免循环依赖

    v01_session = await reasoning_service.get_session(db, ctx)
    if (
        v01_service.is_v01(v01_session)
        and v01_session.workflow_stage is PlanningWorkflowStage.DISCOVERY
    ):
        return await v01_service.answer_v01_in_conversation(
            db,
            ctx,
            v01_session,
            content=text,
            client_message_id=client_message_id,
            context_node_id=context_node_id,
        )

    conversation = await get_or_create_primary_conversation(db, ctx)

    user_message = await _find_user_message(db, conversation.id, client_message_id)
    if user_message is not None:
        existing_reply = await _find_reply_after(db, conversation.id, user_message.seq)
        if existing_reply is not None and existing_reply.role is MessageRole.ASSISTANT:
            # 重复提交。**不重新调模型** —— 那会花两份钱,而且模型每次回答都不一样,
            # 用户会看到同一句话得到两个不同回复。
            #
            # 上次那份提案要一起带回去。不带的话,用户双击发送(或者网络超时后重试)
            # 会看到回复还在、提案卡片却没了 —— 而回复里正说着"我列了个计划,你看一下"。
            return TurnOutcome(
                user_message=user_message,
                assistant_message=existing_reply,
                result=_result_from_stored(existing_reply),
                brief=None,
                changed_fields=(),
                replayed=True,
                proposal=await _proposal_of_reply(db, ctx, existing_reply),
            )
        # 走到这里说明上次模型调用失败了。用已存的这条用户消息重跑。
        logger.info("重试上一轮失败的对话: message_id=%s", user_message.id)
    else:
        user_message = await _insert_user_message(
            db, ctx, conversation, text, client_message_id, context_node_id
        )

    turn = await build_turn_context(
        db,
        ctx,
        conversation_id=conversation.id,
        user_message=user_message.content,
        exclude_message_id=user_message.id,
        context_node_id=context_node_id,
        current_view=current_view,
        scope_root_id=scope_root_id,
    )

    # ---- 有界工具循环在两次提交之间。它失败不会影响上面已经落库的用户消息。----
    # 循环里可能调用只读工具、把结果回填给模型;**中间轮的 actions 不会进 proposal**,
    # 只有这里返回的最终那一份会。
    loop = await agent_loop_service.run_turn(
        db,
        ctx,
        reasoner,
        turn=turn,
        source_message_id=user_message.id,
        context_node_id=context_node_id,
        trigger=trigger,
    )
    result = loop.result

    # **模型刚回来,先比一次输入 —— 而且必须在这一轮自己写库之前比。**
    #
    # 上面那次调用可能花了几十秒,这期间用户完全可能改了正文、加了子节点、调了每周
    # 可投入的时长 —— 那种情况下模型这段话说的已经不是现在的事,它不能再变成一份
    # "点了确认就能生效"的提案。规范 §2.3:这种结果"只能作为标有「基于旧版本」的
    # 历史分析,不冒充最新判断,也不能直接成为可应用提案"。所以这里不是提醒,
    # 是**不生成提案**。
    #
    # **为什么位置这么讲究:** 紧接着的 `apply_claims` 会改 `brief.version` 和
    # `weekly_available_minutes`,这两个都在快照里。把这次比较放到它后面的话,
    # 模型**自己这一轮记下的条件**会被读成"用户在它回答时改了东西",于是每一个
    # "一边记条件一边提方案"的回合都会自我作废 —— 而那正是最常见的那个回合
    # (用户说"我每周能投 10 小时,帮我拆一下")。要量的是**别人的写入**,
    # 窗口就该是"建完上下文 → 模型返回",这一轮之后写的任何东西都不在其中。
    #
    # 这一步也不能挪到确认那一步去做:等到用户点确认才拦,他已经读完一份基于旧数据的
    # 建议了。而确认时那道版本校验仍然保留 —— 它管的是另一段时间(提案生成到用户
    # 点确认),两段都要有,少一段就有一段窗口是空的。
    input_changed = await analysis_service.input_changed_since(db, ctx, turn.input_snapshot)

    assistant_message = await _insert_assistant_message(
        db, ctx, conversation, user_message, result, research=loop.research
    )
    brief, changed = await apply_claims(
        db, ctx.id, result.brief_claims, source_message_id=user_message.id
    )

    if result.analysis is not None:
        # 分析只增不改,重新分析一次不会抹掉上一次 —— 用户常问的是"它上次为什么
        # 那么说",那需要上一次的原话还在。
        await analysis_service.record(
            db,
            ctx,
            conversation_id=conversation.id,
            message_id=assistant_message.id,
            # **不是 `turn.input_snapshot`。** 见 `_snapshot_for_analysis`:
            # 那一份是模型**读之前**取的,而刚刚那次 `apply_claims` 又改了它 ——
            # 照原样存下去,这一轮自己的写入会被下一次读读成"用户后来改了口径"。
            snapshot=_snapshot_for_analysis(
                snapshot=turn.input_snapshot, brief=brief, input_changed=input_changed
            ),
            draft=result.analysis,
            prompt_version=result.prompt_version,
            # 降级时 `source` 是 `rule_fallback` / `unavailable` —— 那段话不是模型
            # 说的,而分析这一栏必须让人分得出来。
            model_source=result.source,
        )

    # 提案和助手消息在**同一个事务**里落地。分两次提交的话,中间那一刻库里会有一条
    # 写着"我给你排了个计划"、却找不到任何提案的回复 —— 而刷新页面正好赶上那一刻
    # 的请求,看到的就是一句没有下文的话。
    #
    # **输入变过时不走 `build_from_actions`。** 但动作也不能就这么丢掉:模型确实提了、
    # 而它们确实没有执行(理由见下面的 `INPUT_CHANGED`),那正是这个模块开头说的
    # "把'AI 提了但我没执行'吞掉,用户会以为他说的调整已经生效了"。所以每条动作
    # 配一条错误,原样交给界面。
    if input_changed:
        outcome = proposal_service.ProposalOutcome(
            proposal=None, errors=_input_changed_errors(result.actions)
        )
    elif intake_mode:
        # 阶段 11:intake 期间**不提具体计划变更**。模型可能把答案当成“可以拆任务了”,
        # 但战略还没收敛;静默掉这些动作,只保留简报更新。
        outcome = proposal_service.ProposalOutcome(proposal=None)
    else:
        outcome = await proposal_service.build_from_actions(
            db,
            ctx,
            conversation_id=conversation.id,
            actions=result.actions,
            # **这一轮送给模型的那份记号表**,不是重新按当前库状态生成的。
            # 重新生成的话,模型说的 n3 可能已经指向了另一个节点 (见 turn_context 里的注释)。
            handles=turn.node_handles,
            # 提示词里说过的范围,在这里变成一条真的检查:范围外的动作逐条被拒,
            # 落在 `proposal_errors` 里如实告诉用户。
            writable_handles=turn.writable_handles,
            # 程序性说明取自模型自己那段回复:用户点开提案卡片看到的"为什么这么排",
            # 应该和它刚才在对话里说的话是同一句,而不是系统另写的一段。
            reasoning=result.reply,
            assistant_message=assistant_message,
        )
    # 问题节点与提案在**同一个事务**里落地。它们分开:问题落库即成卡片,
    # 不需要任何确认;提案仍然是待用户确认的。
    #
    # **输入变过时不建问题。** 问题也是模型基于当时上下文提出的,旧轮次的它同样
    # 可能问错东西;与提案保持同一条判断。
    # **intake 期间也不在这里建问题** —— intake 的关键问题由 `reasoning_service`
    # 落成 `conversation_intake`,不允许这里再造出画布 Question Node。
    if not input_changed and not intake_mode:
        # 战略阶段 = 还没有已确认的战略。这一阶段的问题**不先问排期条件**。
        has_strategy = await strategy_review.has_strategy(db, ctx)
        await question_service.create_from_drafts(
            db,
            ctx,
            result.questions,
            source_message_id=assistant_message.id,
            source_node_id=context_node_id,
            strategy_phase=not has_strategy,
        )
        # 战略复评触发器:**只创建一张问题卡,不改任何战略节点。**
        # 用户明确改了长期条件、或连续多周期失败时才问一次;
        # 单次延期/单日失败不该走到这里(理由见 `strategy_review`)。
        await strategy_review.maybe_create_review_question(
            db,
            ctx,
            changed_fields=changed,
            source_message_id=assistant_message.id,
        )
    await db.commit()

    return TurnOutcome(
        user_message=user_message,
        assistant_message=assistant_message,
        result=result,
        brief=brief,
        changed_fields=changed,
        replayed=False,
        proposal=outcome.proposal,
        proposal_errors=outcome.errors,
        input_changed=input_changed,
    )


#: 输入变过时那几条错误的码。**不是校验错误** —— 校验根本没跑,
#: 所以它不该混在 `OUT_OF_SCOPE` / `DANGLING_PROPOSAL_REF` 那一类里被当成"模型写错了"。
INPUT_CHANGED = "INPUT_CHANGED"

_INPUT_CHANGED_MESSAGE = (
    "这一条没有执行:你在它回答之前改了正文或结构,这段建议是基于改动前的内容做出的。"
    "让它根据最新内容重新分析一次,就能拿到现在成立的那一版。"
)


def _input_changed_errors(actions: tuple[dict, ...]) -> tuple[ActionError, ...]:
    """每条被提出的动作配一条"没执行,因为输入变了"。

    **一条都不漏。** 只报一句总述的话,界面上那份"这次打算改 5 处"和错误数就对不上,
    而用户会以为剩下那几条已经生效了 —— 那正是这一整段要防的事。
    """
    return tuple(
        ActionError(ordinal=index, code=INPUT_CHANGED, message=_INPUT_CHANGED_MESSAGE)
        for index, _ in enumerate(actions, start=1)
    )


async def _insert_user_message(
    db,
    ctx: WorkspaceContext,
    conversation: Conversation,
    text: str,
    client_message_id: str | None,
    context_node_id: uuid.UUID | None,
) -> Message:
    """事务 ①:把用户消息落库并提交。

    先提交是刻意的 —— 见模块开头。提交之后哪怕模型调用把进程拖垮,
    这句话也在库里,重试时还能找回来。
    """
    last_error: IntegrityError | None = None
    for _ in range(_SEQ_RETRIES):
        message = Message(
            conversation_id=conversation.id,
            workspace_id=ctx.id,
            user_id=ctx.owner_id,
            role=MessageRole.USER,
            content=text,
            seq=await _next_seq(db, conversation.id),
            context_node_id=context_node_id,
            client_message_id=client_message_id,
            created_at=utcnow(),
        )
        db.add(message)
        conversation.last_message_at = message.created_at
        try:
            await db.commit()
            return message
        except IntegrityError as exc:
            # 两种可能:seq 撞了(并发),或者 client_message_id 撞了(重复提交)。
            last_error = exc
            await db.rollback()
            existing = await _find_user_message(db, conversation.id, client_message_id)
            if existing is not None:
                return existing
    # 重试若干次仍然冲突:这不是"再试一次就好"的情况,让它以原貌暴露。
    raise last_error  # type: ignore[misc]


async def _proposal_of_reply(
    db, ctx: WorkspaceContext, reply: Message
) -> Proposal | None:
    """这条助手回复当时带来的那份提案。

    **不重新校验、不重新生成。** 提案可能已经过期、被拒绝、甚至已经被别的提案取代,
    那些都不影响这里要做的事 —— 重放一条历史回复时该显示的,是它当时带的东西,
    而不是按现在的数据算出来的一份新提案(那就成了一个没人确认过的惊喜)。
    """
    if reply.proposal_id is None:
        return None
    result = await db.execute(
        select(Proposal).where(Proposal.id == reply.proposal_id, Proposal.workspace_id == ctx.id)
    )
    return result.scalar_one_or_none()


async def _insert_assistant_message(
    db,
    ctx: WorkspaceContext,
    conversation: Conversation,
    user_message: Message,
    result: ReasoningResult,
    *,
    research: dict | None = None,
) -> Message:
    """事务 ②:落助手消息。**降级信息一并落库**,详见 models/conversation.py 的注释。"""

    def build(seq: int) -> Message:
        return Message(
            conversation_id=conversation.id,
            workspace_id=ctx.id,
            user_id=ctx.owner_id,
            role=MessageRole.ASSISTANT,
            content=result.reply,
            # **不写死 `user_message.seq + 1`**:并发写入者可能已经占用了那个号。
            # `_append_message_with_retry` 会重算真正的下一个号并重试冲突。
            seq=seq,
            context_node_id=user_message.context_node_id,
            model_source=result.source,
            degraded=result.degraded,
            degraded_reason=result.degraded_reason,
            prompt_version=result.prompt_version or None,
            model_name=result.model_name,
            latency_ms=result.latency_ms,
            usage=result.usage,
            research=research,
            created_at=utcnow(),
        )

    return await _append_message_with_retry(db, conversation, build)


async def append_reply(
    db,
    ctx: WorkspaceContext,
    *,
    conversation: Conversation,
    result: ReasoningResult,
    proposal_id: uuid.UUID | None = None,
) -> Message:
    """追加一条**不是由用户那句话触发**的助手回复。

    ## 为什么需要单独一个入口

    `submit_turn` 那条路径假定"一定有一条用户消息在前面"—— 它的 `seq` 是
    `user_message.seq + 1`。复盘不是这样:用户点的是界面上那个「按执行情况调整」按钮,
    他没有说话。硬套那条路径的话,要么会凭空造一条用户消息(库里会出现一句他从没说过
    的话),要么 seq 会撞上。

    复盘那条路径上模型的回复**必须留下来**:它解释了为什么建议这么调整,而用户点开
    提案卡片看到的"理由"应该是它当时真的说过的那一句。
    """

    def build(seq: int) -> Message:
        return Message(
            conversation_id=conversation.id,
            workspace_id=ctx.id,
            user_id=ctx.owner_id,
            role=MessageRole.ASSISTANT,
            content=result.reply,
            seq=seq,
            model_source=result.source,
            degraded=result.degraded,
            degraded_reason=result.degraded_reason,
            prompt_version=result.prompt_version or None,
            model_name=result.model_name,
            latency_ms=result.latency_ms,
            usage=result.usage,
            proposal_id=proposal_id,
            created_at=utcnow(),
        )

    return await _append_message_with_retry(db, conversation, build)


def _result_from_stored(message: Message) -> ReasoningResult:
    """把库里那条助手消息还原成 ReasoningResult,用于重复提交的返回。

    注意 `brief_claims` 是空的:重复提交不重新解析条件,简报在上次就已经写过了。
    重新解析会把它再写一遍审计,同一个值出现两条记录,看起来像用户改过主意。
    """
    return ReasoningResult(
        # model_source 理论上不会是 NULL(助手消息一定会写入来源),但库里可能因为
        # 手工修数据而出现 NULL。退回 UNAVAILABLE 而不是编一个来源 ——
        # 界面显示"来源不明"比显示一个错误的来源好。
        reply=message.content,
        source=message.model_source or ModelSource.UNAVAILABLE,
        degraded=message.degraded,
        degraded_reason=message.degraded_reason,
        retryable=False,
        request_id="",
        prompt_version=message.prompt_version or "",
        model_name=message.model_name,
        latency_ms=message.latency_ms,
        usage=message.usage,
    )


async def record_user_message(
    db,
    ctx: WorkspaceContext,
    *,
    conversation: Conversation,
    text: str,
    client_message_id: str | None,
    context_node_id: uuid.UUID | None,
) -> Message:
    """把一条用户消息落库。**与 `submit_turn` 内部走的是同一条路径。**

    阶段 12 的 intake 回答要自己先落库(它绕过普通规划回合),所以把这一条
    公开出来,而不是让别的模块去调私有函数。
    """
    return await _insert_user_message(
        db, ctx, conversation, text, client_message_id, context_node_id
    )


def turn_outcome_for_reply(
    *,
    user_message: Message,
    assistant_message: Message,
    brief: PlanningBrief | None,
) -> TurnOutcome:
    """构造一个“已经有一条落库的助手回复”的回合结果。

    战略 intake 回答路径用它把推理服务的产物包装成与发消息一致的形状。
    模型降级/失败标记从助手消息上读回(`_result_from_stored`),所以界面
    仍然能显示可重试的错误。
    """
    return TurnOutcome(
        user_message=user_message,
        assistant_message=assistant_message,
        result=_result_from_stored(assistant_message),
        brief=brief,
        changed_fields=(),
        replayed=False,
    )


async def list_messages(
    db, ctx: WorkspaceContext, *, limit: int = MESSAGE_PAGE_SIZE
) -> MessagePage:
    """取这个空间**最近**的这些条消息,按时间正序返回。没有对话就是空列表,不建空对话。

    ## 为什么取最近,而不是最早

    这里原来是 `order_by(seq.asc()).limit(100)` —— 取的是**最旧**的 100 条。
    对话一旦超过 100 条,用户新说的话就再也不会出现在界面上:他发了一条,页面
    刷新后它不见了。这是这类错里最坏的一种,因为它看起来像"我这条没发出去"。
    实测里它还会连带藏掉提案 —— 提案是挂在某条助手消息上的,而那条消息一旦
    落在窗口外,用户就看不到 AI 刚提的那份调整。

    ## 为什么要多取一条

    `limit + 1` 是唯一能确定"还有更早的"的办法,不必再发一次 count 查询。
    多出来的那一条不返回,只用来把 `truncated` 置真。
    """
    result = await db.execute(
        select(Message)
        .where(Message.workspace_id == ctx.id)
        .order_by(Message.seq.desc())
        .limit(limit + 1)
    )
    rows = list(result.scalars())
    truncated = len(rows) > limit
    del rows[limit:]
    rows.reverse()
    return MessagePage(messages=rows, truncated=truncated)


__all__ = [
    "MAX_MESSAGE_CHARS",
    "MESSAGE_PAGE_SIZE",
    "MessagePage",
    "TurnOutcome",
    "append_reply",
    "find_primary_conversation",
    "get_or_create_primary_conversation",
    "list_messages",
    "submit_turn",
]
