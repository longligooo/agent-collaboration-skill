# 02 · 添加真实 Web UI 截图

Type: task

Status: ready-for-human

Owner: README layout agent

Layer: README*.md、docs/images/ 和本规格/票；不修改运行时代码或仓库元数据。

Blocked by: 无

**What to build:** 按 spec.md 的 D2，运行仓库实际 Web UI，用合成演示数据截取 1–3 张有用的总览/详情截图，优化图片文件体积，添加六语言说明和替代文本，验证 GitHub 渲染并更新 draft PR。不得合并。

## Acceptance
- 图像来自真实运行界面，不含私人数据，不暗示已运行真实工具集成。
- README 中说明合成示例和界面语言，图片相对链接在 GitHub 可用。
- 回归测试通过，截图进程仅使用任务自己的隔离目录并在完成后清理。


## 验证记录
- 2026-10-29：使用仓库原有 Python UI server 与 ui/ 文件运行真实界面；运行时代码未修改。
- 演示数据通过 CLI 在隔离目录生成：2 个项目、3 条线程、3 位参与者；包含提案、审查、修订、独立验证和待确认信号。未调用外部智能体。
- 使用云浏览器捕获实际线程列表与详情，共 2 张 JPEG（约 108 KiB）；列表截图通过截图范围去掉无关空白，不改造界面。
- 六语言新增明确的合成数据说明、中文界面说明、描述和替代文本；原有命令全部保留。
- 截图文件：`docs/images/web-ui-threads.jpg`、`docs/images/web-ui-thread-detail.jpg`。
