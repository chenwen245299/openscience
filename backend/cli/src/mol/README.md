# Mol agent

MolSessions 下的会话跑在这里的这份 research agent 副本上。复制时与原版逐行一致，之后可以随意修改，研究会话仍然使用原版，原版代码不会导入这里的任何文件。

## 哪些是副本

| 文件           | 复制自                                                    | 负责                                                                                             |
| -------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `loop.ts`      | `src/session/prompt.ts` 里的 `execute` 及它用到的逻辑函数 | 一个回合：恢复、提醒、选工具、上下文预检、分步、结束判断                                         |
| `processor.ts` | `src/session/processor.ts`                                | 一步：调用模型、处理流、执行工具调用、守卫                                                       |
| `llm.ts`       | `src/session/llm.ts`                                      | 组装模型请求和系统提示词                                                                         |
| `system.ts`    | `src/session/system.ts` 里按模型家族选头部的部分          | 选出 `prompt/` 下的头部，填入 science 块和写作规范                                               |
| `prompt/*.txt` | `src/agent/prompt/`、`src/session/prompt/`                | 六个模型家族头部、`science.txt`、`response.txt`、`plan.txt`、`build-switch.txt`、`max-steps.txt` |

## 哪些仍然共用

- 回合的准入、取消和循环租约：`SessionPrompt.loop` 统一负责，所以停止按钮、忙碌状态、实时事件对两种会话都一样。`loop.ts` 回合结束时通过 `SessionPrompt.state()` 通知同一批等待者。
- 执行时的权限检查（`SessionPrompt.permissionAtExecution`）、会话深度、工具 schema 转换、标题生成。
- 工具、沙箱、技能、模型服务、会话存储、上下文压缩（`SessionCompaction`），以及通过 `loop.before_finish` / `loop.guard` 钩子介入的 harness 单元。
- 子任务派生出的 worker 会话不带 `loop`，走原版研究循环。

## 怎么接进来的

会话记录上的 `loop` 字段决定走哪个循环：`POST /session` 传 `loop: "mol"`，分叉保留原值。`SessionPrompt.loop` 里只有一行分发：

```ts
if (session.loop === "mol") return await MolLoop.execute(sessionID, session, abort)
```

## 测试

`test/session/mol-loop.test.ts` 验证 MolSessions 的回合走 `MolLoop` 和 `MolLLM`，研究会话走原版。修改这里的流程后，先跑：

```bash
bun test --timeout 15000 ./test/session/mol-loop.test.ts
```

原版以后修的 bug 不会自动进入这份副本；需要时把原文件和这里的文件对比着合并。
