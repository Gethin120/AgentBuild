# 结伴而行 Agent

## 架构原则
- 单入口：统一从 `run.sh` 进入，减少多脚本行为漂移。
- 分层清晰：编排、能力适配、核心策略、遗留代码隔离。
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

## 关键参数
- `--retry-max-attempts`：评审不通过时最大重试次数
- `--planner-timeout-sec`：单次规划超时阈值
- `--planner-max-retries`：规划节点内部重试次数
- `--enable-llm-strategy`：启用 LLM 参与策略节点
- `--trace-path`：运行轨迹落盘路径

