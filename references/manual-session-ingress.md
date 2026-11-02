# 手动会话接入（默认关闭，离线 fixture 验证）

本功能保留手动开会话，不启动 watcher，不调用模型，不安装或修改任何工具配置。
**当前交付的是 opt-in 本地 CLI、规范化 adapters 和 read-only snapshot；不宣称已在用户四种工具上实测。** 本次随附 UI 已单独接入 `manual_observations` 并显示来源/过期状态，不与真实进程运行混合；旧版 UI 不会因为仅安装本模块就自动显示活动。原文通过既有 human-record 入库后由既有会话读取路径显示。

## 三种证据不能混用

- `process-observed`：现有 watcher 对其实际子进程的观测，本模块不生成。
- `hook-event-observed`：只说明最近收到一个已绑定工具事件，不证明模型此刻仍在执行。15 秒无新事件即 `unknown`；Stop/Interrupt 为 `ended`，撤销为 `revoked`。长时间思考没有新事件会保守地显示未知。
- `agent-self-reported`：协作者显式自报 `working/idle/waiting`，独立记录并在 15 秒后过期，不证明进程实际运行。

快照中 `active` 一律 false；前端若展示 `evidence_active`，必须同时展示证据标签，可展示有来源标签的短暂活动指示，**不能合并到旧版真实进程运行 identities**。无 PID/时间循环自行续租，无任务 phase 推测。CLI 可见性不是执行权限。

## 兼容性与预检

| 模式 | 合成测试覆盖 | 尚未实机确认 |
|---|---|---|
| Claude Code 手动会话 | session_id/cwd、UserPromptSubmit、工具事件、Stop/StopFailure/SessionEnd | 具体版本、安装位置、退出/中断实际投递 |
| Codex CLI / 桌面 Codex | session_id/cwd/turn_id、UserPromptSubmit、工具事件、Stop/Interrupt/SessionEnd | 用户版本、本地或云执行位置及 hook 支持 |
| Cursor Agent Chat | conversation_id/generation_id/cursor_version/workspace_roots、beforeSubmitPrompt、工具事件、stop 三状态/sessionEnd | 用户版本、多根工作区行为及实际投递 |
| 普通 ChatGPT Chat | 无 | 不提供此模式集成承诺 |

先人工查看工具版本/模式、项目路径、当前会话 ID、是否本地执行，确认事件 schema 与本文官方链接一致。不读取 transcript/history 以寻找 ID；ID 应由用户明确提供或由其授权查看的当前 hook 元数据取得。本工具不全局注册、自动发现或自动授权会话。无已验证版本 allowlist，因此绑定要求显式 `--accept-unverified-version`；这是承认**仅 schema fixture 验证**，不是升级为可信兼容。版本变化需要重新审阅并撤销旧绑定、新建绑定。Cursor 校验 payload 自带版本；Claude/Codex 未依赖非文档版本字段，命令中的版本 pin 由配置者维护，不能自动发现其升级。

