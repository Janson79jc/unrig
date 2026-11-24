# Unrig

[![Unrig CI](https://github.com/Janson79jc/unrig/actions/workflows/ci.yml/badge.svg)](https://github.com/Janson79jc/unrig/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/unrig.svg)](https://pypi.org/project/unrig/)
[![npm](https://img.shields.io/npm/v/%40janson79jc%2Funrig.svg)](https://www.npmjs.com/package/@janson79jc/unrig)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

[English](README.md) | **简体中文**

**面向 Coding Agent 的确定性防作弊门禁：事前拦截、增量/新鲜度裁判与隔离盲审。**

> 阻止 Coding Agent 偷改测试断言（`assert True` / `@ts-ignore`）、传 `--no-verify`、篡改门禁或交过期 `junit.xml` 报告（与 3D 骨骼绑定项目 `UniRig` 无关联）。

**Unrig** 把验证边界彻底锁死——**100% 离线 / 零遥测 / 零第三方运行时依赖（仅使用 Python >= 3.10 标准库）**，且**对存量老项目零侵入**（无需在业务源码中写任何特殊注解或标记）。

**5 秒零配置本地试跑 10 个内置逃逸回放场景：**

```bash
uvx unrig replay
# 或使用 npm：
npx @janson79jc/unrig replay
```

实测输出示例（`python examples/escape-replay/run_demo.py`）：

```text
Unrig Escape-Replay Standalone Demo:
  [BLOCKED CLEANLY] MUT-T01 (Unstaged L0 tamper + disarm)
      -> [FAIL] [L0_TAMPER] .unrig.yaml: Protected L0 gate file '.unrig.yaml' has uncommitted git status [ M].
  [BLOCKED CLEANLY] MUT-W02 (Duplicate baseline violation)
      -> [FAIL] [TAUTOLOGICAL-ASSERT] tests/test_legacy.py:5 -> Tautological `assert True` proves nothing. (delta=+1, after=2, before=1)
  [BLOCKED CLEANLY] MUT-W03 (Nested untracked test stub)
      -> [FAIL] [EMPTY-TEST-STUB] pkg/nested/test_stub.py:1 -> Empty test stub `test_placeholder()`. (delta=+1, after=1, before=0)
  [BLOCKED CLEANLY] JUNIT-STALE (Backdated XML report)
      -> [FAIL] [REPORT_STALE] JUnit report 'test-results/junit.xml' is older than source file 'app.py'.
  [BLOCKED CLEANLY] HOOK-L0-BLOCK (PreToolUse traversal & --no-verify)
      -> [UNRIG L0 HOOK BLOCKED] Direct modification to protected L0 file '.unrig/ratchet.json' is forbidden. | [UNRIG L0 HOOK BLOCKED] `--no-verify` / `git commit -n` bypass is forbidden.
```

---

## 架构一览

```text
  Agent 工具调用 (Write / Edit / Bash)
                 │
                 ▼
  ┌─────────────────────────────┐   exit 2 / JSON deny (拦 L0 写入、路径穿越、`>` 重定向、`--no-verify`)
  │   1. `unrig hook` (L0 守卫) │────────────────────────────────────────────────────────────────► 直接阻断
  └──────────────┬──────────────┘
                 │ exit 0 / allow
                 ▼
  ┌─────────────────────────────┐
  │ 2. `unrig verify` 裁判总检  │
  │   • L0 完整性校验           │── git `-uall -z` + `HEAD:.unrig.yaml` 并集锁 + CRLF 归一化 SHA-256
  │   • 多语言注释消声          │── 1:1 保留行号 (Py, JS/TS `/regex/`+`${}`, Vue, Java, Go, XML, SQL, SH, YAML)
  │   • `CounterMultiset` 增量  │── 仅抓 `(after - before)` 空桩、`DUMMY-CONSTANT-RETURN`、`CLONE-SPAM`、`TYPE-ESCAPE`、抑制指令与 `.unrig/ratchet.json`
  │   • JUnit 与覆盖透明度      │── `<testsuite>`/`<testsuites>`、报告新鲜度、`skipped == 0`、`skipped_ignored`/`syntax_fallback`/`SHALLOW_HISTORY`
  └──────────────┬──────────────┘
                 │ [PASS]
                 ▼
  ┌─────────────────────────────┐
  │ 3. 隔离盲审员 `[🛑]`         │   干净上下文只读审查 git diff（不泄露写码者的自辩与推导）
  └─────────────────────────────┘
```

---

## 为什么不只用普通 Linter / `pre-commit`？

| 逃逸模式 | 普通 Linter / `pre-commit` | Unrig |
|---|---|---|
| Agent 修改 `.unrig.yaml` 或传 `--no-verify` | 直接绕过 | 写入前由 `unrig hook` 物理拦断，配合 `-uall -z` + `HEAD` 并集锁捕获未暂存篡改 |
| Agent 在老文件里复制原有坏写法 | 按行 diff 易漏或让整份老文件全红 | `CounterMultiset(after - before)` 精确捕获 `+1` 净增量，不打扰未动的老债 |
| Agent 伪造通过信号（`assert True`、`.only`、`skipped=1`、`@ts-ignore`） | 照样编译通过、CI 变绿 | AST/词法测试空桩/聚焦/吞异常/抑制检查 + `forbid_skipped: true` + 必跑方法校验 |
| Agent 改完源码却提交旧绿 `junit.xml` | 仅检查文件存在即放行 | 判定为 `[REPORT_STALE]`（`xml_mtime < max_source_mtime - 1s`）直接阻断 |
| Agent 用 `return True` 假实现或复制 3 份以上同构测试凑数 | 测试全绿且覆盖数虚高 | `DUMMY-CONSTANT-RETURN` + 跨文件 `Alpha-Normalized` 增量聚类 `CLONE-SPAM` 直接拦截 |
| 外部运行依赖或云端联网 | 依赖重或需连外网 | 零三方运行时依赖（纯 Python 标准库）、100% 断网可用、零遥测 |

---

## 快速上手

```bash
# 从 PyPI 一条命令安装：
pip install unrig
# 或免污染全局直接运行 / 隔离安装（支持 uvx / npx / pipx / npm）：
# uvx unrig replay
# npx @janson79jc/unrig replay
# pipx install unrig
# npm install -g @janson79jc/unrig

# 或从源码克隆安装：
# git clone https://github.com/Janson79jc/unrig.git && cd unrig && pip install -e .

# 1. 初始化 `.unrig.yaml` 与 `.unrig/` 协议及盲审模板
#    （加 `--client claude|antigravity|gemini|codex|all` 可直接生成对应客户端 Hook 配置）
unrig init
git add .unrig.yaml .unrig/ && git commit -m "chore: initialize unrig Level 0 gates"

# 建议在项目的 `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` 加上一句引导：
# “请遵守 .unrig/PROTOCOL.md，交付前运行 `unrig verify`，并配合 .unrig/blind_auditor_prompt.md 做独立盲审。”

# 2. 运行验证裁判
unrig verify

# 3. 随着旧债清理，检查并单向收紧 `.unrig/ratchet.json` 上限
unrig ratchet --check
unrig ratchet --prune

# 4. 查看本地静默拦截台账（记录在自忽略目录 `.unrig/.cache/events.jsonl` 中）
unrig stats --days 7

# 5. 运行 Unrig 自带的 10 场景逃逸回放自测
unrig replay

# 可选：运行独立的 5 场景逃逸演示脚本
python examples/escape-replay/run_demo.py
```

---

## 接入 Agent 生命周期 Hook（`PreToolUse` / `BeforeTool` / `Stop`）

`unrig hook` 会根据 `stdin` JSON 自动识别调用方协议（也可显式指定 `--client claude|antigravity|gemini|codex`）：

- **Claude Code**（`unrig init --client claude` 生成 `.claude/settings.json`）：对 `Write|Edit|MultiEdit|Bash` 阻断时以退出码 **`2`** 返回并向 `stderr` 输出原因。
- **Google Antigravity**（桌面 App、IDE 与 `agy` CLI 通用，`unrig init --client antigravity` 生成 `.agents/hooks.json`）：`PreToolUse` 匹配 `run_command|write_file|edit_file|write_to_file|replace_file_content|...`，通过 `stdout` 返回 `{"decision": "allow"}` / `{"decision": "deny", "reason": "..."}`（放行只读查看工具），并在 `Stop` 钩子自动执行 `unrig verify`（不通过则返回 `{"decision": "continue", "reason": "..."}` 打回返工；连续 2 次同因反弹后自动释放交由人类介入）。
- **Google Gemini CLI**（`unrig init --client gemini` 生成 `.gemini/settings.json`）：针对 `BeforeTool` 通过 `stdout` 返回 `{"decision": "allow"}` / `{"decision": "deny", "reason": "..."}`。
- **OpenAI Codex CLI**（`unrig init --client codex` 生成 `.codex/hooks.json`）：在 `PreToolUse`（`Bash` matcher）调用 `unrig hook --client codex`，阻断危险命令、重定向与 `--no-verify` 并以退出码 **`2`** + `stderr` 返回原因（配合在 `AGENTS.md` 中声明遵守 `.unrig/PROTOCOL.md` 与交付前跑 `unrig verify`，确保补丁/写文件改动在收尾时同样受检）。

示例 `.claude/settings.json`（Codex `.codex/hooks.json` 使用 `"matcher": "Bash"` 与 `"command": "unrig hook --client codex"`）：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "unrig hook --client claude"
          }
        ]
      }
    ]
  }
}
```

只要任何工具试图改动 `protected_l0_files` 中的路径（包括客户端 Hook 配置文件 `.claude/settings.json`、`.agents/hooks.json`、`.gemini/settings.json` 和 `.codex/hooks.json`），哪怕使用 `../` 路径穿越、Windows 反斜杠、`> .unrig.yaml` 等 shell 输出重定向，或 `git ... commit/push --no-verify`，`unrig hook` 都会确定性拦截。

> **纵深防御边界**：`unrig hook` 是在 Agent 工具层防误触与常见逃逸的第一道快速护栏；基于 Git 工作区状态、`HEAD:.unrig.yaml` 并集锁与 CRLF 归一化哈希的 `unrig verify` 才是验收与 CI 合入前的最终确定性兜底。

---

## `.unrig.yaml` 配置参考

| 配置项 | 默认值 | 用途 |
|---|---|---|
| `version` | `1` | 配置 Schema 版本 |
| `protected_l0_files` | `[.unrig.yaml, .unrig/ratchet.json, .unrig/PROTOCOL.md, .unrig/blind_auditor_prompt.md, .claude/settings.json, .agents/hooks.json, .gemini/settings.json, .codex/hooks.json]` | 受保护的 L0 文件列表，始终与不可变默认清单及 `HEAD:.unrig.yaml` 取并集锁定 |
| `reports.require_reports` | `false` | 若未匹配到任何 JUnit XML 则以 `[REPORT_MISSING]` 失败 |
| `reports.forbid_skipped` | `true` | 任意 JUnit XML 中存在跳过用例（含 `<testcase><skipped/></testcase>`）即失败 |
| `reports.junit_globs` | Surefire / `junit*.xml` / `test-results/*.xml` | JUnit XML 报告发现通配符 |
| `reports.required_tests` | `{}` | 必须真实非跳过执行的 `{suite_name: [method_a, ...]}` 映射 |
| `reports.require_fresh_against` | `[]` | 自定义用于新鲜度时间戳比对的源码 glob（默认扫描全部非忽略源码） |
| `reports.privilege_log_tokens` | `["--no-verify", "gate.mode=maintenance"]` | 禁止在测试 XML 日志 / `.txt` 输出中出现的特权绕过词 |
| `runtime_proof_artifacts` | `[]` | 需校验新鲜且非空的 `{path, max_age_seconds}` 运行时产物列表 |
| `diff_checks.enabled` | `true` | 开关 `CounterMultiset(after - before)` 增量扫描 |
| `diff_checks.baseline` | `"HEAD~1"` | Git 基线版本（单提交仓自动回退 `HEAD`；浅克隆仓会给出 `[WARN] [SHALLOW_HISTORY]`） |
| `diff_checks.forbidden_patterns` | `[]` | 团队自定义 `{id, pattern, message, paths}` 规则（对消声后增量代码匹配） |
| `diff_checks.clone_warn_threshold` | `2` | 跨文件净新增 `Alpha-Normalized` 同构代码块达到该数时输出 `[WARN] [CLONE-SPAM]` |
| `diff_checks.clone_block_threshold` | `3` | 跨文件净新增 `Alpha-Normalized` 同构代码块达到该数时以 `[FAIL] [CLONE-SPAM]` 阻断 |
| `diff_checks.clone_min_tokens` | `15` | 参与增量克隆检测的最小归一化 token 数 |
| `ratchet_file` | `".unrig/ratchet.json"` | 棘轮豁免台账路径，每条历史豁免均强制要求非空 `reason`（>= 3 字符） |
| `shell_write_redirect_guards` | `true` | 在 `unrig hook` 中检查 shell 命令对 L0 文件的重定向写入与 `--no-verify` |

---

## 离线与隐私保证

`unrig` **不发起任何网络请求**，也**不包含任何遥测收集**。所有配置、棘轮台账与本地拦截记录均严格保留在你自己的机器本地。

---

## 参与贡献、Agent 规范与安全

- **贡献指南与 AI 披露政策**：见 [CONTRIBUTING.md](CONTRIBUTING.md)（人类第一责任 + PR AI 辅助披露）。
- **测试规范与规则门槛**：见 [TESTING.md](TESTING.md)。
- **开发本仓的 Agent 指南**：见 [AGENTS.md](AGENTS.md)。
- **安全漏洞与门禁绕过披露**：见 [SECURITY.md](SECURITY.md)。
- **社区行为准则**：见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- **开源协议**：[Apache-2.0](LICENSE) — 版本更新详情见 [CHANGELOG.md](CHANGELOG.md)。
