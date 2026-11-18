# 聊天会话记录

聊天内可从上下文继续；用户要求保存、暂停或跨聊天恢复时，将记录写到当前工作区的独立会话文件，例如 interview-sessions/<id>.json。无文件工具时提供可复制记录，并明确未写入磁盘。避免把候选人的原文缩写成摘要后仍声称可做精确引文评分。

记录至少包含：

- session_id、mode=chat、status（active/paused/done）、options（方向、题数、用户提供材料或可读取路径）。
- plan：有序考点、来源、证据等级、难度、内部锚点。
- questions：稳定 id、主题、题面、rounds、状态；每轮 kind=main/probe/clarification、ask、answer、assessment、decision。
- 每题的追问数、澄清数、是否已给补充机会、结束原因。
- evaluations：按 question_id 保存；pending：当前待答题目及回合。

answer=null 代表尚未提交，answer="" 只代表用户明确提交空白。没有新消息不等于交白卷。保存每个已提交答案及接下来待答的问题，恢复时先读记录、核对当前位置，只重现待答问题，不重复评分已有结果。记录不完整时明确缺失的信息，不猜测历史答案。

这些是聊天模式记录，不可直接塞进 LangGraph 数据库。原程序会话必须按 runtime.md 通过 session_id 恢复，不能以此文件替代 checkpoint。纯聊天模式不承诺进程崩溃恢复、调用结果收据或后台评分。