官方依据（2026-11-02）：[Claude](https://code.claude.com/docs/en/hooks)、[Cursor](https://cursor.com/docs/hooks)、[Codex](https://learn.chatgpt.com/docs/hooks)、[桌面模式](https://learn.chatgpt.com/docs/app)。Codex local-only 与 cloud orchestration hooks 的限制不同；不要将本地 shell 配置直接承诺用于 Work Cloud。

## 一次显式绑定（命令示例，不是安装指令）

在用户已经授权记录该项目/会话元数据后，将占位值换为**已确认值**：

```bash
python /path/to/skill/scripts/manual_ingress.py --root /absolute/project bind \
  --binding-id manual-worker-1 --thread-id actual-thread --participant actual-participant \
  --vendor codex --session-id ACTUAL_SESSION_ID --tool-version ACTUAL_INSTALLED_VERSION \
  --consent-activity --accept-unverified-version
```

thread/participant 必须已存在，稳定 identity 在绑定时捕获。工具名不是人名。同一项目中同 vendor/session 只能有一个有效绑定；跨项目绑定必须分别明确授权，根路径在每次事件校验，不猜线程。没有 `--consent-human` 就不能通过该绑定记录人类原文；活动同意并不包含原文同意。授权保存在 `.ai-collab/local/manual-ingress/`，默认不 git-sync，`.gitignore` 防普通 add，但这不是加密或对本地文件写入者的身份认证。

## 供用户审阅的 hooks 配置形状

这些片段**不会被本工具写到用户配置**。用户授权安装前，应合并审阅现有配置，保留既有 hooks，确认绝对脚本路径与本地 Python；不要覆盖原文件。命令接收当前 callback JSON 的 stdin，输出中性 JSON，不阻断、不自动 follow-up。失败返回非零且不回显原文；各工具错误显示由其版本决定。

Codex `.codex/hooks.json` 示例（用户须通过 `/hooks` 审阅并信任；不要绕过信任）：

```json
{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"python /path/to/skill/scripts/manual_ingress.py --root /absolute/project hook --binding-id manual-worker-1 --tool-version ACTUAL_INSTALLED_VERSION"}]}],"Stop":[{"hooks":[{"type":"command","command":"python /path/to/skill/scripts/manual_ingress.py --root /absolute/project hook --binding-id manual-worker-1 --tool-version ACTUAL_INSTALLED_VERSION"}]}],"Interrupt":[{"hooks":[{"type":"command","command":"python /path/to/skill/scripts/manual_ingress.py --root /absolute/project hook --binding-id manual-worker-1 --tool-version ACTUAL_INSTALLED_VERSION"}]}]}}
```

Claude 设置中的 `hooks` 结构同上，但使用 `UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop`、`StopFailure`、`SessionEnd`，**不要配置 Codex 的 Interrupt 事件**。每个 handler 为 `{"type":"command","command":"<上述 hook 命令，使用该会话绑定>"}`。本例只描述命令 hook，不使用 prompt/agent 类型，不引入额外模型调用。

Cursor `.cursor/hooks.json` 示例形状：

```json
{"version":1,"hooks":{"beforeSubmitPrompt":[{"command":"python /path/to/skill/scripts/manual_ingress.py --root /absolute/project hook --binding-id manual-worker-1 --tool-version ACTUAL_INSTALLED_VERSION"}],"stop":[{"command":"python /path/to/skill/scripts/manual_ingress.py --root /absolute/project hook --binding-id manual-worker-1 --tool-version ACTUAL_INSTALLED_VERSION"}]}}
```

可同样按其官方格式配置本适配器支持的 `preToolUse`/`postToolUse`/`postToolUseFailure`/`sessionEnd`。当前代码只白名单接收上表事件。未知事件拒绝，不猜字段。绑定另一 vendor 时必须使用对应 vendor 的绑定，不能混发。

## 原文不会自动收集

hook 的 prompt 只做基本 schema 检查，不落盘；不会保存工具输入/输出、环境、思考内容、附件、transcript_path 或读取其指向文件。`UserPromptSubmit` 也可能是定时、子 agent 回报或自动续跑，**永不凭事件名自动判成人类**。无法确认来源就只留活动元数据。

只有用户另外明确同意当前原文记录，绑定才加 `--consent-human`。后续显式输入严格既有 human-record JSON：

```bash
python /path/to/skill/scripts/manual_ingress.py --root /absolute/project human \
  --binding-id manual-worker-1 < /explicitly-supplied/current-human-event.json
```

**不需要人类每轮手写 JSON。** 已授权的 skill 使用者可把用户明确提供的当前原文传到友好入口；作者与目标绑定来自明确指令，不猜：

```bash
python /path/to/skill/scripts/manual_ingress.py --root /absolute/project human-text \
  --binding-id manual-worker-1 --author ACTUAL_HUMAN_SLUG \
  --confirm-original --confirm-authorized < /explicitly-supplied/current-original.txt
```

也可直接通过 stdin 传原文，无需建立该文本文件。这是手工原文入库声明，不是外部聊天自动捕获；创建时间是这次手工记录创建时刻，不冒充外部消息发送时间；生成的 ID 仅为本地 entry ID。两个确认选项由已获实际授权的调用方填写，不能替用户批准。没有安全来源信号时，统一 hooks 仍不承诺全自动原文采集。

不从 hook 到此命令自动连线。JSON 规范见 [ui-observability](ui-observability.md#explicit-human-input-not-agent-paraphrase)：actor_type、真实 body、timezone created_at、明确 author/recipients、原文及授权声明。recipients 必须恰为本绑定 participant；created_at 在绑定生效后，禁止历史回填。manual 来源可无 source IDs；authorized-import 要求真实 source IDs 且 source_conversation_id 等于绑定会话；不能填伪 ID。声明不能代替用户实际授权。现有不可变记录/重复及冲突校验不变。

## 自报、读取、撤销与卸载

```bash
python /path/to/skill/scripts/manual_ingress.py --root /absolute/project self-report --binding-id manual-worker-1 --status working
python /path/to/skill/scripts/manual_ingress.py --root /absolute/project snapshot
python /path/to/skill/scripts/manual_ingress.py --root /absolute/project revoke --binding-id manual-worker-1
```

自报是该协作者实际调用的声明，不要设置后台定时器假续租。撤销立即使 hook/human/self-report 拒绝后续写入，快照显示 revoked；历史已记录人类原文不自动删除。卸载须由用户审核后仅删除本功能对应 hook handler，保留其他 hooks；若要求删除本地历史，另外明确范围授权，不自动删文件。

## 投递、乱序与故障边界

本地 callback 默认以数据库事务的**接收顺序**排序，不冒充 vendor 原始时间。可选 `--delivery-id` 支持可信投递器幂等重试；不提供时每次 callback 独立观察，不能保证 crash 后重试去重。Python API 的 `sequence/observed_at` 是可信 collector 提供的接收元数据，不是模型自填 provenance。Codex/Cursor 当前 turn 的结束不能被同 turn 重放打开，旧 turn 工具/结束事件不能覆盖新 turn。Claude 文档 payload 无 turn ID，结束后只有新 prompt 能开启新的接收 epoch，不能完全区分延迟旧 prompt；因此仍仅显示最近事件，租约过期降为未知。SessionEnd 后要求显式新绑定。

输入错误/版本不匹配/目录不可写不升级信任，不记录正文；快照损坏返回 errors，不能显示活跃。源进程崩溃未投递结束事件仍会过期。不要因为快照最近有事件而自动发起工作。

## 测试

```bash
python -m unittest discover -s tests -p 'test_manual_ingress.py' -v
python -m unittest discover -s tests -v
```

全为合成文档结构、临时目录、模拟时间；不会运行真实 vendor、安装 hooks 或读取真实会话。UI 消费应使用 `manual_ingress.snapshot(roots).observations` 独立证据区，不改变旧 process runtime API 的含义。
