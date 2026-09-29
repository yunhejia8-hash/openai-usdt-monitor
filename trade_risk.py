"""公开行情风险分析；不发送订单，不把信号当作真实持仓。"""
import math
from datetime import datetime, timezone

VERSION = "risk_v9"
BAR_MS = 900_000
HORIZONS = (4, 24)
MIN_SAMPLES = 30


def trade_math(price, stop, target, side="long", fee_bps=5, slippage_bps=5):
    """每边费用5bps、滑点5bps为研究假设；按入场和退出价格分别计成本。"""
    if side not in ("long", "short"):
        return {"valid": False, "net_rr": None}
    values = (price, stop, target, fee_bps, slippage_bps)
    if any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
        return {"valid": False, "net_rr": None}
    if min(price, stop, target) <= 0 or min(fee_bps, slippage_bps) < 0:
        return {"valid": False, "net_rr": None}
    sign = 1 if side == "long" else -1
    risk, reward = sign*(price-stop), sign*(target-price)
    if risk <= 0 or reward <= 0:
        return {"valid": False, "net_rr": None}
    cost = (fee_bps+slippage_bps)/10000
    loss, gain = risk+(price+stop)*cost, reward-(price+target)*cost
    return {"valid": True, "net_rr": round(gain/loss, 6),
            "stop_loss_pct": round(100*loss/price, 6),
            "target_gain_pct": round(100*gain/price, 6),
            "break_even_target_first_rate": round(loss/(loss+gain), 6) if gain > 0 else None,
            "gross_stop_distance_pct": round(100*risk/price, 6),
            "cost_to_gross_risk": round((price+stop)*cost/risk, 6),
            "fee_bps_per_side": fee_bps, "slippage_bps_per_side": slippage_bps,
            "funding_included": False, "account_return": False}


def _ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()*1000)


def freeze_probe(snap, side, src):
    """冻结产生信号当时的价位；旧记录缺失止损时绝不回填猜测。"""
    f = snap["frames"]["15m"]
    a = f["atr10"]
    anchor = src.get("support_anchor" if side == "long" else "resistance_anchor")
    stop = src.get("probe_stop" if side == "long" else "short_stop")
    target = src.get("risk_reward_target" if side == "long" else "take_profit_target")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (a, anchor, stop, target)) or a <= 0:
        return None
    zone = [anchor, anchor+.8*a] if side == "long" else [anchor-.8*a, anchor]
    return {"version": VERSION, "side": side, "stop": stop, "target": target,
            "entry_zone": zone, "atr": a, "ma20": f.get("ma20"),
            "regime_4h": snap["frames"]["4H"]["trend"],
            "entry_policy": "next_confirmed_15m_open_if_still_in_zone",
            "fee_bps_per_side": 5, "slippage_bps_per_side": 5}


