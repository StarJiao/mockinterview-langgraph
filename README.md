# Mock Interview · LangGraph / LangChain

一个本机运行的练手项目，和 `/Users/jiao/mockinterview` 的 Go/Eino 版独立。
固定本场考点顺序、逐题出题、动态追问、异步单题评分，最后生成 Markdown 报告。
不迁移旧会话，不扩展长期记忆、向量检索、Web 服务或部署平台。

## 快速开始

本机需 `uv`，支持 macOS/Linux。SQLite 是本地文件，不需要 Docker 或独立数据库服务。

```sh
cd /Users/jiao/mockinterview-langgraph
uv sync --locked

# 无密钥、无网络、固定题目与评分，先体验多行编辑和恢复。
uv run mockinterview new --demo --questions 1
```

输入区：

- **Enter 换行**，支持方向键移动、跨行修改、中文退格和多行粘贴。
- **先按 Esc，再按 Enter 提交**。空白答案还需输入 `y` 确认。
- **Ctrl-C / Ctrl-D 暂停**；已提交内容可恢复，未提交草稿不保存。
- 面试官青色、系统黄色；`NO_COLOR`、`TERM=dumb` 或非终端输出时不使用角色颜色。
- 非交互环境使用 `--answers` 文件，不把 EOF 当成空答案。

终端输入法候选窗由宿主终端管理；测试覆盖已输入中文的编辑、删除与粘贴，不代替在你的终端里检查输入法体验。

## 真实模型

默认出题/快评/追问使用 DeepSeek，评分使用 Qwen。可用 `.env.example` 配置新环境，
也可以**只读引用原项目的 `.env`**。本机已在 `inputs/resume.anonymized.md`
准备了一份脱敏简历（个人输入文件不进入版本管理），示例使用该副本：

```sh
uv run mockinterview new \
  --env /Users/jiao/mockinterview/.env \
  --jd /Users/jiao/mockinterview/inputs/jd.txt \
  --resume-file inputs/resume.anonymized.md \
  --state /Users/jiao/mockinterview/inputs/state.md \
  --focus 'Go 后端' --focus 'Agent 开发' --questions 3
```

`--state` 可省略；仍然是可选的薄弱点输入，不是本场运行状态。
模型和地址来自配置，shell 环境变量优先。密钥不写入 checkpoint 或报告。
真实运行会把你指定的简历/JD/回答发送给配置的模型服务；离线演示不发送。
上述 JD 和掌握状态仍是旧项目原文件，本次仅脱敏简历。脱敏副本不会改写旧会话；
要使用新资料，请新建面试，不要恢复带有原始资料的旧会话。详见 `inputs/README.md`。

```sh
uv run mockinterview sessions
uv run mockinterview resume <会话ID> --env /Users/jiao/mockinterview/.env
uv run mockinterview report <会话ID>
```

暂停/报错时会输出对应恢复命令。演示会话即使恢复时没加 `--demo` 也仍为演示；
真实会话禁止用 `--demo` 接续，避免混入固定评分。`sessions` / `report` 不需要模型密钥。
完成的会话再次 `resume` 只导出已有报告，不重新出题或评分。

自定义 `--data-dir` 后，后续命令必须指向相同目录。报告默认写入 `outputs/`。

## 代码入口

- `src/mockinterview/graph.py`：图的节点和边，没有手写恢复路由。
- `nodes.py`：节点只返回 State 更新，不直接改共享 Session。
- `state.py`：State 定义、题量/预算分支、澄清与补充机会的有限次数策略。
- `runtime.py`：持久化 checkpointer、启动/恢复、每会话单写者锁。
- `models.py` / `prompts.py` / `schemas.py`：LangChain ChatModel、面试提示词、Pydantic 与证据引用校验。
- `scoring.py` / `persistence.py`：后台评分与成功模型调用的落盘记录。
- `cli.py` / `report.py`：多行编辑、角色展示、报告。

使用 LangChain 的 `langchain-core` 和 `langchain-openai` 组件，不额外套通用 Agent 循环。
兼容端点使用 Chat Completions；JSON 本地校验，失败最多纠错一次，考点数量与题面风格另有有界校验。
文本完整生成、保存后再展示，不承诺逐 token 显示。模型请求超时由 `MODEL_TIMEOUT` 控制。

## 图与恢复机制

首次从 plan 开始，规划完成后进入 question → answer → decide。
继续追问走 probe → answer；本题结束走 finish_question → submit_score，
然后按题量与预算选择下一题或 report。report 等待包括最后一题在内的全部评分。

`answer` 调用 LangGraph `interrupt()`，此次图调用返回；终端收集完整答案后用
`Command(resume=answer)` 和相同 `thread_id` 恢复。等待输入的节点会重新进入，
因此它在 interrupt 前没有模型调用或展示副作用。出题/追问属于上一个节点，
`durability="sync"` 确保保存后才到达输入中断。

