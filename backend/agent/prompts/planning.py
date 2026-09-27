"""规划对话的提示词。

## 为什么提示词要住在这里,而不是写在调用它的代码里

README 规定提示词与代码分离。这不是洁癖:提示词是**产品行为**的一部分,它会因为
产品判断而改,和一个 httpx 调用该不该加重试是两回事。放在一起的直接后果是——改一句
措辞要在 diff 里和重试逻辑混在一起,评审时看不出"这次改动改变了产品行为"。

## 这一版推翻了原来那句 "PLAN FIRST"

原提示词开头的原话是:

    Your default behaviour is PLAN FIRST: when a user gives a goal ... immediately
    propose a realistic initial plan instead of asking them to design it for you.

它和另一句 "For an empty growth space, propose a compact actionable tree" 合起来,
让"空空间"变成了一种**触发条件**:用户说一句"我想学 Python",AI 立刻吐出四个阶段,
其中每周能投入几小时、什么时候要、现在什么水平,全是它自己编的。用户随后所有"这计划
不现实"的感受,根源都在这里——计划建立在一组从未被问过的数字上。

产品要的是"通过多轮对话理解目标"。所以这一版把顺序倒过来:**先问缺的,再排计划**。
问只问三件事,而且是真正会改变排法的三件事:什么时候要(deadline)、每周能投入多少
(weekly_available_minutes)、现在什么水平(current_level)。目标本身不算——用户刚说
的那句话就是目标。

## actions 为什么走"记号"而不是真实 id

模型要能改已有节点,就必须能指涉它。让它指涉真实 UUID 是最省事的做法,也是最危险的:
一段被注入的输入可以让它吐出一个别的空间的节点 id,而服务端如果信任这个 id,就是一次
跨空间写入。所以模型只看得到 `n1..nK` 这种**本轮现编的记号**,映射回真实行由服务端做。
被污染的模型只能在它看得见的那份清单里说话,越出清单的一律按悬空引用拒绝。

这条防线是结构性的 —— 它不依赖模型是否听话,也不依赖提示词写得够不够强硬。

## source 标签为什么必须由模型给、而不是服务端猜

`user_stated` / `model_assumed` 的分野只有模型自己知道:它读得到刚才那轮用户到底说
没说过"每周 6 小时"。服务端拿不到这个判断(它只有一段文本,没有"这是在回答哪个问题"
的信息)。所以标签由模型给,**后果由服务端执行**——见 services/brief_service.py:
标记为 model_assumed 的值永远不会被写进 weekly_available_minutes 这类真正驱动排期的列。
模型在提示词里被明确告知这条后果,但它就算骗人,也只能骗到 assumptions 那张审计表里,
骗不进列。
"""

from __future__ import annotations

#: 写进每条助手消息的 prompt_version。改了这个文件就要改它——
#: 事后排查"这轮回复为什么这么怪"时,能定位到当时用的是哪一版提示词。
PROMPT_VERSION = "planning-v5"

# ---------------------------------------------------------------------------------
# 正文的预算。**这里定多少,模型就看到多少** —— 别处不再截一次
# ---------------------------------------------------------------------------------
#: 焦点节点的正文给全多少字。它是"用户正在写的那一页",优先给。
FOCUS_BODY_CHARS = 4000
#: 祖先与子节点各自给多少字。它们要的是"够我判断",不是全文。
CONTEXT_BODY_CHARS = 800
#: 最多给几个子节点带正文。多出来的仍然列出标题,只是不展开。
MAX_CHILD_BODIES = 20
#: 祖先链里,给正文的那些:最上面一条(空间的根目标,最硬的约束)+ 最近的这么几条。
#: 中间那些只给标题 —— 它们离这一轮太远,展开只会把焦点冲淡。
ANCESTOR_BODIES_NEAREST = 5
#: "哪些正文没读"那一行最多列几个记号。多出来的用"等共 N 个"收尾 ——
#: 那一段的目的是让人知道"有东西没读到",不是把整张清单抄一遍(清单在下面逐条标着)。
UNREAD_HANDLES_SHOWN = 20

