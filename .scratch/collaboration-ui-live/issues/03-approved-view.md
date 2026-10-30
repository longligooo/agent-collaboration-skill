# 03 · 已批准只读界面

Type: task

Status: resolved

Owner: UI implementer and backend integration implementer

Blocked by: 无（可按 D2/D3 契约并行）

**What to build:** spec.md D1/D2/D3；实现批准的 v0.4 布局和像素动画；CLI 与 UI 复用真实任务/运行/来源读模型。

## Acceptance
- 严格按真实 active 状态动效，所有同身份头像同步；减少动画偏好与窄屏可用。
- 只读来源与接收者展示；缺失旧数据兼容。
- D4 发布权限保持独立等待。

## Comments

- 2026-10-30 UI REVIEW：用户实际截图确认通过；27 UI tests通过。真实只读API在1180px桌面/390px手机验证，人类来源与协调者布局、跨线程6处活动头像和输入提示同步；测试进程exit0后全部停止。离线保留4条缓存消息并提示过期/不可观察，活动归零。减少动效CSS与测试通过（未切换OS偏好）。无提交/推送。
