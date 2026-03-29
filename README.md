# 结伴而行 Agent（LangGraph 版）

## 这版解决了什么
- 使用 Agent 框架（LangGraph）实现有状态决策流，而不是单次脚本调用。
- 具备：节点拆分、条件边、失败重试、自动放宽约束后重规划。

## 架构
1. `parse_intent` 节点
- 调 LM Studio（Qwen）把自然语言转换为结构化 intent。

2. `plan` 节点
- 调用确定性引擎（高德 + 规则）执行规划。

3. `assess` 节点
- 如果无可行方案，Agent 自动放宽约束并重试（受 `max_retries` 控制）。

## 安装依赖
```bash
pip install langgraph
```

如果你还没装其余依赖，确保以下脚本可运行：
- `engine.py`
- `agent_local.py`

## 运行示例
```bash
export AMAP_WEB_SERVICE_KEY='你的高德key'
python3 /Users/gethin/workspace/jiebanerxing/agent_langgraph.py \
  --user-request "我从上海虹桥火车站出发，朋友在上海世纪大道地铁站，我们一起去上海迪士尼乐园。自动找会合点，朋友公交不超过120分钟，我最多绕路90分钟，最多等待45分钟。" \
  --lmstudio-base-url "http://127.0.0.1:1234/v1" \
  --model "qwen/qwen3.5-9b" \
  --show-diagnostics \
  --print-intent \
  --max-retries 1
```

## 关键参数
- `--max-retries`：无解时自动重规划次数。
- `--show-diagnostics`：输出过滤原因。
- `--print-intent`：输出模型解析到的结构化意图。
- `--planner-timeout-sec`：单次规划调用超时（秒），超时会进入错误分支并按策略重试。
- `--planner-max-retries`：规划调用失败时额外重试次数（用于网络/QPS瞬时错误）。

## 安全约定
- 高德 Key 仅通过服务端环境变量 `AMAP_WEB_SERVICE_KEY` 提供，不通过前端参数传入。
- LangGraph 状态中不保存 AMap Key；规划节点使用闭包读取服务端环境变量。

## 你现在能学到的 Agent 能力
1. 有状态图式编排（StateGraph）
2. 节点职责分离（理解 / 求解 / 反思）
3. 条件边路由（成功、失败、重试）
4. Agent 决策与确定性引擎协同