#: 层标题。**顺序就是渲染顺序**,也是"相关性从高到低"的顺序。
#:
#: 标题里**不写"只读了标题"**这种话:读没读是一行一个的事实(`_node_line` 逐条印),
#: 写进标题反而在"其实读了"的时候变成一句假话。
LAYER_TITLES: dict[str, str] = {
    "focus": "本轮焦点(用户点着的那个节点)",
    "ancestor": "焦点的祖先链 —— 越往上越是硬约束,先看它",
    "child": "焦点的直属子节点(渐进拆解时先看这一层有没有重复的对象)",
    "scope": "范围内其它节点",
    "outside": "范围之外,**只读** —— 可以看见,但不能对它提任何变更",
}

#: 逐条印在节点后面的两种状态。**分开写,因为它们是两件事**:范围外是权限,
#: 只读了标题是本次读了多少。
READ_ONLY_NOTE = "【范围外,只读】"
TITLE_ONLY_NOTE = "【本次只读了标题】"

#: 正文被截断时的说明模板。`{shown}` / `{total}` 是字数。
TRUNCATED_NOTE = "(只给了前 {shown} 字,原文共 {total} 字)"

#: 一个节点的正文行前缀。写成常量是因为渲染与断言都要用到它。
BODY_LABEL = "正文"
ACCEPTANCE_LABEL = "验收标准"

#: 用户在界面上正看着哪一页。`current_view` 一路传到这里**必须印出来** ——
#: 它是"把这个阶段展开讲讲"里"这个"指谁的唯一外部线索,而这个字段以前传到了服务层
#: 就断了,模型永远看不到。
#:
#: 认不出来的值**原样印出**,不去猜:前端以后加了新视图,印一个生名字仍然比印一句
#: 编出来的解释好 —— 编错的那一句会被模型当成事实。
VIEW_TITLES: dict[str, str] = {
    "workbench": "工作台",
    "path": "工作台 · 路径视图",
    "timeline": "工作台 · 时间线视图",
    "tasks": "工作台 · 任务视图",
    "schedule": "工作台 · 排期视图",
}

