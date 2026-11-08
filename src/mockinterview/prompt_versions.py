"""Original text stays immutable; improvements are explicit opt-in."""

from . import prompts

VERSIONS = ("original", "improved", "improved_v2")

COVERAGE = {
    "original": prompts.COVERAGE,
    "improved": prompts.COVERAGE
    + """

追问质量补充规则：
- 逐项检查本题之前已经确认的证据。新回答可以补全或纠正旧回答，不能因参考答案措辞不同而撤销有效证据。
- 参考要点是能力标尺，不是必须逐字说出的清单。候选人的替代做法只要达到同一目的，就计为已覆盖；例如超长工具结果外置、保留可回查凭据，可替代把完整返回常驻上下文。
- missed 只列对评价核心能力仍必要、且未得到有效回答的缺口。可选的优化细节放在 reason 中，不以此强制继续追问。
- next_target 最多选择一个此前没有针对性问过的核心缺口。若同一缺口已问过且候选人没有进一步展开，按已获得证据评价并令 sufficient=true；不要换措辞继续追。
- 判断是否保留近期对话或工具结果，应允许候选人按下一步任务依赖作条件判断，不强求固定顺序。若理由和可回查方法充分，令 sufficient=true。
- sufficient 表示已足以评价本题核心能力，不要求所有参考细节都出现。""",
}

PROBE = {
    "original": prompts.PROBE,
    "improved": prompts.PROBE
    + """

生成前先核对完整问答：本轮只问 next_target 中尚未回答的一个能力缺口。若候选人已经给出达到同一目的的替代方案，不再索取参考答案中的指定实现。不要把两个以上缺口合成一道追问，也不要用新假设重新考已经确认的同一点。""",
}

# Keep the version used by earlier comparisons intact. New experiments get a
# distinct name so a resumed session never silently changes its instructions.
COVERAGE["improved_v2"] = COVERAGE["improved"] + """

外置结果的证据边界：说“原文存外部，需要时再读”只说明原文存在，不自动证明当前工具调用能准确对应并找回那份原文。若本题明确考察长上下文中的关键工具进展，而回答未说明调用标识、存储引用或其他等价的对应办法，把“准确回查这次调用及其关键入参/结果”列为一个必要缺口，sufficient=false，并只追问一次。完整原文无需常驻上下文；有可操作的外部记录定位和按需读取办法，就接受为等价方案。不得因为某个可选字段未提而继续追问。"""

PROBE["improved_v2"] = PROBE["improved"] + """

如果唯一缺口是外置结果的可回查性，只问候选人怎样把当前这次工具调用对应到外部原文，之后如何据此找回。不要顺带要求固定预算配额、所有字段、审计或完整结果常驻。"""
