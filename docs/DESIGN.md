# 设计说明

README 是门面，这里是实现细节、取舍理由和验证记录。按主题分节，不需要顺序读。

## 1. 图与恢复机制

首次从 `plan` 开始，规划完成后进入 `question → answer → decide`。
继续追问走 `probe → answer`；本题结束走 `finish_question → submit_score`，
然后按题量与预算选择下一题或 `report`。`report` 等待包括最后一题在内的全部评分；
评分失败不会阻止面试完成，失败类型会保存供查看，缺分题目可单独补评。

`answer` 调用 LangGraph 的 `interrupt()`，此次图调用就此返回；终端收集完整答案后用
`Command(resume=answer)` 和相同 `thread_id` 恢复。等待输入的节点会重新进入，
因此它在 `interrupt()` 之前没有模型调用或展示副作用。出题/追问属于上一个节点，
`durability="sync"` 确保保存后才到达输入中断。

所有业务节点串行返回更新；`questions` 整体替换，`evaluations` 按题目 ID 合并，
`usage` 按调用 ID 合并，避免重复收集导致重复计数。回答的 `None` 与 `""` 语义不同。

`data/` 内有两份职责不同的 SQLite 文件：

- `checkpoints.sqlite`：LangGraph 执行位置和 State，**唯一恢复进度来源**。
- `operations.sqlite`：成功调用的结果记录、已知失败调用用量、会话 ID 索引、
  每次真实模型调用的 `stage_metrics`（环节、起止时间、耗时、token、成败、重试次数）；
  Web 界面另用它保存用户归属、访问令牌与界面系统消息。不保存第二份进度，
  不从这里推断下一节点。

`submit_score` 是一个很薄的技术节点：前一步先把本题 `done` 保存，再提交后台评分。
评分任务拿冻结的问答副本，不写 Graph State；结果先按题目 ID 落盘，后续节点收集合并。
进程退出时取消未完成任务，恢复时补交已经结题但没有成功结果的评分。
等待回答期间评分任务可以继续运行；**Python Task 本身没有被 checkpoint 保存**。

成功模型结果可以在节点重跑时复用；如果服务端已经返回、但本地记录尚未保存就崩溃，
仍可能重复调用。这里不承诺 exactly-once，也不自动迁移任意修改后的旧图 checkpoint。
SQLite 文件中含简历/问答，属于私人数据；恢复或备份需要保留整个 `data/`，复制前先关闭应用。

## 2. 面试与评分策略

沿用同一张图，不增加 Agent 或节点。快评提供语义证据，代码控制次数和流转：

- `answer_kind` 区分最新回答是在作答、澄清题意，还是明确放弃；覆盖情况仍累计全部问答。
- 低覆盖（低于 50）不会立即结题，先给一次针对性的补充机会。之后若仍低覆盖且没有有效新证据，则中性结束；有新证据可在追问上限内继续。
- 核心能力已有足够有效证据时可以提前结束；明确跳过、主动提交空回答或预算耗尽时也结束。
- 每题最多 3 次正式追问，另有最多 1 次题意澄清。澄清使用 `probe` 节点的专用提示词，先回应条件，再让候选人继续回答；不泄露技术答案，也不虚构候选人的项目事实。
- 每轮保存 `kind`（main / probe / clarification）、完整快评和决策。正式追问次数由已保存回合类型统计，模型调用记录则使用实际回合序号，避免澄清和追问共用编号导致串用结果。第三次追问仍允许一次尚未用过的澄清机会。
- 追问目标只描述还缺什么能力证据，不要求引导候选人说出标准答案；不设 40 字长度限制，允许必要背景。

单题评分在一次模型调用中先列证据类别与回合引用，再给四维分数：

- 区分明确正确、明确错误、已问但未展开、未被问到、题意不清或提问带偏。
- 未问到或题意不清不能直接变成低分；证据不足的维度 `score=null`，报告显示“未评估”。有未评估维度时不计算 `/20` 总分，也不折算或填零。
- 本地校验要求引用已提交的真实回合、引用文本确实来自该回合的候选人回答；只有未问到/题意不清证据的维度不能给数字分。失败有一次纠错机会。
- 代码能检查结构、来源和范围，不能证明模型的语义分类或评分理由一定正确；仍需用真实模型样例审查。

