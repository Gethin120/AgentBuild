# 结伴而行 Agent

## 架构原则
- 单入口：统一从 `run.sh` 进入，减少多脚本行为漂移。
- 分层清晰：编排、能力适配、核心策略、遗留代码隔离。
- 规则最小化：规则引擎只负责“约束校验 + 时间计算 + 评分”，策略决策由 Agent 节点负责。
- 可观测：节点级进度与运行 trace 持久化。
- 可恢复：重试控制、超时控制、错误分支明确。
- 安全优先：`AMAP_WEB_SERVICE_KEY` 仅服务端环境变量注入。

## 目录结构
```text
jiebanerxing/
├── app/
│   ├── agent.py                  # LangGraph 编排入口（主流程）
│   ├── intent_and_planner.py     # LLM意图解析 + 规划适配
│   ├── engine.py                 # 高德调用与确定性规划引擎
│   └── core/
│       ├── schemas.py            # 状态/策略/评审 schema
│       ├── policy.py             # 策略节点与约束调整
│       ├── response.py           # 输出裁剪与自然语言生成
│       └── memory.py             # trace 持久化
├── legacy/
│   └── langgraph_legacy.py       # 历史实现归档（不作为主入口）
├── run.sh                        # 统一运行脚本
└── README.md
```

## 工作流
1. `parse_intent`：自然语言请求 -> 结构化意图  
2. `strategy`：生成当前轮策略（约束与候选点策略）  
3. `plan`：调用规划引擎计算方案  
4. `judge`：评审方案质量与风险  
5. `retry`：未通过时在预算内重试  
6. `compose`：输出结构化结果 + 自然语言描述  
7. `persist`：写入 `.runs/trace.jsonl`

## 运行
```bash
export AMAP_WEB_SERVICE_KEY='你的高德key'
sh /Users/gethin/workspace/jiebanerxing/run.sh "我从上海虹桥火车站出发，朋友在上海世纪大道地铁站，我们一起去上海迪士尼乐园。自动找会合点。"
```

`run.sh` 默认执行：
```bash
python -m app.agent
```
并优先使用 `conda llm_local` 环境。

默认只输出最终自然语言结果，适合日常手工测试。

如果你想看完整调试信息、意图解析和 JSON 结果，可以这样运行：

```bash
DEBUG=1 sh /Users/gethin/workspace/jiebanerxing/run.sh "我从上海虹桥火车站出发，朋友在上海世纪大道地铁站，我们一起去上海迪士尼乐园。自动找会合点。"
```

## 关键参数
- `--retry-max-attempts`：评审不通过时最大重试次数
- `--planner-timeout-sec`：单次规划超时阈值
- `--planner-max-retries`：规划节点内部重试次数
- `--enable-llm-strategy` / `--disable-llm-strategy`：开启或关闭 LLM 策略生成（默认开启）
- `--enable-llm-judge` / `--disable-llm-judge`：开启或关闭 LLM 评审（默认开启，失败自动降级到规则评审）
- `--enable-thinking` / `--disable-thinking`：控制模型 thinking 模式（默认关闭）
- `--json-stdout`：把完整 JSON 结果打印到终端；默认仅打印自然语言结果
- `--strategy-step-wait-min`：每次重试放宽等待时间
- `--strategy-step-detour-min`：每次重试放宽司机绕路时间
- `--strategy-step-passenger-travel-min`：每次重试放宽乘客通勤时间
- `--judge-max-avg-wait-min`：评审节点允许的平均等待上限
- `--judge-max-avg-detour-min`：评审节点允许的平均绕路上限
- `--judge-min-options-required`：评审节点要求的最小可行方案数
- `--trace-path`：运行轨迹落盘路径

## P0 回放样本
项目内置了一组 P0 验收样本，可用于批量回放当前结构化 intent 场景：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 scripts/replay_eval.py
```

如需把每条样本的中间工件落盘，便于排查或做链式重规划分析：

```bash
python3 scripts/replay_eval.py --artifacts-dir .runs/replay_artifacts
```

默认样本文件：

```text
eval/p0_cases.json
```

说明：
- 回放脚本会逐条调用 `python -m app.agent`
- 每条样本通过 `--intent-json-path` 注入结构化 intent，避免依赖 LLM 解析稳定性
- 回放默认关闭 LLM 策略和 LLM 评审，尽量保证离线回归结果可重复
- 若样本包含 `previous_case_id`，回放脚本会自动把前一个样本的结果作为上一版方案传入，用于验证重规划差异
- 输出会汇总每条样本的 `status`、`failure_category`、`primary_bottleneck` 和通过率
- 汇总结果还会给出 `status_counts`、`failure_category_counts`、`primary_bottleneck_counts`、`recommendation_basis_counts`、`preference_profile_counts` 和 `replan_type_counts`，更适合做版本回归对比

## 单元测试
项目已经为若干关键纯函数补了基础单元测试，可快速验证偏好解析、结果表达和重规划补丁逻辑：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 -m unittest discover -s tests -p 'test_*.py'
```

