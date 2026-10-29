# 01 · 重构六语言 README 布局

Type: task

Status: ready-for-human

Owner: README layout agent

Layer: README*.md；必要的文档图片及本规格/票；不修改运行时代码和仓库元数据。

Blocked by: 无

**What to build:** 按 spec.md 的 D1，研究 2–3 个真实热门 Agent 项目，统一六语言的紧凑首屏、功能介绍、快速开始、工作流可视化和进阶内容折叠。保留关键说明，验证 GitHub 渲染、链接、命令、翻译结构及回归测试，并交付 draft PR。

## Acceptance
- 六种语言具有一致的信息架构和完整的重要约束。
- 安装与快速开始容易找到，长参考信息按需展开。
- 命令和链接验证通过，回归测试结果记录清楚。
- PR 保持 draft；远程提交 SHA 与交付内容一致。


## 参考与设计取舍
- [LangGraph](https://github.com/langchain-ai/langgraph)：简洁定位、明确的文档入口；使用紧凑首屏和参考导航，不复制品牌内容。
- [CrewAI](https://github.com/crewAIInc/crewAI)：分组导航、分步入门与示例；采用编号步骤，不引入其商业宣传模块。
- [Claude Code](https://github.com/anthropics/claude-code)：快速进入安装与使用；将安装前移，长参考内容按需展开。
- 原创 Mermaid 审查循环，明确标为示例；GitHub 原生 HTML/Markdown，不依赖自定义 CSS。

## 验证记录
- 2026-10-29：从 main `abc4d8f056e91facdc38dad43f426851e898fc63` 读取并逐文件核对 Git blob SHA。
- 六语言原有正文和 Bash 示例保留；新增版式、标题、功能摘要、图示与文档导航同步。
- 独立复核：全部 30 项回归测试通过；六语言快速开始分别在临时目录通过；无人值守示例使用 STOP 安全测试，不启动真实智能体。
- GitHub 实际渲染已检查首屏、流程图和折叠内容；修复了初版自定义锚点与英文标题 ID 冲突。
- 保持 draft PR，等待人类审阅；不合并，不改运行时代码或仓库元数据。
