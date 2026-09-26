# -*- coding: utf-8 -*-
"""任务本体 v0.2 SHACL 校验自测。

用法: python docs/研究整理/_qa/07a-validate-task-ontology.py
断言:
  S1 合法任务图 --> conforms
  S2 判据引用 agentAttested 事实 --> 不 conform (信任级强制)
  S3 dependsOn 成环 + 行动类缺标注 --> 不 conform (无环约束 + 行动类标注)
"""
import sys
from pathlib import Path

from pyshacl import validate
from rdflib import Graph

TTL = Path(__file__).resolve().parents[1] / "07a-任务本体草案" / "task-ontology.ttl"

PREFIX = """@prefix task: <https://ontology-agent.example/ontology/task#> .
@prefix ex:   <https://ontology-agent.example/ontology/demo#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
"""

# 行动类样例(业务本体侧, 带task:executionMode与task:deterministic)与完整归因
GOOD_COMMON = PREFIX + """
ex:queryTicket a ex:ActionClass ;
    task:executionMode task:modeReadOnly ; task:deterministic true .
ex:refundTicket a ex:ActionClass ;
    task:executionMode task:modeExternalWrite ; task:deterministic false .
"""

S1_VALID = GOOD_COMMON + """
ex:factReceipt a task:Artifact ;
    task:trustLevel task:externallyVerified ;
    task:provenanceDoc "biz-api:refund-20260926-001" ;
    task:provenanceChunk "n/a" ; task:provenanceSpan "n/a" .

ex:c1 a task:SuccessCriterion ;
    task:criterionQuery "ASK { ?r task:trustLevel task:externallyVerified }" ;
    task:citesFact ex:factReceipt .

ex:plan1 a task:Plan ;
    task:planStatus task:planActive ; task:planOrigin task:planFromCombination ;
    task:derivedFromTemplate ex:tpl-fallback ;
    task:hasStep ex:s1 , ex:s2 .

ex:s1 a task:Step ;
    task:boundActionClass ex:queryTicket ;
    task:stepState task:stValidated ; task:stepOrder 1 ;
    task:executor [ a task:Executor ; task:executorKind task:exRuleEngine ] .

ex:s2 a task:Step ;
    task:dependsOn ex:s1 ;
    task:boundActionClass ex:refundTicket ;
    task:stepState task:stGated ; task:stepOrder 2 ;
    task:hasFailureMode [ a task:FailureMode ;
        task:failureType "max_retry" ; task:retryCount 2 ;
        task:compensationAction ex:queryTicket ] .

ex:sc1 a task:StateChange ;
    task:about ex:s1 ; task:actorType task:actorRuleEngine ;
    task:actorId "rule-engine@1.0" ;
    task:decisionId "dec-001" ; task:capabilityVersion "kb-pack@0.3" ;
    task:changedAt "2026-09-26T10:00:00"^^xsd:dateTime .

ex:task1 a task:Task ;
    task:goal "退单完成且 SLA 达标(以业务回执为准)" ;
    task:taskState task:tRunning ;
    task:maxSteps 20 ; task:maxTokens 100000 ;
    task:deadline "2026-09-27T18:00:00"^^xsd:dateTime ;
    task:hasPlan ex:plan1 ;
    task:criterion ex:c1 .
"""

# S2: 判据引用 agentAttested 事实 --> 必须违规
S2_CRITERION_TRUST = GOOD_COMMON + S1_VALID.replace(
    "task:citesFact ex:factReceipt", "task:citesFact ex:factBad"
) + """
ex:factBad a task:Artifact ;
    task:trustLevel task:agentAttested ;
    task:provenanceDoc "doc-9" ; task:provenanceChunk "c-1" ; task:provenanceSpan "10-88" .
"""

# S3: dependsOn 成环 + 行动类缺 executionMode/deterministic --> 必须违规
S3_CYCLE_AND_MISSING_MARKS = PREFIX + """
ex:badAction a ex:ActionClass .

ex:planx a task:Plan ;
    task:planStatus task:planActive ; task:planOrigin task:planFreeform ;
    task:hasStep ex:x1 , ex:x2 .

ex:x1 a task:Step ;
    task:dependsOn ex:x2 ; task:boundActionClass ex:badAction ;
    task:stepState task:stPlanned .
ex:x2 a task:Step ;
    task:dependsOn ex:x1 ; task:boundActionClass ex:badAction ;
    task:stepState task:stPlanned .

ex:taskx a task:Task ;
    task:goal "g" ; task:taskState task:tPending ;
    task:maxSteps 5 ;
    task:hasPlan ex:planx ;
    task:criterion [ a task:SuccessCriterion ;
        task:criterionQuery "ASK { }" ;
        task:citesFact [ a task:Artifact ; task:trustLevel task:externallyVerified ;
            task:provenanceDoc "d" ] ] .
"""


def run(name: str, data_turtle: str, expect_conform: bool, expect_msg: str = "") -> bool:
    shapes = Graph().parse(TTL.as_posix(), format="turtle")
    data = Graph().parse(data=data_turtle, format="turtle")
    conforms, results_graph, _ = validate(
        data_graph=data, shacl_graph=shapes, ont_graph=None,
        inference="none", advanced=True, debug=False,
    )
    ok = conforms == expect_conform
    detail = ""
    if expect_msg:
        report_text = results_graph.serialize(format="turtle")
        hit = expect_msg in report_text
        detail = f"report_hits_expected={hit}"
        if not hit:
            ok = False
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: conforms={conforms} (期望 {expect_conform}) {detail}")
    return ok


def main() -> int:
    cases = [
        ("S1 合法任务图", S1_VALID, True, ""),
        ("S2 判据引用 agentAttested", S2_CRITERION_TRUST, False, "externallyVerified"),
        ("S3 依赖成环+行动类缺标注", S3_CYCLE_AND_MISSING_MARKS, False, "dependsOn"),
    ]
    results = [run(*c) for c in cases]  # 逐个执行, 不短路
    all_ok = all(results)
    print("\n任务本体 v0.2 校验自测:", "全部通过" if all_ok else "存在失败")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