旧回合缺少 `kind` 时按主问及普通追问解释，已保存的问答、评分和已执行决策不重写。
新的快评/追问调用记录带 v2 标识，避免复用旧格式的模型响应；尚未执行的步骤使用新策略。
要公平比较整场效果，应新建会话，而不是混合新旧策略恢复后再比较分数。

token 预算是软上限，达到后停止新题/追问，允许必要收尾和评分；已知用量会累计，
没有收到服务端 usage 的失败请求无法精确计费，报告不能替代账单。
报告是单题评价汇总，没有整场模型评价。

## 3. Web 层：令牌、配额与状态轮询

Web 服务是单进程、全局一个 `InterviewApp`。同一进程内多用户并发各自会话是安全的，
但**不要与 CLI 同时操作同一个 `--data-dir`**（每会话文件锁会互相排斥）。
演示与真实会话可以共存于同一个目录，演示报告自带醒目标记；一个进程内仍不能混用两种后端，
Web 的 `answer` / `resume` 会拒绝后端不匹配的会话，报告和详情仍可只读查看。

### 访问令牌的两层模型

- **令牌 = 门**：所有 `/api/*`（除 `/api/login` 与换取路由）都要通过 HTTP 中间件校验令牌。
  库里只存 `sha256(token)`，外加一份明文便于事后补发（`pass list` 可见）。
- **用户名 = 数据分组**：只校验令牌，不校验身份。凭同一枚令牌填任意用户名即自动创建，
  同名看到同一批会话（含简历与问答原文）。`cli` 是命令行的固定用户名：命令行新建的会话、
  以及升级前已有的历史会话都归 `cli`，Web 登录时填 `cli` 即可查看。因此两个前端可以共用同一个 `--data-dir`。
- **配额 = 钱包**：临时令牌带 `quota_tokens`，用量按「令牌 → 会话」归属（`pass_sessions` 表）
  从 `stage_metrics` 真实用量汇总，含后台评分。耗尽后新建/作答/恢复返回 403 且带
  `quota_exhausted` 标记，查看历史与切换用户不受限；拿到新令牌后可继续未完成的面试。

临时令牌有两套时钟：`expires_at` 在签发时按 `ttl_seconds` 预置，首次使用时
（`UPDATE ... WHERE first_used_at IS NULL`，条件写保证并发安全）把首次使用时间落库并重算
`expires_at`。未激活的令牌不会随时间缩水。

`?pass=TOKEN` 由 `/pass/{token}` 换取路由处理：先校验并种 http-only cookie，再 302 到
`/?pass=TOKEN`，让登录表单能预填令牌（cookie 是 http-only，前端读不到）。
cookie 为 `samesite=lax`；启用 HTTPS 时加 `--secure-cookie` 补上 `Secure` 标记。

### 轮询与耗时

心跳只请求 `/api/interviews/{id}/status`，返回由三条索引计数（模型结果收据、界面事件、
阶段指标）构成的 `revision`、`busy`、后台评分是否仍在跑，以及当前阶段与已用时；
只有 `revision` 变化或本轮结束时才拉完整详情，不每次重建整页。等待期间显示
「当前阶段：快评决策 / 生成追问 / 单题评分…」。

每一次真实模型调用（生成考点计划、出题、快评决策、生成追问、本题收尾、单题评分）
都在 `operations.sqlite` 的 `stage_metrics` 落一行：环节名与 `stage_key`、起止时间、
耗时毫秒、token、成功/失败（只存异常类名，不存异常正文）、重试次数。CLI、Web、离线 demo 都写入，
重启服务后仍可查。命中已有结果收据的重放不重复记录，避免把恢复过程算成模型调用。
没有 usage 的失败请求 token 记为 0，不能替代账单。页面底部「阶段耗时与 Token 用量」卡片
展示调用次数、累计耗时、累计 token、失败次数及逐条明细；`GET /api/interviews/{id}/timings`
另返回进程内的 `live` 实时阶段。这些记录只是诊断证据，不写入 checkpoint、不参与流程判断。

