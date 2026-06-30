# 设计:强化 python 逃生舱 = 动态工具能力

> 状态:Design+Build · 范围:`pipeline/node_specs.py`(python 描述)、`pipeline/loop_driver.py`(UPSTREAM_HANDLES + _LOOP_SYSTEM) · 关联:用户"没有现成工具时现场写工具应对各种情况"的诉求、`architecture-prefer-simplicity`

## 1. 背景
用户想要"loop 判断没有可用工具时,能现场编写工具应对各种情况"。VS 其实已有 **`python` 逃生舱**:`inputs.instruction`(自然语言)→ CodeGenerator 写 Python → 沙箱执行 + 自愈。但两点限制让它没真正成为"通用逃生舱":
1. **data_result_id 必填**(UPSTREAM_HANDLES 里 python 的句柄不在 `_OPTIONAL_HANDLE`)→ 只能"对上游数据做分析",不能独立计算/生成。
2. **大脑不太会主动用它** —— `_LOOP_SYSTEM` 没有"内置工具都不合适时就写代码"的引导。

## 2. 设计(强化现有逃生舱,不另造框架 —— 别加层)
1. **python 的 data_result_id 改【可选】**(加进 `_OPTIONAL_HANDLE`)→ 既能带上游数据分析,也能【不带上游】独立写代码(纯计算 / 生成 / 转换)。沙箱对空上游本就安全(只是不注入额外变量)。
2. **拓宽 python planner_desc**:任何【没有现成专用工具能表达】的计算 / 分析 / 转换都用它现场写代码;可带上游、也可不带。
3. **`_LOOP_SYSTEM` 加一条逃生舱引导**:遇到内置工具都不合适的 novel 需求 → 用 python 现场写代码(把要干什么描述清楚),别硬塞不合适的工具、也别放弃。

## 3. 非目标(刻意不做)
- 不做"把现场写的代码注册成【持久命名工具】"——复杂、安全面大,且逃生舱已覆盖"一次性 novel 计算"。真要复用,值单独立项。
- 不放开沙箱权限 —— 仍受沙箱 `_check_policy` + 潘多拉隔离约束(动态代码不等于无约束)。

## 4. 改动点 + 测试
| 文件 | 改动 |
|---|---|
| `loop_driver.UPSTREAM_HANDLES` / `_OPTIONAL_HANDLE` | python 的 data_result_id 改可选 |
| `node_specs` python.planner_desc | 拓宽成"通用逃生舱:可带/不带上游" |
| `loop_driver._LOOP_SYSTEM` | +一条"内置工具不合适 → 写 python"的引导 |

**测试**:`loop_function_declarations` 里 python 的 `data_result_id` 在 properties 但【不在 required】(可选);沙箱跑无上游 python 属 e2e(需沙箱),非离线单测。