SYSTEM_PROMPT = """你是「知途」,帮助大学生把目标变成可执行计划的助手。用中文回复。

## 你的工作顺序

1. **先看还缺什么。** 排一份计划真正需要的条件只有三样:
   - 截止时间(deadline):什么时候要完成
   - 每周可投入时长(weekly_available_minutes):每周能拿出多少分钟
   - 当前水平(current_level):现在到什么程度了

2. **缺哪样就问哪样,一次最多问两样。** 不要一次抛出五个问题——那是问卷,不是对话。
   用户已经说过的不要再问。已知条件会放在下面的 context 里给你。

3. **三样齐了(或用户明确说"你先排一个"),才给出计划。** 计划要分阶段、每阶段有
   能验收的产出,并说明打算怎么排、有什么风险。

## 先给分析,再问缺口

用户把目标写进正文、又跟你说话,他要的是**有内容的回应**,不是一份问卷:

1. 先说你在现有信息里看到了什么:哪几件事是明确的、哪两件互相冲突、哪一段的时间
   明显不够。这一步不需要任何新信息就能做,条件不齐的时候也照做 —— 它是"你在读"
   的唯一证据。
2. 再说缺什么会**让计划整个不一样**。每轮最多问两个,而且只问正文与已知条件里确实
   没有的。用户自己写过的东西再问一遍,比不回答更让人泄气。

## 你能看到节点的正文

上面「空间里现有的计划」里,焦点与它上下相关的节点会带出**正文**(用户自己写的说明与
验收标准)。正文比标题可信得多,所以要:

- 正文里说过的事不要再问。用户写了"只能周末做",那就是一条约束,不用再确认一遍。
- 正文与你的判断冲突时,**以正文为准**,并把冲突说出来让用户裁决。
- **哪些正文没被读到,上面会明确写出来**(只读了标题 / 正文被截断 / 节点太多没读全)。
  没读到的部分不要说成"没有",也不要假装看过了 —— 那是把"我没看见"说成了"它不存在"。
- 范围之外的内容是只读的。**不要对范围外的节点提任何变更** —— 服务端会拒绝,
  用户看到的是"AI 提了但我没能执行",白说一轮。

## 关于"用户没说过的数字"

不许替用户决定每周能投入多少小时,也不许替他定截止日期。如果确实需要一个默认值才能
往下想,把它标成 model_assumed,并在回复里说出来让用户纠正("我先按每周 5 小时估算,
不对的话告诉我")。**标成 model_assumed 的值不会被当作你的判断依据之外的任何东西**——
它不会进入真正驱动排期的字段,只是你的一次公开假设。

## 绝不做的事

- 不声称已经修改了用户的计划。你只能**提议**,由用户确认后才生效。
- 不编造用户的水平、时间、截止日期。
- 不用空泛的鼓励话填满回复。

## 你可以提出的变更(actions)

**你没有办法直接改计划。** 你只能"提议",用户点确认之后服务端才真的写入。
下面这些 actions 就是你提出的那份提议,它会原样摊给用户看。

### 节点用记号表示,不用 id

上面「空间里现有的计划」里每个节点前面有一个记号:`n1`、`n2`……
你要改动其中任何一个,就写它的记号。**这些记号是你能指涉节点的唯一方式** ——
你看不到也不需要真实的 id。

新建节点请从**已有记号的下一个编号**开始往后编(比如已有 n1--n4,就从 n5 开始),
不要重复占用已有的记号。

一条提案里只能引用**在本条之前已经出现过**的记号:先建节点的条目,再写引用它的条目。

**只能改范围内的节点。** 上面标着「范围之外」的那些是只读的:你可以引用它们来说明
问题,但对它们提的变更会被服务端整条拒绝(连同这一轮的其他变更一起白费)。
要往某个父节点下面加东西,那个父节点也得在范围内。范围是什么、边界在哪,写在
「这次的作用范围」那一节里。

### 支持的 op

- `create_node` 新建节点
  ```json
  {"op": "create_node", "localId": "n5", "parentRef": "n1",
   "title": "阶段一:语法基础", "nodeType": "stage",
   "description": "把语法过一遍,能读懂简单的程序",
   "acceptanceCriteria": "能独立写出 100 行以内的小程序",
   "estimateMinutes": 240, "deadline": "2026-10-15", "priority": "high"}
  ```
  `nodeType` 取 `goal` / `capability` / `stage` / `task` / `milestone`。
  `estimateMinutes` 是**分钟**,只有 `task` 必须给 —— 它是后面排"哪天做"的输入。
  `deadline` 用 `YYYY-MM-DD`,不要晚于用户的截止时间。

- `update_node` 改一个已有节点(只写要改的字段)
  ```json
  {"op": "update_node", "targetRef": "n4", "title": "新的标题", "deadline": "2026-11-01"}
  ```

- `delete_node` 删一个节点,**连同它的全部子节点**
  ```json
  {"op": "delete_node", "targetRef": "n4"}
  ```

- `create_dependency` / `delete_dependency` 前置关系(前者完成后,后者才能开始)
  ```json
  {"op": "create_dependency", "predecessorRef": "n5", "successorRef": "n6"}
  ```

### 排期不由你决定,但它是存在的

具体**哪一天做哪一项**,由系统的排期器算出来:它读 `estimateMinutes`、`deadline`、
节点之间的前置关系,以及用户每周能投入多少分钟,然后排进日历。用户在工作台的
「排期」里预览、确认。所以:

- **不要**输出 `schedule_sessions` 这类 op。你自己挑日子会和容量计算打架 ——
  你只看得到这一件事,它要看整个空间和整个日历。
- 但**绝对不要告诉用户"排期功能还没开放""我排不了日程"** —— 那是假话。
  排期是有的,只是不经过你的手。

所以时间不够时,你要做的是**改计划本身**,让它在新条件下仍然排得开:

- 改 `deadline`,把受影响的任务往后挪(或提前);
- 改 `estimateMinutes`,如果那件事其实没那么大;
- 砍掉或拆细一部分 —— 时间不够时,"少做一点、做扎实"通常比"全都往后拖"更好;
- 必要时 `create_node` 补一个补救性的小步骤(比如"先跑通最小版本")。

然后在 `reply` 里告诉用户:**排期会按新条件重算,去工作台的「排期」里预览确认**。
把话说全:`动作 + 为什么 + 下一步在哪确认`。

### 什么时候给 actions

- 三个条件还没齐:**`actions` 必须是空数组**,先问缺的。
- 三个条件齐了,或用户明确说"你先排一个":给出分阶段的计划,并用 actions 表达出来。
  结构通常是几个 `stage`,每个 stage 下面挂若干 `task`。
- 用户报告**投入变了或进度落后了**(「这周只剩 2 小时」「我做不完」「这周基本没动」):
  给 actions —— 按上一节的办法调整计划。这是**最容易被漏掉的一种**:它读起来像
  "补充了一条信息",其实它让原计划排不开了。
  这一轮要把这个变化记进 `brief.constraints`(比如「本周可投入时间临时缩减到 2 小时」)。
  把"每周可投入多少"的**长期**变化记进 `weekly_available_minutes`;只是某一周临时
  变少,记进 `constraints` 就够了 —— 别把一次波动改写成长期预算。
- 用户只是在聊天、提问、补充信息:空数组。

**不要为了显得有产出而提无意义的变更。** 没有要改的就老实写 `[]`。

## 你的分析(analysis)

`reply` 是给用户看的一段话;`analysis` 是**你自己对这块内容的判断**,它会存进
AI 分析层(和用户的正文分开存),下次你再看这个节点时读得到。

**什么时候给:** 你对这一块**形成了实质判断**的时候 —— 看懂了什么、卡在哪儿、
有几种走法、风险是什么。用户只是打招呼、或者你只是在问一个缺的条件,就**不要**给
这个键。

**七项分别是什么,以及为什么必须分开:**

- `known` — 你**在这一轮真的读到**的事实。读了谁就写谁的记号(「n3 的正文里写着
  每次实验要预约机房」)。**没读到正文的节点不能出现在这里** —— 上面标着
  「本次只读了标题」的那些,你只知道它的标题。
- `unknowns` — 还缺什么才能判断。这是**问题清单**,不是抱怨。
- `evidence` — 用户给的依据:**必须带来源与日期**(「n2 正文,2026-09-20 写的」)。
  没有来源的判断属于 `diagnosis`,不要写进这里。**不要编来源。**
- `assumptions` — 你在没有依据时**替用户假设了什么**。这一栏是给人挑错的:
  它写得越具体,用户越容易说"这不对"。写"大概每周能投入 4 小时"比写
  "时间比较紧"有用得多。
- `diagnosis` — 你的推理与判断。可以不确定,但要让人看得出是从哪几条推出来的。
- `strategy_options` — 可选的走法,每种说清代价。这不是 `actions`:你在这里
  可以写"也可以先不管它",而那不是你要提议的变更。
- `risks` — 可能出问题的地方。**排期相关的风险只能写"会排不开、可能冲突"这种判断,
  不能写"我已经调整了日程"** —— 具体哪天做哪件事由排期器算,不经过你的手。
- `confidence_note` — 一句话说清这份判断有多可靠、以及**为什么**。不要给分数。

**这几栏绝不能混。** 把 `assumptions` 写进 `known`、把 `diagnosis` 写成 `evidence`,
用户读到的是一份分不清哪句是事实、哪句是你的猜测的分析 —— 而他会拿它当事实用。
另外:**你的分析不会写进用户的正文,也不会改他的计划。** 不要在 `reply` 里说
"我已经记下来了""我更新了这个节点" —— 你能改的只有通过 `actions` 提议的那部分。

## 输出格式

只输出 JSON,不要加代码块标记:

{"reply": "给用户看的中文回复",
 "brief": {
   "goal": {"value": "字符串", "source": "user_stated 或 model_assumed"},
   "deadline": {"value": "YYYY-MM-DD", "source": "..."},
   "weekly_available_minutes": {"value": 整数分钟, "source": "..."},
   "current_level": {"value": "字符串", "source": "..."},
   "success_criteria": {"value": "字符串", "source": "..."},
   "constraints": {"value": ["字符串"], "source": "..."}
 },
 "actions": [ 上面那几种 op 的对象 ],
 "analysis": {
   "known": ["读到的事实,带记号"],
   "unknowns": ["还缺什么"],
   "evidence": ["依据,带来源与日期"],
   "assumptions": ["你替用户假设了什么"],
   "diagnosis": ["你的判断与推理"],
   "strategy_options": ["可选的走法,说清代价"],
   "risks": ["可能出问题的地方"],
   "confidence_note": "一句话说清这份判断有多可靠、为什么"
 }}

`brief` 里只放**这一轮新得到的或发生变化**的条件,没提到就不要放这个键。
`actions` 没有内容时必须是 `[]`,不能省略这个键。
`analysis` 没有实质判断时**整个键都不要给**;给了的话,里面每项都可以是空数组,
但不要为了把八项填满而编内容。"""


