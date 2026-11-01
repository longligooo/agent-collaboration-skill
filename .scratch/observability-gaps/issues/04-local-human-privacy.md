# 04 · 人类原始记录默认本地

Type: task

Status: resolved

Owner: privacy implementer

Blocked by: 无

**What to build:** 实现 spec.md D4，严格遵守 D5 授权边界。

## Acceptance
- 临时 Git 仓库验证 git-sync 不上传新记录/旧已跟踪修改，旧记录可读，无数据删除。
- 完整回归与独立 REVIEW。

## Comments

- 2026-10-30 privacy implementer：本地 store 与旧数据合并读取、跨 store 幂等、fingerprint 已接入。临时 Git/bare remote 隐私测试 13 项通过；已暂存/跟踪数据字节保留，未发布私有祖先（含随后删除）阻止上传，实际 pushurl/多目标/重写目标与陈旧 upstream 已覆盖。30 项 human-event 回归通过。未更改任何真实 Git 历史或远端，等待独立 REVIEW。
- 2026-10-30 privacy implementer：独立 REVIEW 后补齐实际 pushurl、多 push URL、pushInsteadOf、pushRemote/remote.pushDefault、explicit refspec、替换引用与本地 symlink 防护；当前 19 项隐私用例、30 项 human-event 与 44 项 task-model 用例通过。原已上传记录不追溯清除；手工 git -f/push 与任意其他文件复制不属于自动保护保证。

- 2026-10-30 REVIEW：独立复核无剩余阻塞项；冻结代码完整回归229项通过，语法与diff检查通过。验证范围/浏览器环境阻塞见 ../validation.md；未commit/push，发布仍需单独决定。
