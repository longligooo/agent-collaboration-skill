# 01 · 真实运行观察

Type: task

Status: resolved

Owner: runtime implementer

Blocked by: 无

**What to build:** spec.md D2；在既有授权 watch 执行路径记录有过期时间的 runner 生命周期与心跳，跨线程按稳定身份查询，保留已有授权与停止/失败行为。

## Acceptance
- 实际进程启动后才 running；完成、错误、超时、失联不再 active。
- 待处理通知/任务阶段不会变成 running；过期/异机数据不会冒充本机活跃。
- 原子写入，回归测试通过；无新自动命令执行入口。

## Comments

- 2026-10-30 backend REVIEW：独立审阅无剩余阻塞项。真实进程/超时/退出/过期、遥测失败保留任务执行、坏记录隔离、历史人类身份与接收者不可追溯重映射、源文件路径安全均已补测。16 runtime + 30 human-event + 9 API/history tests 通过；生产 UI 仍在独立浏览器 QA。未 push。
