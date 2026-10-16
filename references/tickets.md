# Tickets

English lead: write a human requirement as a spec and tickets before dispatch. One round of requirements may become several tickets. Each ticket locks exactly one thing that can be accepted on its own. Without a ticket for that work, do not start implementation and do not open a collaboration thread. The Chinese text below is normative. Copy its headings and field names as written. `scripts/collab.py` does not parse these files into signals or the turn state machine.

## 先写规格和票，再派工

人类给出需求后，先写成 `spec.md` 和票，再派工。写规格和票发生在人类当前的会话里，不靠协作线程完成。

一轮需求可以拆成多张票。拆分标准是能否单独验收：每张票只锁一件可单独验收的事。两件必须合在一起才算完成的事，仍是一张票；一件可以单独验收的事，不和别的事写在同一张票里。

没有对应的票，不开始实施，也不开协作线程。讨论、研究、实现都一样。研究可以先有一张 `Type: research` 的票，再开线程。

## 票根目录

按这个顺序找票放在哪里：

1. 项目的 agent 文档如果已经声明票的目录和布局，用那个目录和那种布局。本仓库的声明是 [`.docs/agents/issue-tracker.md`](../.docs/agents/issue-tracker.md)，根目录是 `.scratch/<feature-slug>/`。
2. 没有声明时，不要因为某个目录名叫 `issues` 就把它当成票根。写到 `.ai-collab/tickets/<feature-slug>/`，与 `threads/` 同级，绝不写进某个线程目录里。

功能目录内的 `issues/` 是规定的子目录。项目根上一个碰巧叫 `issues` 的目录不是票根。

`<feature-slug>` 用英文 kebab-case。一个功能一个目录。

本仓库的 `.gitignore` 忽略 `.ai-collab/`。未声明票根的项目，票落在这个本地工作区里。需要把票提交进仓库时，在 agent 文档里声明一个工作区以外的根，像本仓库这样。

## 目录布局

```text
<ticket-root>/<feature-slug>/
  spec.md
  issues/<NN>-<slug>.md
```

- `spec.md` 是这一功能的规格。
- 票文件是 `issues/<NN>-<slug>.md`。编号从 `01` 起，两位数字，slug 用英文 kebab-case。一票一文件。
- 文件名里的编号必须和标题里的编号一致。`issues/05-client-fields.md` 的标题就是 `# 05 · …`。

回合保持不可改。票可以改状态。评论只追加。

一张已 `resolved` 的票又出现新问题，另开新编号。不要把旧票改回进行中。

`Blocked by` 里列出的每一张票都变成 `resolved` 之后，这张票才算解除阻塞。`wontfix` 不算解除。

## 规格

`spec.md` 使用这个骨架。决策编号在同一份规格内从 D1 连续编号。

```markdown
# 功能规格标题
Status: ready-for-agent
## 目标
## 已拍定的决策（人类确认，协作方不要重新论证）
### D1 决策标题
## 范围之外
```

规格可以另有章节，例如进度、接口契约。规格的 `Status` 描述整个功能。不要因为部分子票已关闭，就把规格改成 `resolved`。

状态取值与票相同，见下文。协作方不要重新论证「已拍定的决策」里的条目。

## 票头

每张 `task` 票使用下面的票头。每一行之间空一行。

```markdown
# NN · 标题

Type: task

Status: ready-for-agent

Owner: 待定

Layer: 要改的路径。明确不改的路径也写在这里。

Blocked by: 无

**What to build:** 用一段话写清要交付的行为，并指向规格里的决策编号，例如「口径以 spec.md 的 D7 为准」。这是验收口径。
```

`Type` 是 `task` 或 `research`。

`research` 把 `**What to build:**` 换成 `**What to analyze:**`。问题写在正文里，结论写在 `## Answer`。研究票可以省略 `Layer`、`Scope`、`Acceptance`。

没有跨层范围时，可以省略 `Layer`。其余票头行仍每行之间空一行。

### Status

取值：

- `needs-triage`
- `needs-info`
- `ready-for-agent`
- `ready-for-human`
- `blocked`
- `wontfix`
- `resolved`

本仓库的票常见 `ready-for-agent` 和 `resolved`。关闭时写成 `resolved`。

可以在状态后附一句证据，例如 `resolved（2026-10-16 david 实施，cursor round-8 复核通过）`。不附这句话也可以关闭。

### Owner

`Owner` 是实施者。

- 还没有人：`待定`，并写上预计改哪些路径。
- 已有线程：`david（协作线程 project-overview-jump-backend）`。
- 同一张票两个人并行：`后端 david（线程 project-overview-jump-backend），前端 lily（线程 project-overview-jump-frontend）`。

### Blocked by

没有依赖时写 `无` 或 `无。`。后面可以加上仍然有效的约束。

有依赖时写票号，以及它挡住什么、什么可以先做。例如：`票 05（只阻塞重新生成客户端和接真实字段。跳转决策、文案和 Model 测试先按本票契约做）`。编号可以写成 `票 05` 或 `05`。

## 票头之后的可选节

这些节都不是必填。`## Comments` 如果有，放在全文最底部。

- `## 已拍定、不要重做`：必须保持的行为，以及禁止的改动。
- 票内需要编号时，标题改用 `## 已拍定的决定`，条目写成 `### D13-1 · 标题`。编号是 `D` + 票号 + 序号。不要和规格里的 D1、D10 混用。
- `## 已核实的事实` 或 `## 已核对的事实`：核对过的代码、测试和结论，带日期。
- `## 修法`：指定的实现方式。
- `## 优先级`：相对其他票的顺序。
- `## Scope` 和 `## Out of Scope`：范围和验收分开写。
- `## Acceptance`：验收时必须成立的行为。
- `## Comments`：只追加。每条是 `- 2026-10-16 cursor：正文`。不改旧评论。不覆盖 `What to build`。

规格里的 D 编号、票内的 D13-1 编号，以及 `What to build`，是需求事实。Finding ID 留在回合上，不拿来代替这些编号。

## 研究票

```markdown
# 02 · 标题

Type: research

Status: ready-for-agent

Owner: 待定

Blocked by: 无

**What to analyze:** 要回答的问题。口径以 spec.md 的 D2 为准。

## Answer

结论写在这里。未完成时留空，不要删这一节。
```

## 线程是新的关联

线程不从旧票推断。开线程是此刻新写的关联，不是从历史票里找一个旧线程接着用。

默认：一张进行中的票，一条协作线程。把票的路径写进线程 objective。

前后端并行时，一张票可以有两条线程，两条都写进 `Owner`。

派工用 `DECISION`。验收用 `REVIEW`。这张复核通过之后，才把票的 `Status` 写成 `resolved`。复核未通过时不改成 `resolved`。

新协作者每人一条开头语，让对方用这个 skill 加入该线程。人类正在和协调者对话时，把开头语交给人类转发。协调者是当前会话的 subagent 时，由开始线程的 agent 直接转发，不让人类转。已经参与过的协作者不写开头语，只让人类告知有一条新线程派给对方。

`collab.py` 不读票，也不把票解析进信号或回合状态机。
