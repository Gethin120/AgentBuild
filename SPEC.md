# 结伴出行会合点规划 P0 接口规格

本文档定义当前产品 MVP 的 `P0` 输入输出契约，用于统一以下模块之间的边界：

- 自然语言意图解析
- 确定性规划引擎
- 策略与评审
- 结果表达层
- 指标与日志采集

目标不是一次性定义最终形态，而是为当前阶段提供一个稳定、可执行、便于迭代的接口标准。

## 1. P0 目标

P0 只解决一个核心问题：

- 用户用自然语言输入双地出发、共同目的地和偏好约束
- 系统自动生成会合点
- 系统返回 1 到 3 个可执行方案
- 系统说明推荐理由
- 系统记录关键过程数据

## 2. 模块边界

P0 推荐分为 5 层：

1. `user_request`
   接收原始自然语言输入

2. `intent`
   将自然语言转为结构化规划意图

3. `plan_request`
   将结构化意图转换为可供引擎执行的规划请求

4. `plan_result`
   返回满足硬约束的候选方案与诊断信息

5. `response_payload`
   面向用户输出推荐结果、备选方案与解释

## 3. 输入契约

### 3.1 原始输入 `user_request`

类型：

```json
{
  "user_request": "string"
}
```

说明：

- `user_request` 为用户自然语言原文
- 必须保留原始文本，便于后续回放、评估与优化

示例：

```json
{
  "user_request": "我从上海嘉定区曹安公路4750号驾车出发，朋友在上海张江高科地铁站，我们一起去苏州西山岛。自动找会合点，朋友公交地铁不超过100分钟，我最多绕路90分钟，最多等待45分钟。"
}
```

### 3.2 结构化意图 `intent`

这是 `intent parser` 的标准输出，也是后续策略层和规划层的标准输入。

类型：

```json
{
  "driver_origin_address": "string",
  "passenger_origin_address": "string",
  "destination_address": "string",
  "geocode_city": "string",
  "candidate_mode": "auto | manual",
  "pickup_addresses": ["string"],
  "constraints": {},
  "weights": {
    "arrival_weight": 0.55,
    "wait_weight": 0.25,
    "detour_weight": 0.20
  },
  "top_n": 3,
  "driver_departure_delay_min": 0,
  "passenger_departure_delay_min": 0,
  "replan_context": {},
  "auto_pickup": {
    "limit": 20,
    "radius_m": 1000,
    "sample_km": 5.0,
    "keywords": "地铁站|公交站|停车场|商场"
  }
}
```

字段要求：

- `driver_origin_address`
  驾车用户出发地，必填
- `passenger_origin_address`
  公共交通乘客出发地，必填
- `destination_address`
  共同目的地，必填
- `geocode_city`
  地理编码城市提示，选填；若跨城或不确定，必须为空字符串
- `candidate_mode`
  候选点模式，`auto` 表示自动找点，`manual` 表示使用用户手工输入候选点
- `pickup_addresses`
  `manual` 模式下必填，`auto` 模式下可为空数组
- `constraints`
  用户明确提出的硬约束；若用户未明确表达，可为空对象
- `weights`
  排序权重，作为软偏好表达
- `top_n`
  最多返回方案数
- `driver_departure_delay_min`
  司机延迟出发分钟数，默认 `0`
- `passenger_departure_delay_min`
  乘客延迟出发分钟数，默认 `0`
- `replan_context`
  若当前请求来自一次动态重规划，则记录触发事件上下文
- `auto_pickup`
  自动候选点生成参数，仅在 `auto` 模式生效

P0 默认值：

- 不为 `constraints` 注入默认硬约束
- `arrival_weight = 0.55`
- `wait_weight = 0.25`
- `detour_weight = 0.20`
- `top_n = 3`
- `driver_departure_delay_min = 0`
- `passenger_departure_delay_min = 0`
- `auto_pickup.limit = 20`
- `auto_pickup.radius_m = 1000`
- `auto_pickup.sample_km = 5.0`
- `auto_pickup.keywords = "地铁站|公交站|停车场|商场"`

P0 校验规则：

- 三个核心地址字段不能为空
- `candidate_mode` 只能为 `auto` 或 `manual`
- `manual` 模式下 `pickup_addresses` 不能为空
- `weights` 三项之和应接近 `1.0`
- 约束值必须为正整数
- `top_n` 必须为正整数，建议范围为 `1` 到 `5`

## 4. 规划请求契约

### 4.1 内部规划请求 `plan_request`

这是进入确定性规划引擎前的内部对象，不一定直接对外暴露，但必须稳定。

逻辑字段：

- `driver_origin`
- `passenger_origin`
- `destination`
- `departure_time`
- `pickup_candidates`
- `driver_mode`
- `passenger_mode`
- `constraints`
- `weights`
- `top_n`

说明：

- `driver_origin`、`passenger_origin`、`destination` 必须被解析为经纬度对象
- `pickup_candidates` 必须是已完成地理解析的候选点列表
- `constraints` 只包含硬约束
- 未明确输入的硬约束不得由系统自动补齐为 `120/90/45`
- `weights` 只影响排序，不得覆盖硬约束过滤结果

