# 结伴而行 Agent 方案（V2）

## 目标
- 在保留 V1.0 的确定性路径计算能力基础上，引入 Agent 做意图理解、方案协商与动态重规划。
- 保证关键路径结果仍由地图 API 和规则引擎提供，Agent 负责“编排与解释”。

## 总体架构
1. `Conversation Agent`
- 输入：用户自然语言偏好（如“我最多等 15 分钟，优先地铁站接人”）。
- 输出：结构化规划请求（约束、权重、模式、策略）。

2. `Tool Gateway`
- 统一给 Agent 暴露工具：
  - `geocode_address`
  - `generate_pickup_candidates`
  - `estimate_segment_time`
  - `rank_pickup_options`
  - `replan_with_event`

3. `Deterministic Engine`（复用 V1.0）
- 候选点生成、路径耗时计算、约束过滤、排序评分。

4. `Policy Guardrails`
- 强约束必须由规则层校验：
  - 超出最大等待
  - 超出最大绕路
  - 地址地理编码不可信
- Agent 不能绕过约束直接输出“推荐结果”。

## Agent 增强点
1. 意图参数化
- 把用户语言映射为：
  - `passenger_travel_max_min`
  - `driver_detour_max_min`
  - `max_wait_min`
  - 评分权重

2. 多方案协商
- 输出 `A/B/C` 三方案并解释取舍：
  - 最快到达
  - 最少等待
  - 最少绕路

3. 动态重规划
- 触发事件：
  - 司机堵车
  - 乘客晚到
  - 地铁延误
- Agent 调用 `replan_with_event` 给新会合点建议。

## 工具契约（建议）
```json
{
  "tool": "plan_rendezvous",
  "input": {
    "driver_origin_address": "string",
    "passenger_origin_address": "string",
    "destination_address": "string",
    "geocode_city": "string",
    "candidate_mode": "manual|auto",
    "pickup_addresses": ["string"],
    "constraints": {
      "passenger_travel_max_min": "int",
      "driver_detour_max_min": "int",
      "max_wait_min": "int"
    },
    "weights": {
      "arrival_weight": "float",
      "wait_weight": "float",
      "detour_weight": "float"
    }
  }
}
```

## 版本策略
1. `V1.0`（冻结）
- 文件：`engine.py`
- 用途：稳定、可复现的规则引擎版本。

2. `V2`（Agent 编排）
- 本文件定义产品设计与接口约束。
- 现有实现参考：`agent_local.py` 与 `agent_langgraph.py`。

## 下一步实现建议
1. 先做 `intent_to_parameters` 模块（纯函数，便于测试）。
2. 把 V1.0 封装为 `plan_rendezvous()` 工具函数。
3. 增加 `explain_plan()`，输出自然语言解释和可视化摘要。
