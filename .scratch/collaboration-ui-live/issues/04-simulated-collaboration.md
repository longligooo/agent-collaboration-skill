# 04 · 非界面模拟协作端到端验收

Type: task

Status: resolved

Owner: independent simulated collaboration tester and backend implementer

Blocked by: 无

**What to build:** spec.md D5；通过真实公开CLI与HTTP读取接口，在隔离临时仓库中模拟协作者完成验收/驳回/修复/恢复全过程。

## Acceptance
- missing/stale/failed evidence 与独立审核门槛在真实命令中生效。
- 进程重启检查点、跨项目我的任务、等待分类、重复请求/CAS可重现。
- 人类来源事件不执行命令、不发协议消息、不授予操作权。
- watcher失败/超时/死进程/过期心跳不误标运行。
- 逐场景提供实际观察结果与可重跑测试；清楚标注模拟，不声称真实LLM遵循。

## Comments

- 2026-10-30 independent simulation REVIEW：5条公开CLI/真实HTTP旅程多轮通过；最终总回归183tests通过。139次CLI、28次HTTP、12次预期拒绝结果正确；无新缺陷、无真实模型调用。逐场景结果见../validation.md与../simulation-results.json，测试源tests/test_simulated_collaboration_e2e.py。