离线演示后端没有网络往返、也不产生真实 usage，所以耗时接近 0 ms、token 记为 0；
非零耗时与 token 都来自真实模型服务。

### 界面与报告

信息流按 **系统 / 面试官 / 候选人** 三种角色区分，候选人用蓝色底色与面试官的青绿拉开：
问答与收尾来自 checkpoint，系统消息来自已保存的决策、评分状态和界面运行记录。
提交回答后由后台任务推进，不会因刷新丢失已提交内容。

报告采用结论前置：标题与元信息之后依次是「整场汇总」和「后续复习建议」，再往后才是
逐题记录与评分。「整场汇总」只统计已落盘题数与已收集评分份数，「后续复习建议」只从
已落盘的四维分数与快评记录派生优先复习方向，不额外调用模型；证据不足时明确写
「不给薄弱方向判断」。Web 报告由接口即时返回，不写入 `outputs/`；需要落盘文件仍用 CLI 的 `report`。

`--data-dir` / `--output-dir` 默认是相对**当前工作目录**的 `data` / `outputs`：
在项目根目录启动就读写本项目已有的会话与报告，从别的目录启动会各自新建一套；
要固定位置就显式传路径。

## 4. CLI 输入区

- **Enter 换行**，支持方向键移动、跨行修改、中文退格和多行粘贴。
- **先按 Esc，再按 Enter 提交**。空白答案还需输入 `y` 确认。
- **Ctrl-C / Ctrl-D 暂停**；已提交内容可恢复，未提交草稿不保存。
- 面试官青色、系统黄色；`NO_COLOR`、`TERM=dumb` 或非终端输出时不使用角色颜色。
- 非交互环境使用 `--answers` 文件，不把 EOF 当成空答案。

终端输入法候选窗由宿主终端管理；测试覆盖已输入中文的编辑、删除与粘贴，
不代替在具体终端里检查输入法体验。

## 5. 模型与提示词版本

默认出题/快评/追问使用 DeepSeek，评分使用 Qwen，走 `langchain-core` + `langchain-openai`，
不额外套通用 Agent 循环。兼容端点使用 Chat Completions；JSON 本地校验，失败最多纠错一次，
考点数量与题面风格另有有界校验。文本完整生成、保存后再展示，不承诺逐 token 显示。
模型请求超时由 `MODEL_TIMEOUT` 控制。

`--prompt-version original|improved|improved_v2` 选择快评与追问提示词：

- `original` 保留原版，默认仍使用它。
- `improved` 加入已获证据、合理替代方案和避免重复追问的要求。
- `improved_v2` 保留上述要求，并区分「原文已外置」和「能准确找回这次调用的原文」：
  后者缺失时只追问一次。

版本在新建会话时保存，恢复时不能更换；已运行的 `improved` 文本保持原样。

`--preset` 读取 JSON 预定问题文件，**题数等于 `questions` 数组长度**，不再传 `--questions`。
格式见 [`../inputs/preset-example.json`](../inputs/preset-example.json)：每题需要 `topic`、
`difficulty`（1–5）、非空 `reference_points` 和 `rounds`。第一轮 `kind=main`，
后续可用 `probe` 或 `clarification`；每轮 `ask` 必填，`answer` 可选。除最后一轮外，
所有轮次必须有答案；最后一轮有答案时从该答案的快评继续，没有答案时先显示该问题并等待回答。
空字符串表示主动提交空回答。

预定轮次是固定的历史问答，**不会调用模型重新生成，也不会伪装成新模型的产出**。
从最后一个预定答案开始，才调用所选版本做快评、决定是否追问，随后生成新追问。
预定追问仍占每题最多三次的额度，预定澄清最多一次。预定问答在新会话中保存；
不要把含私人资料的预定文件提交到仓库。

同一份固定历史在两个独立会话中比较提示词版本：

