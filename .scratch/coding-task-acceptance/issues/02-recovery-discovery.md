# 02 · 恢复检查点与稳定身份查询

Type: task

Status: resolved

Owner: backend implementer and read API implementer

Blocked by: 无

**What to build:** 实现 spec.md D2、D3 的原子持久检查点、恢复校验和跨项目我的任务读模型；CLI/API 使用同一事实来源，不修改生产 UI。

## Acceptance
- 重启后可读阶段、负责人、工件、问题和下一步；当前消息或代码变化时提示重新核查。
- 并发/过期写拒绝，重复写幂等；没有旧字段仍可查询。
- 稳定身份跨会话与显式项目映射；不按工具名推断身份。

## Comments

- 2026-10-30 backend：实现完成；独立代码 REVIEW 无剩余阻塞项。完整 unittest 96 项通过，语法编译与 diff 检查通过；普通讨论/ACK 兼容，生产 ui/ 未改动。后续仅创建草稿 PR，不合并。
