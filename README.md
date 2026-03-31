# 结伴而行 Agent

一个面向周末结伴出游场景的会合点规划 Agent。

它解决的问题不是普通导航，而是：

- 两个人从不同地点出发
- 最终要去同一个目的地
- 系统自动找合适会合点
- 同时考虑公共交通、司机绕路、等待时间和用户偏好

当前版本已经达到 `P0`：具备自然语言输入、结构化意图、自动候选点生成、约束过滤、方案排序、结果解释、重规划基础能力，以及回放和测试闭环。

## 核心能力

- 自然语言转结构化意图
- 自动生成会合点候选
- 输出主推荐方案和备选方案
- 解释推荐理由与方案差异
- 无解时给出诊断和放宽建议
- 支持事件驱动的动态重规划
- 记录 trace、指标和用户动作，便于后续 Agent 自优化

## 适用场景

- 你开车，朋友坐地铁或公交，一起去郊区或景点
- 两人从不同城区出发，想自动找中间更合理的会合点
- 出发前有人晚点、堵车，需要快速重算

## 项目状态

当前仓库以 `P0` 为主，重点已经从“技术原型”推进到“产品 MVP”。

已完成：

- `P0` 输入输出契约
- 自然语言意图解析
- 自动候选点生成
- 硬约束过滤与排序
- 决策型结果表达
- 指标与日志骨架
- 回放样本与单元测试

下一阶段重点：

- 把“建议晚点出发”纳入排序，而不只是提示
- 继续压缩端到端耗时
- 累积真实 `share / confirm / replan` 行为数据

## 目录结构

```text
jiebanerxing/
├── app/
│   ├── agent.py
│   ├── engine.py
│   ├── intent_and_planner.py
│   └── core/
├── eval/
├── scripts/
├── tests/
├── PRD.md
├── SPEC.md
├── TASKS.md
└── run.sh
```

关键文件：

- [`PRD.md`](/Users/gethin/workspace/jiebanerxing/PRD.md)：产品定义与目标
- [`SPEC.md`](/Users/gethin/workspace/jiebanerxing/SPEC.md)：P0 输入输出契约
- [`TASKS.md`](/Users/gethin/workspace/jiebanerxing/TASKS.md)：研发任务清单和阶段结论
- [`app/agent.py`](/Users/gethin/workspace/jiebanerxing/app/agent.py)：主编排入口
- [`app/engine.py`](/Users/gethin/workspace/jiebanerxing/app/engine.py)：确定性规划引擎
- [`app/core/response.py`](/Users/gethin/workspace/jiebanerxing/app/core/response.py)：结构化结果与自然语言输出

## 工作流

1. `parse_intent`：自然语言请求转结构化意图
2. `strategy`：生成当前轮策略
3. `plan`：调用高德和规则引擎计算方案
4. `judge`：评审结果质量与风险
5. `retry`：必要时自动重试
6. `compose`：生成 `response_payload` 和自然语言结果
7. `persist`：写入 `.runs/trace.jsonl`

## 环境要求

- Python 3.9+
- 本地可访问的 LM Studio OpenAI 兼容接口
- 高德 Web Service Key

建议环境变量：

```bash
AMAP_WEB_SERVICE_KEY=你的高德key
LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
LM_STUDIO_API_KEY=你的LM Studio key
MODEL_NAME=qwen/qwen3.5-9b
```

说明：

- 代码会自动读取项目根目录下的 `.env` 和 `.env.local`
- 如果 LM Studio 开启了 API key 校验，当前版本会自动带 `Authorization: Bearer ...`

## 快速开始

日常手工测试：

```bash
cd /Users/gethin/workspace/jiebanerxing
sh ./run.sh "我从上海虹桥火车站出发，朋友在上海世纪大道地铁站，我们一起去上海迪士尼乐园。自动找会合点。"
```

默认只输出最终自然语言结果。

如果你想看完整调试信息、解析结果和 JSON 输出：

```bash
cd /Users/gethin/workspace/jiebanerxing
DEBUG=1 sh ./run.sh "我从上海虹桥火车站出发，朋友在上海世纪大道地铁站，我们一起去上海迪士尼乐园。自动找会合点。"
```

## 常用测试命令

单元测试：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 -m unittest discover -s tests -p 'test_*.py'
```

P0 一键检查：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 scripts/check_p0.py --skip-replay
```

P0 样本回放：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 scripts/replay_eval.py --artifacts-dir .runs/replay_artifacts
```

trace 汇总：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 scripts/summarize_trace.py
```

报告对比：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 scripts/compare_reports.py \
  --baseline-path .runs/check_report_prev.json \
  --current-path .runs/check_report.json
```

## 动态重规划

如果已经有一份结构化 intent，可以在其基础上注入事件重算：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 -m app.agent \
  --user-request "重规划" \
  --intent-json-path /path/to/intent.json \
  --replan-event-json-path /path/to/replan_event.json \
  --previous-response-json-path /path/to/previous_response.json \
  --output-json-path /path/to/output.json
```

支持的事件类型：

- `passenger_delay`
- `driver_delay`
- `expand_wait`
- `expand_detour`
- `expand_passenger_travel`
- `expand_search_radius`
- `expand_pickup_limit`

## 输出说明

当前系统对外有两层输出：

- 自然语言结果：适合命令行和聊天式使用
- `response_payload`：适合前端、日志分析和后续 Agent 自优化

`response_payload` 包含这些核心信息：

- `status`
- `summary`
- `recommended_option`
- `alternative_options`
- `suggestions`
- `relaxation_suggestions`
- `share_text`
- `share_card`
- `error`

## 已知限制

- 真实 E2E 依赖本地 LM Studio 和高德接口，运行时稳定性受环境影响
- 跨城自动找点时，高德请求较多，整体耗时仍偏长
- “建议晚点出发”目前主要体现在结果提示里，尚未完全进入排序内核
- 当前主要面向双人会合，暂不覆盖多人公平性建模

## 相关文档

- [`PRD.md`](/Users/gethin/workspace/jiebanerxing/PRD.md)
- [`SPEC.md`](/Users/gethin/workspace/jiebanerxing/SPEC.md)
- [`TASKS.md`](/Users/gethin/workspace/jiebanerxing/TASKS.md)