P0 强约束：

- 乘客到会合点耗时不得超过 `passenger_travel_max_min`
- 司机接人绕路时间不得超过 `driver_detour_max_min`
- 会合等待时间不得超过 `max_wait_min`
- 地址地理编码失败时，规划必须失败并返回清晰原因

## 5. 输出契约

### 5.1 规划结果 `plan_result`

这是规划引擎与上层结果表达之间的标准结果对象。

类型：

```json
{
  "resolved_locations": {
    "driver_origin": { "name": "string", "lat": 0, "lon": 0 },
    "passenger_origin": { "name": "string", "lat": 0, "lon": 0 },
    "destination": { "name": "string", "lat": 0, "lon": 0 }
  },
  "pickup_candidates_count": 0,
  "options": [
    {
      "pickup_point": "string",
      "score": 0.0,
      "eta_driver_to_pickup": "ISO-8601 string",
      "eta_passenger_to_pickup": "ISO-8601 string",
      "pickup_wait_time_min": 0,
      "driver_detour_time_min": 0,
      "total_arrival_time": "ISO-8601 string"
    }
  ],
  "diagnostics": [
    {
      "pickup_point": "string",
      "reasons": ["string"]
    }
  ]
}
```

字段说明：

- `resolved_locations`
  三个核心地址解析后的结果
- `pickup_candidates_count`
  候选点总数，用于评估候选点生成质量
- `options`
  满足硬约束的可行方案列表，按得分降序排列
- `diagnostics`
  被过滤候选点与过滤原因，仅在调试或诊断模式下返回

P0 要求：

- `options` 中的所有方案必须满足硬约束
- 至少保留 `pickup_point`、等待时间、司机绕路时间、整体到达时间
- `diagnostics` 应尽量标准化原因枚举，避免后续统计困难

当前实现中常见的过滤原因包括：

- `passenger_travel_exceeded`
- `driver_detour_exceeded`
- `wait_time_exceeded`

### 5.2 面向用户的结果 `response_payload`

P0 建议把用户最终看到的结果标准化为以下结构，而不是仅返回自然语言长文本。

类型建议：

```json
{
  "status": "ok | no_solution | error",
  "summary": {
    "driver_origin_name": "string",
    "passenger_origin_name": "string",
    "destination_name": "string",
    "candidate_count": 0,
    "feasible_option_count": 0,
    "retry_count": 0,
    "preference_profile": "balanced | fast_arrival | min_wait | min_detour",
    "preference_label": "string",
    "is_replan": true,
    "replan_context": {},
    "replan_summary": {
      "title": "string",
      "reason": "string",
      "changes": ["string"]
    },
    "replan_delta": {
      "pickup_changed": true,
      "previous_pickup_point": "string",
      "current_pickup_point": "string",
      "wait_delta_min": 0,
      "detour_delta_min": 0,
      "arrival_delta_min": 0
    }
  },
  "recommended_option": {
    "pickup_point": "string",
    "pickup_wait_time_min": 0,
    "driver_detour_time_min": 0,
    "total_arrival_time": "ISO-8601 string",
    "reason": "string",
    "tradeoff_tags": ["string"],
    "recommendation_basis": "string",
    "preference_alignment": true
  },
  "alternative_options": [
    {
      "pickup_point": "string",
      "pickup_wait_time_min": 0,
      "driver_detour_time_min": 0,
      "total_arrival_time": "ISO-8601 string",
      "reason": "string",
      "tradeoff_tags": ["string"],
      "recommendation_basis": "string",
      "preference_alignment": false
    }
  ],
  "suggestions": [
    "string"
  ],
  "relaxation_suggestions": [
    {
      "field": "string",
      "current_value": 0,
      "suggested_value": 0,
      "reason": "string"
    }
  ],
  "primary_bottleneck": "string",
  "constraint_diagnostics": {
    "filtered_candidate_count": 0,
    "reason_counts": {
      "wait_time_exceeded": 0
    },
    "avg_exceed_by_reason": {
      "wait_time_exceeded": 0.0
    }
  },
  "share_text": "string",
  "share_card": {
    "title": "string",
    "subtitle": "string",
    "highlights": ["string"],
    "pickup_point": "string",
    "arrival_time": "string"
  },
  "error": {
    "code": "string",
    "message": "string"
  }
}
```

状态定义：

- `ok`
  成功返回至少一个可执行方案
- `no_solution`
  输入可理解，但当前约束下没有可行方案
- `error`
  输入解析失败、地址解析失败、地图服务失败或系统异常

P0 文案要求：

