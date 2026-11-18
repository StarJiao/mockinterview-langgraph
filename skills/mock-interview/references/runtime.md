# 原项目运行方式

聊天面试不依赖本项目。以下功能需要真实 checkout、Python 3.11+、uv 和项目依赖。优先检查当前工作区；本机原始项目位置为 `/Users/jiao/mockinterview-langgraph`。迁移后由用户指定 checkout，不能假设此绝对路径存在。命令均在项目根目录执行，先用 `uv run mockinterview --help` 及子命令帮助核对版本。

## 启动与输入

`uv sync --locked` 安装锁定依赖。`uv run mockinterview new --demo --questions 1` 是固定题目与固定评分的离线流程演示，不能当作真实测评。

真实模型从用户指定 env 加载，默认 `.env`；参考 `.env.example` 的字段，不打印密钥。出题/快评/追问默认 DeepSeek，评分默认 Qwen，支持项目配置覆盖。输入资料和答案会发送到这些已配置服务；纯聊天请求无需启动外部模型。

例：

```sh
uv run mockinterview new --env .env --focus 'Agent 工程,后端' --questions 3 --jd /path/to/jd.md --resume-file /path/to/resume.md --state /path/to/learning.md
uv run mockinterview sessions
uv run mockinterview resume SESSION_ID --env .env
uv run mockinterview report SESSION_ID
uv run mockinterview score SESSION_ID --env .env
```

`--data-dir` 默认 data，`--output-dir` 默认 outputs，整个会话保持一致。`--budget` 为软 token 上限，允许必要收尾和评分。CLI 的 `--jd` 接受一个文本文件，多份 JD 可保留来源标记合成一份；Web 支持多份 JD。

交互终端 Enter 换行，Esc 后 Enter 提交，Ctrl-C/Ctrl-D 暂停；未提交草稿不会保存。代理工具的非交互环境使用 `--answers` 文件，答案用独立行 `===ANSWER===` 分隔，文件耗尽即暂停，空文件不等于空答案。只写用户实际提供的答案，不替候选人编造答案。文件格式的边界细节以 `cli.py` 的 ScriptAnswers 为准。

## 固定题目与版本对比

`new --preset /path/to/preset.json` 使用预定题目，不能同时传 `--questions`。顶层仅有 questions 非空数组；每题必须且只能有 topic、difficulty（1–5）、reference_points（非空字符串数组）、rounds。每轮 kind、ask 和可选字符串 answer；首轮为 main，后续只能 probe/clarification，上限分别为 3/1。只有最后一轮允许省略 answer。

固定输入可以复现多轮历史；查看 `inputs/preset-example.json`、`inputs/preset-followup-example.json` 的结构，不将样例历史误当用户回答。新会话可选 `--prompt-version original|improved|improved_v2`，默认 original。版本对比用同一预定历史、分别新建会话；恢复必须沿用创建时版本，Web 当前固定 original。`prompt_versions.py` 保存具体差异。

## 恢复、补评与诊断

checkpoints.sqlite 是唯一进度来源；operations.sqlite 存结果收据、评分、归属及阶段指标，不用它推断下一节点。使用程序 API/CLI 恢复，不手改数据库，不用新会话冒充恢复。已结束会话可以 `score` 补评缺分题，已有评分不重算；评分失败不阻塞面试结束。

成功请求可通过收据复用，但不保证 exactly-once；远端返回、本地尚未落盘时崩溃可能重调。旧图 checkpoint 不保证自动迁移。真实耗时、token、失败和重试次数来自 stage_metrics/Web 指标，缺 usage 的请求无法精确计费；不能把软预算当账单。

## Web、访问控制与部署

```sh
uv run mockinterview web --demo --host 127.0.0.1 --port 8000
uv run mockinterview web --env .env --host 127.0.0.1 --port 8000
```

本机预览显式绑定 127.0.0.1；程序默认 0.0.0.0，仅在用户要求网络访问时使用适当地址。Web 包含用户名+令牌登录、主题/题数/简历/多 JD、新建及历史面试、三角色信息流、阶段进度、报告和调用指标。用户名只是数据分组标签，访问凭证是令牌。

用户明确要求管理令牌时使用：

```sh
uv run mockinterview pass new --days 365
uv run mockinterview pass new --hours 1 --quota 300000 --count 5 --label 朋友
uv run mockinterview pass list
uv run mockinterview pass revoke PASS_ID
```

长期令牌从签发起计时、不限配额；临时令牌从首次使用起计时，配额耗尽后仅能查看历史和报告，换新令牌可继续。`--base-url` 可生成分享链接；创建链接不等于授权代发消息。`pass list` 会输出明文令牌，只有任务需要时调用，勿写进公开报告。

单进程 Web 可处理多用户，但 CLI 与 Web 不同时操作同一个 data-dir。HTTPS 使用 `--secure-cookie`。公网部署按 checkout 的 `docs/DESIGN.md`、`deploy/` 和 `scripts/deploy.sh` 执行；先确定目标和用户要求，不因创建 skill 就部署、签发令牌或启动公网服务。
