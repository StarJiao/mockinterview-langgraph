# 提示词对比：回放样本与人工判定

比较对象：`original` 与 `improved_v2`。每个版本使用相同模型配置、同一份固定问答，各自新建会话。预定历史不会重新生成；从最后一个已给答案开始重新快评。读快评的 `covered`、`missed`、`sufficient`、`next_target`，以及实际追问。覆盖分和最终总分仅作辅助，不能代替逐项判断。

## 已用于修改提示词的两例（回归样本）

| 样本 | 预期 |
| --- | --- |
| `inputs/preset-example.json` | 已给出预算、结构化状态、外置回查标识和按任务取舍；应结束，不再追问固定配额或要求超大结果完整常驻。|
| `inputs/preset-followup-example.json` | 已给出预算与结果外置，但没有说明如何准确对应并找回这次调用的原文；可继续问一次回查办法，不重新问预算或外置原则。|

这两例已经参与改进版调整，只能用于回归，不能证明泛化效果。此前真实运行中，两版在完整样本都结束；不完整样本里 `original` 正确追问，`improved` 提前结束，`improved_v2` 追问了准确回查办法。

## 新的对照样本（尚未用于修改提示词）

以下两个本地文件从用户已保存的 HRIS 面试问答提取，保持原题、评价要点及已提交轮次。文件位于被 `.gitignore` 排除的 `inputs/`，不提交问答原文。

| 样本 | 运行前写定的人工预期 |
| --- | --- |
| `inputs/holdout-hris-history.json` | 候选人已经提出封存历史、不重算已结算考勤、用薪酬调整项对平，并解释了不重算的理由。快评应承认这是有条件成立的替代处理；不得把“必须冲销重算”当成唯一正确方案。若继续追问，最多只问一个仍影响一致性判断的点，例如历史组织归属与薪酬调整项如何让查询者区分。|
| `inputs/holdout-hris-sync.json` | 候选人先澄清“HRIS 正常”的含义，澄清后按员工报错区分未下发与设备侧问题，并提出检查下发条件。快评应承认这些已获证据；同步任务是否触发、接口是否成功尚未回答，继续问这一处合理。不得重问源系统记录到底指什么。|

评判时逐项记录：已获证据是否保留、未覆盖项是否确实必要、继续或结束是否合理、追问是否只补一个缺口。对一个样本的新旧差异，需要写出具体句子与理由；不能凭覆盖分高低判胜。真实模型输出可能波动，单次运行只提供案例证据，不能推断总体胜率。

## 运行命令

每个样本分别运行两版。以下命令会调用真实模型并把固定问答发送给配置的模型服务；不带 `--answers` 时，若生成新追问，会停在终端等待作答，可用 Ctrl-C 暂停。

```sh
uv run mockinterview new --env .env --preset inputs/holdout-hris-history.json --prompt-version original
uv run mockinterview new --env .env --preset inputs/holdout-hris-history.json --prompt-version improved_v2

uv run mockinterview new --env .env --preset inputs/holdout-hris-sync.json --prompt-version original
uv run mockinterview new --env .env --preset inputs/holdout-hris-sync.json --prompt-version improved_v2
```

只比较这一轮时，在新追问处暂停即可；若直接收尾，再看报告中的快评依据。不要用不同版本各自生成的问题继续喂同一份按序答案。

## 本次实际结果

2026-11-08 使用相同 `.env`，四个回放分别存于 `/tmp/mockinterview-holdout-20261108/` 的 `original-history`、`improved-history`、`original-sync`、`improved-sync` 子目录。每个会话从固定历史的最后一个答案开始；生成追问后由空答案文件暂停，没有替候选人作答。

| 样本 | original | improved_v2 | 人工判断 |
| --- | --- | --- | --- |
| HRIS 回溯生效 | 快评 60，`sufficient=true`；承认调整项对平，不继续追。 | 快评 70，`sufficient=true`；也承认封存历史与调整项方案，不继续追。 | 两版都达到预期。分数差异不能证明新版本更好。 |
| HRIS 同步故障 | 快评 25，继续追；同一句问任务触发、接口成功、整批或个别失败。 | 快评 25，继续追；集中问取哪些同步链路信息，判断任务是否执行、断在哪一环。 | 两版都保留了已获证据并正确继续。改进版问法较集中；仍包含执行与断点两个紧密相关的部分。 |

第一组两版都进入异步评分，但评分服务均失败，整场没有完成；比较只依据已经持久化的快评和停止决策，不把缺失的评分当作模型质量结果。第二组都停在生成的追问，符合回放目的。初次普通网络环境运行因连接失败，随后获准连接模型服务的调用才得到上述结果。

两组新样本均未用于修改 `improved_v2`，但数量少、模型输出有随机性，结论仅限这四次实际运行。后续若要声称稳定提升，应扩展独立样本并多次运行，报告每例的接受替代方案、重复追问、单问点和停止判断结果及调用成本。