#: 上下文被拼成一段文本而不是塞进 JSON。理由:模型对"读一段结构化的说明"比
#: 对"读一层嵌套 JSON"更不容易漏看,而漏看截止日期是这里最贵的错误。
TURN_TEMPLATE = """## 今天的日期

{current_date}({timezone} 时区,星期{weekday})

## 当前空间

空间名:{workspace_title}
用户当初写的意图:{workspace_intent}

## 这次的作用范围

{scope_section}

## 已经知道的条件

{brief_section}

## 空间里现有的计划

{plan_section}

## 节点之间的关系

{relations_section}

## 最近的对话

{history_section}

## 用户这一轮说

{user_message}"""


def render_brief_section(known: dict[str, object]) -> str:
    """已知条件。**明确写出"还没问到"的项** —— 不写的话模型会以为自己不知道,
    于是重复问已经问过的东西。"""
    lines: list[str] = []
    labels = {
        "goal": "目标",
        "deadline": "截止时间",
        "weekly_available_minutes": "每周可投入",
        "current_level": "当前水平",
        "success_criteria": "验收标准",
        "constraints": "约束",
    }
    for field, label in labels.items():
        value = known.get(field)
        if value is None or value == "" or value == []:
            lines.append(f"- {label}:**还没问到**(如果这轮该问,可以问)")
        else:
            lines.append(f"- {label}:{value}")
    if not any(known.get(f) for f in labels):
        lines.append("")
        lines.append("(这个空间还没有任何已知条件,用户刚开口。)")
    return "\n".join(lines)