```sh
uv run mockinterview new --env /path/to/.env \
  --preset inputs/preset-example.json --prompt-version original
uv run mockinterview new --env /path/to/.env \
  --preset inputs/preset-example.json --prompt-version improved
```

比较时保持同一模型配置、预算和预定问答，分别新建会话。两版可能生成不同追问，
不能把按序固定答案文件当成能回答任意追问的候选人。用于修改提示词的样本与最终验收样本应分开；
离线 demo 只验证流程，不代表真实模型效果。2026-11-06 的一次真实对比中，`original` 对一份
不完整样本追问了回查方法，`improved` 却将其判为证据充分——这个样本已用于修改 `improved_v2`，
此后只能作为回归检查。

## 6. 部署（Debian 13 / systemd / nginx）

代码走 GitHub 单向拉取，不打包不 rsync：本地 `git push`，服务器 `git pull` + `scripts/deploy.sh`
（`git pull --ff-only` → `uv sync --locked` → 重启服务 → 自检 curl）。首次部署：

```sh
# 1. 装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 拉代码、放密钥（.env 不入库，单独传，权限 600）
mkdir -p ~/apps && cd ~/apps
git clone https://github.com/StarJiao/mockinterview-langgraph.git
cd mockinterview-langgraph && uv sync --locked

# 3. 启用 systemd 与 nginx
sudo cp deploy/mockinterview.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mockinterview
sudo cp deploy/nginx-mockinterview.conf /etc/nginx/sites-available/mockinterview
sudo ln -sf /etc/nginx/sites-available/mockinterview /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

服务只监听 `127.0.0.1:8000`，由 nginx 反代 80/443，便于同机后续挂别的站点。
nginx 配置按 `default_server` 写，域名备案下来后改 `server_name` 即可。

运维须知：

- 日志：`journalctl -u mockinterview`（journald 已持久化到 `/var/log/journal`，默认按 4G/15% 封顶滚动）；nginx 走 logrotate（daily / rotate 14 / compress）。
- 业务数据不会自动清理：`checkpoints.sqlite` 与 `operations.sqlite`（含 `stage_metrics`）长期保留，属于设计而非遗漏——用量统计与配额核算依赖它们。
- `?pass=TOKEN` 会出现在 uvicorn 与 nginx 的 access log 里，共享日志前先确认这一点。
- 想在本机分析线上会话：`scp` 整个 `*.sqlite*`（含 WAL/SHM）到本地，再用
  `uv run mockinterview sessions --data-dir <目录>` 或直接 SQL 查 `stage_metrics`。

## 7. 测试与验证

```sh
uv run pytest -q
uv run ruff check src tests

# 一场三题，每题主问 + 3 次追问，离线端到端验证。
uv run mockinterview new --demo --questions 3 --answers inputs/demo-answers.txt
```

测试使用真实 SQLite checkpoint 与固定模型替身，覆盖重启续答、追问上限、空答案、
模型失败重试、checkpoint 写入失败、后台评分重叠、评分复用/补交、并发打开保护及中文编辑；
另覆盖低分补充机会、澄清不占追问额度、澄清后的跨进程恢复、调用记录不串用、
澄清结果保存失败后的复用、充分回答提前结束、未评估维度展示、非法证据引用纠错。

`tests/test_interview_policy.py` 用真实失败话语配合预设快评结果验证程序分支，
不是对真实模型理解能力的验证；提示词约束测试也不等于模型语义测试通过。

`tests/test_web.py` 使用离线模型替身与真实 SQLite，覆盖按用户隔离、旧会话归属、必填校验、
多 JD 进入提示词、三角色信息流、空答案确认、继续未完成会话、已完成只读与报告导出，
以及令牌过期/作废/配额耗尽、首用计时、`?pass=` 换取、Secure cookie 标记、
阶段耗时与 token 落库、重开数据库后仍可查、失败调用只记异常类名；
不包含浏览器端输入法与真实模型效果验证。

离线报告明确标记演示，不代表真实能力。自动化测试不调用付费模型联调。