## P0 一键检查
如果想快速确认当前版本是否还满足基础质量门槛，可以直接运行统一检查脚本：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 scripts/check_p0.py --skip-replay
```

说明：
- 默认会运行单元测试和 trace 汇总
- 脚本会优先加载项目根目录下的 `.env` / `.env.local`
- 默认优先使用 `conda llm_local` 环境；若需覆盖，可显式传 `--python-bin`
- 若本地已具备高德 key、模型服务和网络环境，也可以去掉 `--skip-replay`，把样本回放一起纳入检查
- 可配合 `--report-output-path .runs/check_report.json` 保存检查报告
- 可配合 `--replay-artifacts-dir .runs/replay_artifacts` 保存样本回放工件
- 检查报告现在还会先校验 `eval/p0_cases.json` 的结构合法性，并汇总动作日志文件概况

## 报告对比
如果你已经保存了两版检查报告或两版回放报告，也可以直接比较关键指标变化：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 scripts/compare_reports.py \
  --baseline-path .runs/check_report_prev.json \
  --current-path .runs/check_report.json
```

输出会给出：
- 核心数值指标的增减，例如成功率、错误率、平均可行方案数
- 状态分布、失败类型分布、重规划类型分布、动作分布的变化

## Trace 汇总
如果已经积累了多次运行的 trace，可以快速汇总关键指标：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 scripts/summarize_trace.py
```

默认会读取：

```text
.runs/trace.jsonl
```

输出包含：
- 成功率、无解率、错误率
- 平均重试次数、约束命中率、候选点利用率
- 失败类型分布、主要瓶颈分布、约束过滤原因分布
- 重规划占比与不同重规划类型分布
- 推荐策略与 tradeoff tag 分布
- 分享率、确认率、整体互动率

## 用户动作记录
如果后续接入前端或手动验证流程，可以把分享、确认、重规划等动作记入独立日志：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 scripts/log_user_action.py \
  --request-id <request_id> \
  --action share \
  --pickup-point "龙阳路地铁站"
```

支持的动作类型：
- `share`
- `confirm`
- `replan`
- `discuss`
- `dismiss`
- `open_navigation`

默认会写入：

```text
.runs/actions.jsonl
```

`summarize_trace.py` 会自动读取该文件，并汇总分享率、确认率和互动率。

## 动态重规划
如果已经有一份结构化 intent，也可以注入临时事件后重新规划，例如“朋友晚点”或“扩大等待容忍时间”：

```bash
cd /Users/gethin/workspace/jiebanerxing
python3 -m app.agent \
  --user-request "重规划" \
  --intent-json-path /path/to/intent.json \
  --replan-event-json-path /path/to/replan_event.json \
  --previous-response-json-path /path/to/previous_response.json \
  --output-json-path /path/to/output.json \
  --intent-output-json-path /path/to/intent_out.json
```

支持的重规划事件类型：
- `passenger_delay`
- `driver_delay`
- `expand_wait`
- `expand_detour`
- `expand_passenger_travel`
- `expand_search_radius`
- `expand_pickup_limit`

事件文件示例：

```json
{
  "type": "passenger_delay",
  "delay_min": 20,
  "reason": "朋友出地铁站晚了"
}
```

如果同时提供上一版结果，系统会在输出里附带新旧方案差异，例如：
- 会合点是否变化
- 等待时间增减
- 司机绕路增减
- 整体到达时间提前或推后多少分钟

自然语言输出也会同步说明这些变化，便于直接在终端或聊天式界面中查看。

如果你希望把当前结果或最终 intent 直接落盘，方便下一轮继续引用：
- `--output-json-path`：写出最终完整结果
- `--intent-output-json-path`：写出最终 intent