def probe_outcomes(rows, bars, now_iso, existing):
    """最长24小时内去重；只在完整观察期限到期后纳入统计。"""
    now = _ms(now_iso)
    seen = {x.get("evaluation_id") for x in existing}
    by_ts = {b["ts"]: b for b in bars
             if b.get("confirm", True) and b["ts"]+BAR_MS <= now}
    selected, last_end = [], -1
    excluded_legacy = 0
    for row in sorted(rows, key=lambda r: r.get("generated_at_utc", "")):
        if row.get("state") not in ("PROBE", "SHORT_PROBE"):
            continue
        p = row.get("probe_plan")
        if not p or p.get("version") != VERSION:
            excluded_legacy += 1
            continue
        start = (_ms(row["generated_at_utc"])//BAR_MS+1)*BAR_MS
        if start < last_end:
            continue
        selected.append((row, p, start))
        last_end = start+24*3600000
    events, missing_paths = [], 0
    for row, p, start in selected:
        for hours in HORIZONS:
            event_id = f"{VERSION}:{row['generated_at_utc']}:{p['side']}:{hours}h"
            end = start+hours*3600000
            if event_id in seen or end > now:
                continue
            times = list(range(start, end, BAR_MS))
            if any(t not in by_ts for t in times):
                missing_paths += 1
                continue
            path = [by_ts[t] for t in times]
            price = path[0]["open"]
            sign = 1 if p["side"] == "long" else -1
            costs = trade_math(price, p["stop"], p["target"], p["side"],
                               p["fee_bps_per_side"], p["slippage_bps_per_side"])
            ext = (price-p["ma20"])/p["atr"] if p.get("ma20") is not None else None
            entered = (p["entry_zone"][0] <= price <= p["entry_zone"][1]
                       and costs["valid"] and costs["net_rr"] >= 1
                       and ext is not None and sign*ext <= 3)
            outcome, lower_return, upper_return = "not_entered", None, None
            if entered:
                outcome = "timeout"
                exits = [path[-1]["close"]]
                for bar in path:
                    stop_hit = bar["low"] <= p["stop"] if sign == 1 else bar["high"] >= p["stop"]
                    target_hit = bar["high"] >= p["target"] if sign == 1 else bar["low"] <= p["target"]
                    gap_stop = sign*(bar["open"]-p["stop"]) <= 0
                    gap_target = sign*(bar["open"]-p["target"]) >= 0
                    if gap_stop:
                        outcome, exits = "stop", [bar["open"]]
                    elif gap_target:
                        outcome, exits = "target", [p["target"]]
                    elif stop_hit and target_hit:
                        outcome, exits = "ambiguous", [p["stop"], p["target"]]
                    elif stop_hit:
                        outcome, exits = "stop", [p["stop"]]
                    elif target_hit:
                        outcome, exits = "target", [p["target"]]
                    else:
                        continue
                    break
                c = (p["fee_bps_per_side"]+p["slippage_bps_per_side"])/10000
                returns = [100*(sign*(x-price)-(price+x)*c)/price for x in exits]
                lower_return, upper_return = min(returns), max(returns)
            events.append({"event_type": "PROBE_STOP_OUTCOME", "evaluation_id": event_id,
                           "version": VERSION, "signal_generated_at_utc": row["generated_at_utc"],
                           "evaluated_at_utc": now_iso, "entry_ts": start, "horizon_end_ts": end,
                           "horizon_hours": hours, "side": p["side"], "regime_4h": p["regime_4h"],
                           "entry_price": price, "stop": p["stop"], "target": p["target"],
                           "outcome": outcome, "return_pct_lower": lower_return,
                           "return_pct_upper": upper_return})
    return events, {"selected_nonoverlapping_plans": len(selected),
                    "excluded_legacy_without_frozen_plan": excluded_legacy,
                    "missing_complete_paths": missing_paths}


def _wilson(k, n):
    if not n:
        return [None, None]
    z = 1.959963984540054
    p = k/n
    denominator = 1+z*z/n
    center = (p+z*z/(2*n))/denominator
    half = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/denominator
    return [max(0, center-half), min(1, center+half)]


def stop_statistics(events, now_iso, side, regime):
    """这是同版本、同方向、同4H环境的历史条件频率，不冒充已校准预测。"""
    now = _ms(now_iso)
    reports = {}
    unique = {e["evaluation_id"]: e for e in events if e.get("event_type") == "PROBE_STOP_OUTCOME"}
    for hours in HORIZONS:
        cohort = [e for e in unique.values() if e.get("version") == VERSION
                  and e.get("side") == side and e.get("regime_4h") == regime
                  and e.get("horizon_hours") == hours and e.get("horizon_end_ts", now+1) <= now
                  and _ms(e["evaluated_at_utc"]) <= now and e.get("outcome") != "not_entered"]
        n = len(cohort)
        k = sum(e["outcome"] == "stop" for e in cohort)
        ambiguous = sum(e["outcome"] == "ambiguous" for e in cohort)
        interval = [_wilson(k, n)[0], _wilson(k+ambiguous, n)[1]]
        status = "insufficient_samples" if n < MIN_SAMPLES else "ambiguous_paths" if ambiguous else "wide_interval" if interval[1]-interval[0] > .30 else "historical_estimate"
        reports[str(hours)] = {
            "status": status, "probability_estimate": k/n if status == "historical_estimate" else None,
            "samples": n, "minimum_samples": MIN_SAMPLES, "stop_first": k,
            "target_first": sum(e["outcome"] == "target" for e in cohort),
            "timeouts": sum(e["outcome"] == "timeout" for e in cohort), "ambiguous": ambiguous,
            "observed_rate_bounds": [k/n, (k+ambiguous)/n] if n else [None, None],
            "wilson_95_interval": interval,
            "mean_initial_leg_net_return_pct_bounds": [sum(e[key] for e in cohort)/n for key in ("return_pct_lower", "return_pct_upper")] if n else [None, None],
            "definition": "首次止盈前先触及初始止损；假设信号后下一根15m开盘仍在入场区，最长观察指定小时数",
            "limitations": "历史模拟频率，非真实成交或校准胜率；不含资金费/精确强平；24h去重不能消除市场相关性"}
    return reports


def exit_plan(position, price, frames, levels):
    policy = {"tp1_reduce_current_fraction": .25, "tp2_reduce_current_fraction": .70,
              "tp2_keep_current_fraction": .30, "reset_stages_after_add": False,
              "stop_may_widen": False, "add_after_tp2": False}
    if not position:
        return {"status": "position_context_required", "policy": policy,
                "note": "需要实际仓位与已成交阶段；信号不能代替成交记录"}
    if position.get("flat") is True:
        return {"status": "flat", "policy": policy, "action": "NO_POSITION"}
    required = ("side", "quantity", "initial_stop", "stop", "tp1", "tp2", "tp1_done", "tp2_done")
    if any(k not in position for k in required) or position["side"] not in ("long", "short"):
        return {"status": "invalid_position_context", "policy": policy}
    if any(not isinstance(position[k], (float, int)) or not math.isfinite(position[k]) or position[k] <= 0 for k in ("quantity", "initial_stop", "stop", "tp1", "tp2")):
        return {"status": "invalid_position_context", "policy": policy}
    if any(type(position[k]) is not bool for k in ("tp1_done", "tp2_done")) or (position["tp2_done"] and not position["tp1_done"]):
        return {"status": "invalid_position_context", "policy": policy}
    sign = 1 if position["side"] == "long" else -1
    if sign*(position["tp2"]-position["tp1"]) <= 0:
        return {"status": "invalid_position_context", "policy": policy}
    choose = max if sign == 1 else min
    stop = choose(position["initial_stop"], position["stop"])
    out = {"status": "advisory_only", "policy": policy, "action": "HOLD",
           "reduce_current_fraction": 0, "proposed_stop": stop,
           "proposed_tp1": position["tp1"], "proposed_tp2": position["tp2"],
           "fills_required_to_advance_stage": True, "reason_codes": []}
    adverse = frames["1H"]["trend"] == ("bearish" if sign == 1 else "bullish") and frames["15m"]["supertrend_direction"] == ("down" if sign == 1 else "up")
    if sign*(price-stop) <= 0 or adverse:
        out.update(action="EXIT_ALL", reduce_current_fraction=1,
                   reason_codes=["PROTECTIVE_STOP_REACHED" if sign*(price-stop) <= 0 else "TREND_EXIT"])
        return out
    for stage, fraction in ((1, .25), (2, .70)):
        if not position[f"tp{stage}_done"] and (stage == 1 or position["tp1_done"]) and sign*(price-position[f"tp{stage}"]) >= 0:
            out.update(action=f"TAKE_PROFIT_{stage}", reduce_current_fraction=fraction,
                       reason_codes=["OLD_TARGET_REACHED_BEFORE_TARGET_UPDATE"])
            return out
    favorable = (frames["4H"]["supertrend_direction"] == ("up" if sign == 1 else "down")
                 and frames["1H"]["trend"] != ("bearish" if sign == 1 else "bullish")
                 and frames["15m"]["trend"] == ("bullish" if sign == 1 else "bearish")
                 and frames["15m"]["supertrend_direction"] == ("up" if sign == 1 else "down"))
    candidates = sorted([x["level"] for x in levels["resistances" if sign == 1 else "supports"]
                         if sign*(x["level"]-price) > 0], reverse=sign == -1)
    if favorable and len(candidates) >= 2:
        if not position["tp1_done"]:
            out["proposed_tp1"] = choose(position["tp1"], candidates[0])
        if not position["tp2_done"]:
            out["proposed_tp2"] = choose(position["tp2"], candidates[1])
        if sign*(out["proposed_tp2"]-out["proposed_tp1"]) <= 0:
            out["proposed_tp1"], out["proposed_tp2"] = position["tp1"], position["tp2"]
    if position["tp2_done"]:
        trail = frames["1H"].get("supertrend_10_3")
        if trail is not None and frames["1H"]["supertrend_direction"] == ("up" if sign == 1 else "down") and sign*(price-trail) > 0:
            out["proposed_stop"] = choose(stop, trail)
            out["reason_codes"].append("TAIL_1H_SUPERTREND_RATCHET")
    out["allow_add_on_new_grade_only"] = not position["tp2_done"]
    out["note"] = "目标调整依据已确认K线；只提出计划，实际成交后才更新阶段。加仓还须通过总风险预算。"
    return out


def assess_snapshot(snap, previous=None, position=None):
    """将可执行候选、重复信号、行情过期及人工持仓上下文分开。"""
    previous = previous or {}
    price = float(snap["ticker"]["last"])
    now = _ms(snap["generated_at_utc"])
    strategy = snap["strategy"]
    warnings = []
    ticker_ts = snap["ticker"].get("ts")
    stale = not isinstance(ticker_ts, (int, float)) or not -5000 <= now-ticker_ts <= 120000
    for tf, duration in (("15m", BAR_MS), ("1H", 3600000), ("4H", 14400000)):
        age = now-(snap["frames"][tf]["asof_ts"]+duration)
        if age < -5000 or age >= duration:
            stale = True
    if stale:
        warnings.append("STALE_OR_UNVERIFIED_MARKET_DATA")
    if strategy.get("signal_conflict"):
        warnings.append("DUAL_SIDE_CONFLICT")
    invalid_context = snap.get("execution_plan", {}).get("status") == "invalid_position_context"
    if invalid_context:
        warnings.append("INVALID_POSITION_CONTEXT")
    stop_ts = (position or {}).get("last_stop_ts")
    cooldown = isinstance(stop_ts, (int, float)) and now-stop_ts < 2*BAR_MS
    if cooldown:
        warnings.append("TWO_BAR_COOLDOWN_AFTER_REPORTED_STOP")
    rhythm = {}
    for side, key in (("long", "low_risk_entry"), ("short", "low_risk_short")):
        e = strategy[key]
        codes = []
        ranks = {"PROBE": 1, "CONFIRMED": 2, "ADD": 3,
                 "SHORT_PROBE": 1, "SHORT_CONFIRMED": 2, "SHORT_ADD": 3}
        filled = (position or {}).get("last_filled_entry_state")
        same_side = (position or {}).get("side") == side
        exit_due = same_side and snap.get("execution_plan", {}).get("action") in ("EXIT_ALL", "TAKE_PROFIT_1", "TAKE_PROFIT_2")
        tail_only = same_side and position.get("tp2_done", False)
        grade_block = same_side and (filled not in ranks or ranks.get(e["entry_state"], 0) <= ranks[filled])
        active = e["entry_state"] in ("PROBE", "CONFIRMED", "ADD", "SHORT_PROBE", "SHORT_CONFIRMED", "SHORT_ADD")
        if exit_due: codes.append("EXIT_TAKES_PRIORITY_OVER_ENTRY")
        if tail_only: codes.append("TAIL_POSITION_NO_ADD")
        if grade_block and active: codes.append("NO_NEW_POSITION_GRADE")
        if stale or invalid_context or cooldown or exit_due or tail_only or (grade_block and active):
            if active:
                e["blocked_candidate_state"] = e["entry_state"]
            e["entry_state"] = "WAIT"
            active = False
            e["limit_order_allowed" if side == "long" else "short_order_allowed"] = False
            for trigger in (("probe", "confirmed_buy", "add", "buy_or_add", "setup") if side == "long" else ("short_probe", "short_confirmed", "short_add", "short_setup")):
                strategy["triggers"][trigger]["enabled"] = False
                if "limit_order_allowed" in strategy["triggers"][trigger]:
                    strategy["triggers"][trigger]["limit_order_allowed"] = False
                if "short_order_allowed" in strategy["triggers"][trigger]:
                    strategy["triggers"][trigger]["short_order_allowed"] = False
        if side == "long" and not e["rules"]["reclaimed_support"]:
            codes.append("SUPPORT_NOT_RECLAIMED")
        if side == "long" and not e["rules"].get("confirmed_candle_reclaim", False):
            codes.append("NO_CONFIRMED_CANDLE_RECLAIM")
        if e.get("first_breakout_wait"):
            codes.append("FIRST_BREAKOUT_WAIT_RETEST")
        if e["hard_no_chase"]:
            codes.append("HARD_NO_CHASE")
        if snap["frames"]["4H"]["trend"] == ("bearish" if side == "long" else "bullish"):
            codes.append("COUNTER_4H_TREND")
        if snap["frames"]["15m"]["volume_ratio_vs_20"] < .85:
            codes.append("WEAK_VOLUME")
        net = e.get("net_probe", {})
        if not net.get("valid") or net.get("net_rr", -1) < 1:
            codes.append("INSUFFICIENT_NET_REWARD")
        if net.get("cost_to_gross_risk", 0) >= .5:
            codes.append("COST_LARGE_RELATIVE_TO_STOP")
        anchor = e.get("support_anchor" if side == "long" else "resistance_anchor")
        token = f"{VERSION}:{side}:{snap['frames']['15m']['asof_ts']}:{anchor}:{e['entry_state']}"
        prior = previous.get("risk_assessment", {}).get("rhythm", {}).get(side, {})
        new = active and token != prior.get("signal_id")
        # 同一等级在持仓期间反复出现，不解释为重复补仓许可。
        may_add = (same_side and not position.get("tp2_done", False) and filled in ranks
                   and ranks.get(e["entry_state"], 0) > ranks[filled] and new)
        e["risk_reason_codes"] = codes
        rhythm[side] = {"signal_id": token, "new_actionable_signal": new,
                        "candidate_state": e["entry_state"],
                        "repeat_signal_is_not_new_order": True,
                        "position_grade_upgrade_candidate": may_add,
                        "add_requires_total_risk_budget_check": True}
        warnings += [side.upper()+"_"+c for c in codes]
    if stale or invalid_context or cooldown:
        strategy["state"] = "风险检查未通过，观望"
        strategy["reason_codes"] += warnings
    elif any(c.endswith(("EXIT_TAKES_PRIORITY_OVER_ENTRY", "TAIL_POSITION_NO_ADD", "NO_NEW_POSITION_GRADE")) for c in warnings):
        strategy["state"] = "持仓管理优先，暂停新增仓位"
    strategy["reason_codes"] = [c for c in strategy["reason_codes"] if not c.startswith(("ENTRY_", "SHORT_ENTRY_"))]
    strategy["reason_codes"] += ["ENTRY_"+strategy["low_risk_entry"]["entry_state"],
                                 "SHORT_ENTRY_"+strategy["low_risk_short"]["entry_state"]]
    return {"warnings": sorted(set(warnings)), "rhythm": rhythm,
            "validation_status": "research_only_not_proven_profitable",
            "price": price, "cost_assumption": "每边手续费5bps+滑点5bps；同时输出2/5/10bps敏感性，资金费未计入",
            "position_risk_note": "亏损百分比按名义持仓计算，不是账户权益亏损；账户风险需真实仓位及总权益",
            "account_risk_formula": "账户计划亏损%=名义持仓占权益%×stop_loss_pct/100；杠杆不再重复相乘；跳空及强平可能突破计划值",
            "stop_probability_note": "小样本不显示预测百分比；查看stop_risk的4h/24h样本量和区间",
            "protective_order_note": "此程序只分析；全仓止损必须由实际执行端维护"}
