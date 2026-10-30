# 01 · 编码任务证据验收门槛

Type: task

Status: resolved

Owner: backend implementer

Blocked by: 无

**What to build:** 实现 spec.md D1 的可选任务档案、标准、证据和审核门槛；D4 边界保持不变。

## Acceptance
- 缺失/失败/过期证据与审核阻塞禁止 accepted。
- 命令、cwd、退出码、日志、阻塞与代码版本有结构化记录；不执行命令。
- 老线程、普通讨论和 ACK 行为不变；有回归测试。

## Comments

- 2026-10-30 backend：实现完成；独立代码 REVIEW 无剩余阻塞项。完整 unittest 96 项通过，语法编译与 diff 检查通过；普通讨论/ACK 兼容，生产 ui/ 未改动。后续仅创建草稿 PR，不合并。