- `recommended_option.reason` 必须说明为什么它是首推方案
- `alternative_options.reason` 必须说明与首推方案的差异
- `tradeoff_tags` 和 `recommendation_basis` 应可直接供前端使用，用于展示“更少等待”“更少绕路”“更快到达”等标签
- `summary.preference_profile` 应明确当前结果更偏向哪类决策偏好
- `summary.is_replan` 和 `summary.replan_context` 应让上层知道该结果是否来自动态重规划
- `summary.replan_summary` 应用人话概括本次重规划触发原因和关键变更
- 若提供上一版结果，`summary.replan_delta` 应说明新旧首推方案差异
- `no_solution` 时必须提供至少一条 `suggestions`
- `no_solution` 时应尽量提供结构化 `relaxation_suggestions`
- `constraint_diagnostics` 应汇总候选点被过滤的主要原因和超限幅度，用于产品提示与评估统计
- `share_text` 应能直接用于转发给同行人
- `share_card` 应提供适合前端卡片、IM 分享卡片或消息模板复用的结构化字段
- `error.message` 必须面向用户可理解，不能仅暴露底层异常字符串

## 6.4 用户动作日志 `action_event`

为支持分享、确认和后续采纳分析，建议单独记录用户动作。

类型建议：

```json
{
  "time": "ISO-8601 string",
  "request_id": "string",
  "action": "share | confirm | replan | discuss | dismiss | open_navigation",
  "pickup_point": "string",
  "note": "string",
  "metadata": {}
}
```

说明：

- `request_id` 必须与规划结果一一对应
- `action` 用于统计分享率、确认率和互动率
- `pickup_point` 用于分析用户最终是否围绕推荐点执行
- `metadata` 预留给前端或渠道侧补充来源信息

## 6.5 重规划事件 `replan_event`

为支持途中变化后的重新规划，允许在已有 `intent` 基础上应用一次事件补丁。

类型建议：

```json
{
  "type": "passenger_delay | driver_delay | expand_wait | expand_detour | expand_passenger_travel | expand_search_radius | expand_pickup_limit",
  "delay_min": 0,
  "delta_min": 0,
  "reason": "string"
}
```

说明：

- `passenger_delay` 和 `driver_delay` 用于调整双方出发时间
- `expand_wait`、`expand_detour`、`expand_passenger_travel` 用于动态放宽约束
- `expand_search_radius` 和 `expand_pickup_limit` 用于扩大自动候选点搜索范围
- `reason` 用于记录触发重规划的背景

如果调用方能提供上一版结果，建议把上一版 `recommended_option` 作为上下文输入，以便系统输出本次重规划与上次方案的差异摘要。

## 6. 指标与日志契约

为支持后续 Agent 自优化，P0 需要最小日志结构。

### 6.1 单次请求日志 `trace_event`

建议至少包含：

```json
{
  "request_id": "string",
  "time": "ISO-8601 string",
  "stage": "string",
  "status": "start | done | error",
  "message": "string",
  "extra": {}
}
```

### 6.2 单次完整会话记录 `trace_session`

建议至少包含：

- `request_id`
- `user_request`
- `parsed_intent`
- `resolved_locations`
- `pickup_candidates_count`
- `filtered_candidates`
- `feasible_options_count`
- `recommended_option`
- `retry_count`
- `retry_reason`
- `final_status`
- `failure_category`

建议补充的 `failure_category` 枚举：

- `intent_parse_failed`
- `address_resolution_failed`
- `no_pickup_candidates`
- `constraints_too_strict`
- `planner_timeout`
- `upstream_api_error`
- `unknown_error`

### 6.3 P0 核心指标

至少支持计算：

- 请求成功率
- 可行方案产出率
- 约束命中率
- 无解率
- 重试收益率
- 候选点利用率

## 7. 错误处理规范

P0 需区分三类失败：

### 7.1 输入类失败

场景：

- 缺少核心地址
- 角色关系不明确
- 用户表达无法形成有效规划请求

处理要求：

- 返回 `status = error`
- 提示用户补充必要信息

### 7.2 无解类失败

场景：

- 地址可解析
- 候选点存在
- 但所有候选点都被硬约束过滤

处理要求：

- 返回 `status = no_solution`
- 解释主因
- 给出可操作的放宽建议

### 7.3 系统类失败

场景：

- 地理编码失败
- 地图 API 失败
- 超时
- 内部异常

处理要求：

- 返回 `status = error`
- 提供清晰错误码
- 保留底层错误到日志，但不要原样暴露给最终用户

## 8. P0 非目标

本规格暂不覆盖：

- 三人及以上多人会合
- 多目的地联动
- 完整地图 UI 交互
- 实时共享位置
- 履约、支付、订单能力

## 9. 当前代码对照

当前项目中，以下实现已基本接近本规格：

- `app/intent_and_planner.py`
  已定义 `intent` 基本字段，并能输出 `plan_result`
- `app/engine.py`
  已实现候选点生成、硬约束过滤、排序与诊断信息
- `app/core/response.py`
  已提供简化结果和自然语言输出，但尚未完全满足 `response_payload` 的结构化要求
- `app/agent.py`
  已具备策略重试、评审和 trace 记录骨架

当前最值得继续推进的差距有两个：

- 将 `response` 从自然语言拼接升级为稳定的结构化 `response_payload`
- 将 `trace` 从过程记录升级为可统计、可评估的产品与 Agent 指标输入