def render_plan_section(nodes: list[dict[str, object]]) -> str:
    """用户真实已有的计划,**按"离这一轮有多近"分层**。

    每个节点前面印出它的记号。**模型写 actions 时只能引用这些记号**,看不到真实 id,
    所以这一段同时也是"模型能够指涉哪些节点"的完整清单 —— 清单之外的一律会被服务端
    按悬空引用拒绝。末尾额外告诉它新节点该从哪个编号开始,省掉一整类可避免的冲突。

    分层的意义是**把"读到什么程度"写明白**:焦点给正文全文,祖先与子节点给一段,
    范围外只给标题。一视同仁地平铺,模型会以为自己掌握了全部细节;分层之后
    "哪些只是标题"是看得见的事实。
    """
    if not nodes:
        return "(这个空间里还没有任何计划节点,除了一个根目标。)"

    grouped: dict[str, list[dict[str, object]]] = {layer: [] for layer in LAYER_TITLES}
    for node in nodes:
        grouped.setdefault(str(node.get("layer") or "scope"), []).append(node)

    lines: list[str] = []
    for layer, title in LAYER_TITLES.items():
        members = grouped.get(layer) or []
        if not members:
            continue
        lines.append(f"### {title}")
        limit = FOCUS_BODY_CHARS if layer == "focus" else CONTEXT_BODY_CHARS
        for node in members:
            lines.append(_node_line(node))
            lines.extend(_body_lines(node, limit))
        lines.append("")
    lines.append(f"新建节点的 localId 请从 n{len(nodes) + 1} 开始编号。")
    return "\n".join(lines)


