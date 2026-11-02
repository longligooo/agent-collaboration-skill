# 01 · 按页面触发跨刷新增量读取

Type: task

Status: resolved（2026-11-02 独立审阅通过；验证见 ../validation.md）

Owner: implement_progressive_overview_loading

Layer: scripts/ui_server.py、ui/app.js、相关测试及文档；不集成自动任务功能。

Blocked by: 无

**What to build:** 按 spec.md D1–D3，消除自动重复扫描，导航/明确刷新只重读变化内容，保留新增/删除一致性。

## Acceptance
- 完成后推进所有计时器不重启内容请求，活动心跳独立。
- 筛选、展开、渲染等交互零内容读取；导航、刷新发起合适的请求。
- 服务端重复无变化请求不重投影线程；变更、新增和删除正确反映，部分失败及版本失效安全。
- 并发导航去重，过期响应不覆盖新页面；60秒批次、全局排序和原有测试继续通过。

## Comments
- 2026-11-02 implement_progressive_overview_loading：修复并独立复核了旧详情失效标记竞争、Git索引变化与扫描中身份配置变化；未提交或发布。
- 2026-11-02 implement_progressive_overview_loading：用户随后授权本批直接发布 main；只提交本票修复，使用非强制更新与 expected_sha 保护并发改动。
