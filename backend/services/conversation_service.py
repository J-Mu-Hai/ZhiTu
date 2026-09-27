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
from dataclasses import dataclass

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
)
from backend.services import proposal_service
from backend.services.brief_service import apply_claims
from backend.services.context import WorkspaceContext
from backend.services.errors import InvalidInput
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
) -> TurnOutcome:
    text = (content or "").strip()
    if not text:
        raise InvalidInput("消息不能是空的。")
    if len(text) > MAX_MESSAGE_CHARS:
        raise InvalidInput(f"消息太长了,请控制在 {MAX_MESSAGE_CHARS} 字以内。")

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

    # ---- 模型调用在两次提交之间。它失败不会影响上面已经落库的用户消息。----
    result = await reasoner.reason(turn)

    assistant_message = await _insert_assistant_message(db, ctx, conversation, user_message, result)
    brief, changed = await apply_claims(
        db, ctx.id, result.brief_claims, source_message_id=user_message.id
    )

    # 提案和助手消息在**同一个事务**里落地。分两次提交的话,中间那一刻库里会有一条
    # 写着"我给你排了个计划"、却找不到任何提案的回复 —— 而刷新页面正好赶上那一刻
    # 的请求,看到的就是一句没有下文的话。
    outcome = await proposal_service.build_from_actions(
        db,
        ctx,
        conversation_id=conversation.id,
        actions=result.actions,
        # **这一轮送给模型的那份记号表**,不是重新按当前库状态生成的。
        # 重新生成的话,模型说的 n3 可能已经指向了另一个节点(见 turn_context 里的注释)。
        handles=turn.node_handles,
        # 提示词里说过的范围,在这里变成一条真的检查:范围外的动作逐条被拒,
        # 落在 `proposal_errors` 里如实告诉用户。
        writable_handles=turn.writable_handles,
        # 程序性说明取自模型自己那段回复:用户点开提案卡片看到的"为什么这么排",
        # 应该和它刚才在对话里说的话是同一句,而不是系统另写的一段。
        reasoning=result.reply,
        assistant_message=assistant_message,
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
) -> Message:
    """事务 ②:落助手消息。**降级信息一并落库**,详见 models/conversation.py 的注释。"""
    message = Message(
        conversation_id=conversation.id,
        workspace_id=ctx.id,
        user_id=ctx.owner_id,
        role=MessageRole.ASSISTANT,
        content=result.reply,
        seq=user_message.seq + 1,
        context_node_id=user_message.context_node_id,
        model_source=result.source,
        degraded=result.degraded,
        degraded_reason=result.degraded_reason,
        prompt_version=result.prompt_version or None,
        model_name=result.model_name,
        latency_ms=result.latency_ms,
        usage=result.usage,
        created_at=utcnow(),
    )
    db.add(message)
    conversation.last_message_at = message.created_at
    await db.flush()
    return message


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
    message = Message(
        conversation_id=conversation.id,
        workspace_id=ctx.id,
        user_id=ctx.owner_id,
        role=MessageRole.ASSISTANT,
        content=result.reply,
        seq=await _next_seq(db, conversation.id),
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
    db.add(message)
    conversation.last_message_at = message.created_at
    await db.flush()
    return message


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