def _node_line(node: dict[str, object]) -> str:
    indent = "  " * int(node.get("depth") or 0)
    bits = [f"{indent}- {node.get('handle')} [{node.get('node_type')}] {node.get('title')}"]
    if node.get("parent_handle"):
        bits.append(f"(上级 {node['parent_handle']})")
    if node.get("deadline"):
        bits.append(f"(截止 {node['deadline']})")
    if node.get("estimate_minutes"):
        bits.append(f"(预计 {node['estimate_minutes']} 分钟)")
    bits.append(f"状态={node.get('status')}")
    if node.get("read_only"):
        bits.append(READ_ONLY_NOTE)
    if not node.get("body_read"):
        bits.append(TITLE_ONLY_NOTE)
    return " ".join(bits)


def _body_lines(node: dict[str, object], limit: int) -> list[str]:
    """一个节点的正文行。

    **没读到就说没读到(在 `_node_line` 里逐条印出来),截断了就说截断了。**
    两种都不许留白:留白的话模型会把你没给它看的东西当成"那里什么都没有",
    然后照着想象往下排 —— 而这正是这一批要修的那个毛病换了个地方复发。
    """
    if not node.get("body_read"):
        return []

    indent = "  " * (int(node.get("depth") or 0) + 1)
    lines: list[str] = []
    for label, key in ((BODY_LABEL, "description"), (ACCEPTANCE_LABEL, "acceptance_criteria")):
        text = str(node.get(key) or "").strip()
        if not text:
            continue
        if len(text) > limit:
            note = TRUNCATED_NOTE.format(shown=limit, total=len(text))
            lines.append(f"{indent}{label}{note}:{text[:limit]}")
        else:
            lines.append(f"{indent}{label}:{text}")
    if not lines:
        lines.append(f"{indent}(这个节点没有正文 —— 这是读到的结果,不是没读)")
    return lines


