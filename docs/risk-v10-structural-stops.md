# risk_v10：结构止损与支撑角色同步

## 触发原因

2026-09-29 早盘多头 PROBE 暴露出两个相互关联的问题：

1. 旧支撑被当前 levels 重新分类为阻力后，long 模块仍可能短暂沿用旧 support_anchor。
2. PROBE 止损只按“当前支撑 - 0.75×15m ATR”计算，可能把保护位放在正常波动噪声内。

历史快照显示，08:33 的 PROBE 约在 158.63，旧算法止损约 158.32；随后价格曾下探到足以触发该类紧止损，但更低一级结构支撑约 157.15 并未失效，之后价格反弹到 160 以上。这是一个错误止损样本，但单一样本不证明放宽止损能提高总体收益。

## 修复

- 若旧 support_anchor 已进入当前 resistance 集群且已收盘价格没有重新收复，立即停止把它当作支撑锚点。
- PROBE 同时计算：
  - microstructure stop：当前锚点 ± 0.75×ATR；
  - structural stop：下一层结构失效位外再加 0.20×ATR 缓冲。
- 最终保护位选择更保守的结构止损；空头对称处理。
- 更宽止损不允许保持原仓位风险。以旧 0.75×ATR 风险为基准计算 size multiplier：
  - >=0.80：small
  - 0.50–0.80：reduced
  - <0.50：micro
- 如果结构止损导致扣成本后的净 R:R < 1，则不产生 PROBE；宁可 WAIT，也不靠贴身止损“制造”漂亮 R:R。
- 风险统计版本升级为 risk_v10，避免与旧 risk_v9 止损口径混算。

## 2026-09-29 回归样本

固定输入近似复现 08:33：

- price = 158.63
- primary support = 158.4862
- secondary structural support = 157.1477
- 15m ATR = 0.234208
- nearest resistance = 159.165

新逻辑：

- microstructure stop ≈ 158.3105
- structural stop < 157.1477
- stop_basis = STRUCTURE_BUFFER
- position size class = micro
- 因结构止损后的净 R:R < 1，entry_state = WAIT

因此修复目标不是“永不止损”，而是把“是否值得入场”和“止损应放在哪里”统一到同一套结构风险口径。

## 验证

分支 CI 使用 Python 3.12：

- 44 项 unittest 全部通过；
- monitor.py / trade_risk.py / replay_probe_risk.py / workflow_health.py / watchdog.py 及测试文件 py_compile 通过；
- 新增专门的 2026-09-29 结构止损回归测试。

这仍是研究策略，不代表已证明收益改善。后续需要用 risk_v10 独立积累新样本，再比较 stop-first、Expectancy、Profit Factor 和最大回撤。
