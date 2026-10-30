# 02 · 明确人类输入来源

Type: task

Status: resolved

Owner: provenance implementer

Blocked by: 无

**What to build:** spec.md D3；新增显式人类输入事件与只读标准化历史，记录手动/授权导入来源、原文、源会话消息、接收者，禁止转述冒充原始输入。

## Acceptance
- 新事件不可覆盖、重复幂等，校验参与者与来源记录；来源不明明确展示为未知而非人类。
- 兼容旧回合，普通 author/role 不推断 human；不访问外部聊天。
- 显式 CLI 记录与只读 API，无 WebUI 发送入口。

## Comments

- 2026-10-30 backend REVIEW：独立审阅无剩余阻塞项。真实进程/超时/退出/过期、遥测失败保留任务执行、坏记录隔离、历史人类身份与接收者不可追溯重映射、源文件路径安全均已补测。16 runtime + 30 human-event + 9 API/history tests 通过；生产 UI 仍在独立浏览器 QA。未 push。
