---
name: vla-implement-phase
description: Use when implementing a Phase from implementation-plan.md — locates the target Phase (by number or "next"), extracts its "必读" + "验收" sections, walks TDD steps, runs acceptance code, and marks progress on success. Triggers on "实现 Phase N", "继续 Phase", "做下一个 Phase", "implement phase X", "重做 Phase N".
---

# vla-implement-phase

按 `implementation-plan.md` 的 Phase 推进实施。本 skill 是项目级编排器,**严格走 TDD**,每个 Phase 完成后才打勾。

---

## 进入 skill 前的硬性前置

- ✅ `uv run vla doctor` 全 OK(失败项必须先补,见 `.claude/CLAUDE.md`)
- ✅ git 工作区干净(`git status` 无未提交改动),或用户明确说"可以混着改"
- ✅ 已读 `.claude/CLAUDE.md`(项目级规范)

任一不满足 → 先解决,不进入 Phase。

---

## 输入解析

用户给的目标 Phase 用以下任一形式:

| 形式 | 示例 | 解析 |
|---|---|---|
| 显式编号 | "Phase 3" / "做 Phase 5" | 直接定位 |
| "下一个" / "继续" | "继续 Phase" / "下一个" | 看 implementation-plan.md 进度跟踪,找第一个未打勾的 `[ ]` |
| "重做 N" | "重做 Phase 2" | 清掉 Phase N 的 `[x]` 当作未完成,然后定位 |
| 模糊 | "把字幕那块搞了" | 提示用户给具体 Phase 编号 |

**解析后大声复述**:"即将实现 Phase N:<标题>(估算 Xh)" —— 等用户确认再继续。

---

## Phase 推进流程

### 1. 抽取 Phase 上下文

从 `implementation-plan.md` 提取该 Phase 段落,记录:

- **必读**:涉及的 FR 编号(去 `requirements.md` 读对应章节)
- **目标模块**:`src/vla/<module>/<file>.py` 清单
- **测试文件**:`tests/test_<module>.py` 清单
- **验收代码**:Phase 末尾"验收"块原文(整块保存,执行时一字不改)
- **预估工时**:Phase 标题里写的

### 2. 拆分任务清单

用 TaskCreate 为该 Phase 的每个子模块建一条任务(不要把整个 Phase 当一条任务)。模板:

```text
Phase N:<Phase 标题>
├── N.1 <模块 A>测试骨架 (RED)
├── N.2 <模块 A>实现 (GREEN)
├── N.3 <模块 B>测试骨架
├── N.4 <模块 B>实现
└── N.99 跑 Phase 验收代码 + 勾选进度
```

### 3. 走 TDD(每个子模块)

每个子模块严格按 `superpowers:test-driven-development`:

1. **写测试**:`tests/test_<module>.py` 加 case(happy path + 1 个失败 path)
2. **跑测试,确认 RED**:`uv run pytest tests/test_<module>.py -x`
3. **写最小实现**:`src/vla/<module>/<file>.py`
4. **跑测试,确认 GREEN**:`uv run pytest tests/test_<module>.py -x`
5. **跑 ruff / mypy**(如果项目已配)

### 4. 验收

跑 Phase 末尾"验收代码"块:

- **全过** → 走第 5 步(打勾)
- **部分过** → 用 `superpowers:systematic-debugging` 逐个修复,不要跳
- **全失败** → 检查测试是不是写错了,或 Phase 上下文理解有误,**停下问用户**

### 5. 打勾

只更新该 Phase 的进度跟踪勾选,**不**改其他 Phase 的状态:

```diff
- [ ] Phase N:<标题>
+ [x] Phase N:<标题>
```

### 6. 提议下一步

"Phase N 已完成 ✅。下一步:
1. 继续 Phase N+1
2. 先 git commit 当前 Phase
3. 暂停"

等用户选。

---

## 红线(违反即停)

| 触发 | 处理 |
|---|---|
| 用户问"为什么这样设计" | 跳到 `requirements.md` 对应 FR,**不**凭印象答 |
| 测试一直红但实现看起来对 | 走 `superpowers:systematic-debugging`,不堆改动 |
| 验收代码与 requirements.md 矛盾 | **停下问用户**,不擅自二选一 |
| 改动超出当前 Phase 范围 | 拒绝,要求新 Phase 或新对话 |
| Phase 跨度太大(>1 天工作量) | 提示用户拆 Phase,**不**自动拆 |
| 涉及 SSOT 变更(requirements.md) | 先 brainstorming 再改 |

---

## 不要做的事

- ❌ 不批量打勾(只勾当前 Phase)
- ❌ 不"先实现再补测试"(TDD 是红线)
- ❌ 不跳过验收代码直接说"做完了"
- ❌ 不在 Phase 中改 `requirements.md`(那是 brainstorm 的事)
- ❌ 不自动 git commit(让用户决定 commit 时机)
- ❌ 不引用 Phase 外的"经验"作为依据(必须看 implementation-plan.md 原文)

---

## 输出格式

每个子模块完成后,简短报告:

```text
✅ <模块名>(N.x)
   测试: 3 passed
   实现: src/vla/<module>/<file>.py
```

Phase 全部完成后:

```text
🎯 Phase N 完成
   测试: X passed
   验收: Y passed
   进度: Phase N 已勾选
   下一步: ...
```