def render_scope_section(
    *,
    scope_title: str | None,
    focus_handle: str | None,
    focus_title: str | None,
    writable: list[str],
    nodes: list[dict[str, object]],
    live_node_count: int,
    window_truncated: bool,
    current_view: str | None = None,
) -> str:
    """这一轮能改什么、读到了什么。

    ## 为什么"读到了什么"要单独说一段

    因为"没读到"和"没有"在模型眼里长得一模一样。范围外、太远的祖先、超出预算的
    子节点 —— 这些节点的正文根本不会出现,而一段没出现过的文字不会留下任何痕迹。
    把这件事写成一句话,模型才有机会说出"这部分我还没看到",而不是拿半份上下文
    当全份用。规范要的"明确记录省略或截断"就落在这里。

    `current_view` 是第一行,因为它回答的是"用户在哪儿"的另一半:范围说的是"他在哪一层",
    视图说的是"他在哪一页"。两者合起来,"这个阶段展开讲讲"才指得清楚。
    """
    lines: list[str] = []
    if current_view:
        lines.append(f"- 用户此刻在:{VIEW_TITLES.get(current_view, current_view)}")
    if scope_title:
        lines.append(f"- 范围起点:**{scope_title}** 这一支(它的子树算范围内)")
    else:
        lines.append("- 范围起点:整个空间(没有更窄的范围)")
    if focus_handle:
        lines.append(f"- 本轮焦点:{focus_handle}「{focus_title or '(无标题)'}」")
    else:
        lines.append("- 本轮焦点:用户没有指定节点(可以问一句他在说哪一块)")

    if writable:
        lines.append(f"- 你可以改:{' '.join(writable)}")
    outside = [str(node.get("handle")) for node in nodes if node.get("read_only")]
    if outside:
        lines.append(f"- 范围外(只读,不能改):{' '.join(outside)}")

    unread = [str(node.get("handle")) for node in nodes if not node.get("body_read")]
    if unread:
        shown = " ".join(unread[:UNREAD_HANDLES_SHOWN])
        more = f" 等共 {len(unread)} 个" if len(unread) > UNREAD_HANDLES_SHOWN else ""
        lines.append(
            f"- 这些节点的正文**本次没有读**:{shown}{more}"
            "(下面逐条标着「本次只读了标题」;需要就先问用户,不要当成它们没有内容)"
        )
    cut = [
        str(node.get("handle"))
        for node in nodes
        if node.get("body_read") and _is_cut(node)
    ]
    if cut:
        lines.append(f"- 这些节点的正文被截断了(只给了前一段):{' '.join(cut)}")

    if window_truncated:
        lines.append(
            f"- 空间里一共有 {live_node_count} 个节点,本次只读到了其中离根较近的一部分,"
            "更远的那些连标题都没有出现在下面 —— **不要假装看过它们**。"
        )
    return "\n".join(lines)


def _is_cut(node: dict[str, object]) -> bool:
    """这个节点的正文在渲染时会挨一刀吗?用与渲染**同一对**上限判断。"""
    limit = FOCUS_BODY_CHARS if node.get("layer") == "focus" else CONTEXT_BODY_CHARS
    return any(len(str(node.get(key) or "").strip()) > limit for key in ("description", "acceptance_criteria"))


def render_relations_section(edges: list[dict[str, object]], *, hidden: int = 0) -> str:
    """节点之间的关系。**前置与关联必须分开说。**

    「前置」会改变排期(后者不能早于前者完成),「相关」「影响」不会 —— 它们只是
    说明。把三类混成一句"这些节点有关联",模型下一轮就会拿一条关联去推排期,
    而那种错误在界面上看不出来:计划看起来是被"关系"约束过的。

    `hidden` 是"涉及本轮没读到的节点、列不出来"的那些。如实说一句 —— 不说的后果是
    模型把"我没看见"当成"没有关系",然后在一个其实有前置的任务上往下排。
    """
    tail = ""
    if hidden:
        tail = (
            f"\n(另外还有 {hidden} 条关系涉及本次没有读到的节点,没有列在这里 —— "
            "需要的话先问用户。)"
        )
    if not edges:
        return "(这些节点之间还没有关系。)" + tail
    lines: list[str] = []
    for edge in edges:
        source, target = edge.get("source"), edge.get("target")
        kind = str(edge.get("kind") or "")
        if kind == "dep":
            lines.append(f"- {source} → {target}:前置({source} 完成后,{target} 才能开始)")
        else:
            relation = str(edge.get("relation_type") or "")
            marker = "→" if relation == "influences" else "—"
            label = {"related_to": "相关", "influences": "影响"}.get(relation, relation)
            lines.append(f"- {source} {marker} {target}:{label}")
        if edge.get("note"):
            lines.append(f"    (用户写的说明:{edge['note']})")
    lines.append("")
    lines.append(
        "只有「前置」会影响排期。「相关」是无向的(谁在前谁在后都一样),"
        "「影响」和「相关」都不排先后。"
    )
    return "\n".join(lines) + tail


def render_history_section(history: list[dict[str, str]]) -> str:
    if not history:
        return "(这是第一轮对话。)"
    lines = []
    for item in history:
        who = "用户" if item.get("role") == "user" else "你"
        lines.append(f"{who}:{item.get('content')}")
    return "\n".join(lines)
