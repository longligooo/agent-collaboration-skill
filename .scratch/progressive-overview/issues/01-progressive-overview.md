# 01 · 60 秒超时和总览渐进加载

Type: task

Status: resolved（2026-11-02 实施完成，独立审阅通过；验证见 ../validation.md）

Owner: implement_progressive_overview_loading

Layer: scripts/ui_server.py、ui/app.js、tests；不改协议和用户数据。

Blocked by: 无

**What to build:** 按 spec.md D1、D2，实现内容请求 60 秒超时及端到端渐进加载。

## Acceptance
- 首批数据在全部线程扫描完成前可见；服务端资源有界，活动接口独立。
- 增量刷新正确处理删除、重试、过期请求，保持排序、路由和滚动状态。
- 回归测试覆盖慢响应、60 秒边界、多项目大线程集和原有功能。

## Comments
- 2026-11-02 implement_progressive_overview_loading：独立审阅发现的默认身份重排、服务端累积性能与尾沿绘制问题均修复；未提交或发布。
- 2026-11-02 implement_progressive_overview_loading：用户随后授权直接发布本批到 main，见 spec.md D3；发布前复核远端，禁止强推。
