"""将本地演示库中旧的 Python 周计划通用模板改为具体可执行任务。

仅处理标题仍是旧版“确定/练习/制作/记录/复盘/检验”模板、且所属阶段资料
明确提及 Python 的任务；不会触碰用户手写任务、非 Python 空间或完成状态。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


TASKS = (
    ("安装：Python 环境验证", "下载 Python 3，完成安装并确认终端可调用 python。", "Python 环境验证记录", "终端执行 python --version 成功，并能进入交互式解释器。", 30),
    ("编写：输入与变量脚本", "用变量、input()、print() 写读取两个数字并输出和与差的脚本。", "input_output.py", "输入两组数字均输出正确和与差，代码从空白文件独立写出。", 45),
    ("实现：if else 条件判断", "用 if / elif / else 完成分数等级或奇偶数判断。", "condition_check.py", "至少覆盖 3 个分支，边界输入能得到预期结果。", 60),
    ("实现：for while 循环", "分别用 for 和 while 写累加或九九乘法表，并使用 break/continue。", "loop_practice.py", "两个循环均可运行，能说明退出条件和 break/continue 的作用。", 75),
    ("整合：命令行小程序", "把输入、条件分支和循环组合成猜数字或菜单计算器。", "cli_mini_app.py", "程序含输入、条件判断和循环，可连续运行并正常退出。", 90),
    ("验收：脱离教程重写", "从空白文件重写一个包含输入、if else 与循环的 10–20 行脚本，并记录调试过程。", "weekly_python_check.py 与错误记录", "脚本可运行；记录至少 1 个报错、原因和修复方式。", 60),
)

OLD_PREFIXES = ("确定：", "练习：", "制作：", "记录：", "复盘：", "检验：")


def main() -> None:
    database = Path(__file__).resolve().parents[1] / "data" / "zhitu_dev.db"
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            """
            SELECT task.id
            FROM plan_nodes AS task
            JOIN plan_nodes AS week ON task.parent_id = week.id
            JOIN plan_nodes AS phase ON week.parent_id = phase.id
            JOIN workspaces AS workspace ON task.workspace_id = workspace.id
            WHERE (week.title LIKE '本周计划%' OR week.title LIKE '下周预览%')
              AND task.deleted_at IS NULL
              AND week.deleted_at IS NULL
              AND phase.deleted_at IS NULL
              -- 阶段标题可能只叫“从空白页起步”；Python 这个上下文在空间目标上。
              AND lower(workspace.title || '\n' || coalesce(workspace.intent, '') || '\n'
                        || phase.title || '\n' || coalesce(phase.description, '')) LIKE '%python%'
            ORDER BY week.created_at DESC, task.order_index ASC
            """
        ).fetchall()
        old_ids = [row[0] for row in rows if row[0] and connection.execute(
            "SELECT title FROM plan_nodes WHERE id = ?", (row[0],)
        ).fetchone()[0].startswith(OLD_PREFIXES)]
        for index, node_id in enumerate(old_ids):
            title, content, output, acceptance, minutes = TASKS[index % len(TASKS)]
            connection.execute(
                """
                UPDATE plan_nodes
                SET title = ?, description = ?, acceptance_criteria = ?, estimate_minutes = ?,
                    content_version = content_version + 1
                WHERE id = ?
                """,
                (
                    title,
                    f"动作：{title.split('：', 1)[0]}\n内容：{content}\n产出：{output}\n所属：本周计划 · Python 基础",
                    acceptance,
                    minutes,
                    node_id,
                ),
            )
        connection.commit()
        print(f"已更新 {len(old_ids)} 项旧版 Python 周任务。")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