所有业务节点串行返回更新；`questions` 整体替换，`evaluations` 按题目 ID 合并，
`usage` 按调用 ID 合并，避免重复收集导致重复计数。回答的 `None` 与 `""` 语义不同。

`data/` 内有两份职责不同的 SQLite 文件：

- `checkpoints.sqlite`：LangGraph 执行位置和 State，**唯一恢复进度来源**。
- `operations.sqlite`：成功调用的结果记录、已知失败调用用量、会话 ID 索引。
  不保存第二份进度，不从这里推断下一节点。

`submit_score` 是一个很薄的技术节点：前一步先把本题 `done` 保存，再提交后台评分。
评分任务拿冻结的问答副本，不写 Graph State；结果先按题目 ID 落盘，后续节点收集合并。
进程退出时取消未完成任务，恢复时补交已经结题但没有成功结果的评分。
等待回答期间评分任务可以继续运行；**Python Task 本身没有被 checkpoint 保存**。

成功模型结果可以在节点重跑时复用；如果服务端已经返回、但本地记录尚未保存就崩溃，
仍可能重复调用。这里不承诺 exactly-once，也不自动迁移任意修改后的旧图 checkpoint。
SQLite 文件中含简历/问答，属于私人数据；恢复或备份需要保留整个 `data/`，复制前先关闭应用。

## 面试与评分策略（2026-10-10）

沿用同一张图，不增加 Agent 或节点。快评提供语义证据，代码控制次数和流转：

- `answer_kind` 区分最新回答是在作答、澄清题意，还是明确放弃；覆盖情况仍累计全部问答。
- 低覆盖（低于 50）不会立即结题，先给一次针对性的补充机会。之后若仍低覆盖且没有有效新证据，则中性结束；有新证据可在追问上限内继续。
- 核心能力已有足够有效证据时可以提前结束；明确跳过、主动提交空回答或预算耗尽时也结束。
- 每题最多 3 次正式追问，另有最多 1 次题意澄清。澄清使用 `probe` 节点的专用提示词，先回应条件，再让候选人继续回答；不泄露技术答案，也不虚构候选人的项目事实。
- 每轮保存 `kind`（main / probe / clarification）、完整快评和决策。正式追问次数由已保存回合类型统计，模型调用记录则使用实际回合序号，避免澄清和追问共用编号导致串用结果。第三次追问仍允许一次尚未用过的澄清机会。
- 追问目标描述还缺什么能力证据，不再要求引导候选人说出标准答案；取消追问 40 字限制，允许必要背景。

单题评分在一次模型调用中先列证据类别与回合引用，再给四维分数：

- 区分明确正确、明确错误、已问但未展开、未被问到、题意不清或提问带偏。
- 未问到或题意不清不能直接变成低分；证据不足的维度 `score=null`，报告显示“未评估”。有未评估维度时不计算 `/20` 总分，也不折算或填零。
- 本地校验要求引用已提交的真实回合、引用文本确实来自该回合的候选人回答；只有未问到/题意不清证据的维度不能给数字分。失败有一次纠错机会。
- 代码能检查结构、来源和范围，不能证明模型的语义分类或评分理由一定正确；仍需用真实模型样例审查。

旧回合缺少 `kind` 时按主问及普通追问解释，已保存的问答、评分和已执行决策不重写。
新的快评/追问调用记录带 v2 标识，避免复用旧格式的模型响应；尚未执行的步骤使用新策略。
要公平比较整场效果，应新建会话，而不是混合新旧策略恢复后再比较分数。

## 验证与边界

```sh
uv run pytest -q
uv run ruff check src tests

# 一场三题，每题主问 + 3 次追问，离线端到端验证。
uv run mockinterview new --demo --questions 3 --answers inputs/demo-answers.txt
```

测试使用真实 SQLite checkpoint 与固定模型替身，覆盖重启续答、追问上限、空答案、
模型失败重试、checkpoint 写入失败、后台评分重叠、评分复用/补交、并发打开保护及中文编辑。
新增测试覆盖低分补充机会、澄清不占追问额度、澄清后的跨进程恢复、调用记录不串用、
澄清结果保存失败后的复用、充分回答提前结束、未评估维度展示以及非法证据引用纠错。
`tests/test_interview_policy.py` 使用真实失败话语配合预设的快评结果验证程序分支，
不是对真实模型理解能力的验证；提示词约束测试也不等于模型语义测试通过。
离线报告明确标记演示，不代表真实能力。未自动调用付费模型做联调。

token 预算是软上限，达到后停止新题/追问，允许必要收尾和评分；已知用量会累计，
没有收到服务端 usage 的失败请求无法精确计费，报告不能替代账单。
报告仍是单题评价汇总，没有新增整场模型评价。

后续学习重点是对照 State、Reducer、条件边及原生中断恢复，不再执行原 Go 版 M3/M4 扩建计划。
