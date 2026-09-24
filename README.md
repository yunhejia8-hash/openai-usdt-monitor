# openai-usdt-monitor

读取 OKX 已收盘的 15m / 1H / 4H K 线，生成 `output/latest.json` 与 HTML。
只输出策略候选，不访问账户或自动下单。

## 入场状态（schema_version 6）

`strategy.low_risk_entry.entry_state` 保留 `PROBE` 兼容值，显示字段
`entry_label` 为 `LIMIT/PROBE`。状态优先级：ADD → CONFIRMED → PROBE → WAIT。

- LIMIT/PROBE：支撑附近的小仓候选；成交时可能尚未完成支撑确认。
- CONFIRMED：已收盘 15m 阳线触及支撑区并收回支撑，HH/HL、SuperTrend 向上、
  MACD 正、成交量比 ≥1.10 均满足，同时满足原有评分及 R:R ≥1.5。
- ADD：还需显式已有仓位、多周期强化确认、更高评分、MA20 偏离 ≤1.5 ATR、R:R ≥2。
- WAIT：现价不允许立即入场。`limit_order_allowed` 单独表示是否仍可在下方区域预挂，
  因此 WAIT 和存在预挂计划可以同时成立。原 SETUP 合并为 WAIT。

默认假设无持仓。实际已有仓位时设置环境变量 `HAS_POSITION=true`；GitHub Actions
使用同名 Repository Variable。这个输入必须随真实持仓维护，不能把上次信号当成交凭据。

新增字段都位于 `strategy.low_risk_entry`：

| 字段 | 含义 |
| --- | --- |
| support_anchor | 本次使用的第一支撑或已确认角色翻转的旧阻力 |
| support_distance_atr | (现价 − 支撑锚点) / 15m ATR |
| support_distance_score / location_score | max(0, 100 − 50 × 绝对距离 ATR)，最高 100 |
| limit_order_zone | [支撑 − 0.2 ATR, 支撑 + 0.6 ATR]，需同时检查 limit_order_allowed |
| confirmed_entry_zone | [支撑, 支撑 + 0.6 ATR]，仅表示价格区域，仍需检查 entry_state |
| invalidation_level | 支撑下方超过 0.2 ATR 的最近支撑；没有则使用锚点 − 0.2 ATR |
| risk_reward_target / risk_reward_ratio | 下一阻力；(阻力 − 现价)/(现价 − 结构失效价) |
| breakout_level / breakout_ts | 从上一快照旧阻力登记的突破及首次突破 K 线时间 |

两个区域及失效价也在 `strategy` 顶层输出。保留原 BuyScore/EntryScore、rules、
thresholds、triggers、支撑阻力与三周期指标。`near_support` 布尔值仅作兼容诊断；
位置评分连续衰减，状态仍有明确的评分阈值。距离支撑超过 1.2 ATR 或 MA20 偏离超过
3 ATR 强制禁止即时买入，不允许强趋势抵消。ATR 缺失有效数值、缺少支撑时禁止入场；
缺少上方阻力、风险非正或 R:R 不足时禁止正式确认和加仓。

例如支撑 164.49、ATR 0.318：预挂候选区间为 **164.4264–164.6808**，包含
164.45–164.65；现价 164.95 距支撑约 1.447 ATR，强制 WAIT。区域不是成交指令，
R:R 为未扣手续费与滑点的理论比值，结构失效价不是保证成交价。

突破回踩读取前一快照的阻力；首次收盘突破只登记，必须等更晚的已收盘 K 线回踩收回，
再走严格确认。突破记录跌破 0.2 ATR 或超过 24 小时失效。不使用当前必在现价上方的阻力
判断“已突破”。旧 schema 快照没有 OHLC 时保守等待，不虚构回踩证据。

material change 检测状态变化、BuyScore 的 55/65/72 与 EntryScore 的 58/68/70/75
双向阈值跨越；区域端点变化 ≥0.1 ATR、区域出现/消失，以及预挂许可与防追涨门槛变化。
保留原有趋势、结构价位、穿越支撑阻力及方向评分变化检测。

## 验证

```sh
pip install -r requirements.txt
python -m unittest -v
python -m py_compile monitor.py test_monitor.py
python monitor.py
```

Windows 如无系统时区数据库，额外安装 `tzdata`。测试使用合成样本验证四种状态、
逐项确认、防追涨、R:R、持仓约束、旧阻力跨快照回踩和通知阈值；不等于盈利回测。
