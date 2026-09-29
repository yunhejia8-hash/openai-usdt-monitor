"""固定历史数据上比较试仓首次退出；不把单腿结果称为完整策略收益。"""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import random

import monitor

DURATION = {"15m":900000,"1H":3600000,"4H":14400000}


def replay(data, engine):
    times={tf:[b["ts"]+DURATION[tf] for b in bars] for tf,bars in data.items()}
    cache={}; previous=None; busy_until=-1; results=[]; states=Counter()
    bars=data["15m"]
    for i,bar in enumerate(bars[:-96]):
        decision=bar["ts"]+900000
        if decision%1800000:
            continue
        frames={}
        for tf in DURATION:
            end=bisect_right(times[tf],decision)
            window=data[tf][max(0,end-240):end]
            if len(window)<60 or decision-times[tf][end-1]>=DURATION[tf]:
                break
            key=(tf,end)
            if key not in cache:
                cache[key]=engine.metrics(window,engine.TFS[tf][2])
            frames[tf]=cache[key]
        if len(frames)!=3:
            continue
        levels=engine.levels(bar["close"],frames)
        strategy=engine.strategy_state(bar["close"],frames,levels,previous)
        old_previous=previous
        previous={"frames":frames,"levels":levels,"strategy":strategy}
        states[strategy["low_risk_entry"]["entry_state"]]+=1
        if i<=busy_until:
            continue
        choices=[(side,key) for side,key,state in (("long","low_risk_entry","PROBE"),("short","low_risk_short","SHORT_PROBE"))
                 if strategy[key]["entry_state"]==state]
        if len(choices)!=1:
            continue
        side,key=choices[0];sign=1 if side=="long" else -1
        price=bars[i+1]["open"]
        # 下一根开盘重新计算候选；禁止只用信号收盘价假定成交。
        check=engine.strategy_state(price,frames,engine.levels(price,frames),old_previous)[key]
        if check["entry_state"] != ("PROBE" if sign==1 else "SHORT_PROBE"):
            continue
        stop=check["probe_stop" if sign==1 else "short_stop"]
        target=check["risk_reward_target" if sign==1 else "take_profit_target"]
        outcome="timeout";exit_price=bars[i+96]["close"];exit_index=i+96
        for j in range(i+1,i+97):
            b=bars[j]
            sh=b["low"]<=stop if sign==1 else b["high"]>=stop
            th=b["high"]>=target if sign==1 else b["low"]<=target
            if sign*(b["open"]-stop)<=0:
                outcome,exit_price="stop",b["open"]
            elif sign*(b["open"]-target)>=0:
                outcome,exit_price="target",target
            elif sh and th:
                outcome,exit_price="ambiguous",stop
            elif sh:
                outcome,exit_price="stop",stop
            elif th:
                outcome,exit_price="target",target
            else:
                continue
            exit_index=j
            break
        busy_until=exit_index
        results.append({"day":datetime.fromtimestamp(decision/1000,timezone.utc).strftime("%Y-%m-%d"),
                        "side":side,"outcome":outcome,"entry":price,"exit":exit_price,
                        "returns":{str(b):100*(sign*(exit_price-price)-(price+exit_price)*2*b/10000)/price for b in (2,5,10)}})
    return results,dict(states)


def summarize(trades):
    counts=Counter(t["outcome"] for t in trades);n=len(trades)
    stats={"samples":n,"outcomes":dict(counts),
           "stop_first_rate_bounds":[counts["stop"]/n,(counts["stop"]+counts["ambiguous"])/n] if n else None,
           "cost_scenarios":{}}
    for b in (2,5,10):
        groups=defaultdict(list)
        for t in trades:groups[t["day"]].append(t["returns"][str(b)])
        values=[t["returns"][str(b)] for t in trades]
        days=list(groups);rng=random.Random(20260929);boot=[]
        if len(days)>=2:
            for _ in range(1000):
                sampled=[v for d in rng.choices(days,k=len(days)) for v in groups[d]]
                boot.append(sum(sampled)/len(sampled))
            boot.sort()
        stats["cost_scenarios"][str(b)]={"fee_bps_per_side":b,"slippage_bps_per_side":b,
                "mean_net_initial_leg_return_pct":sum(values)/n if n else None,
                "mean_net_return_day_bootstrap_95": [boot[25],boot[974]] if boot else None,
                "net_positive_fraction":sum(v>0 for v in values)/n if n else None}
    return stats


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--data",required=True)
    parser.add_argument("--baseline",required=True,help="固定旧版本的monitor.py路径")
    parser.add_argument("--output",help="保存验证结果JSON；不保存账户信息")
    args=parser.parse_args();raw=Path(args.data).read_bytes();data=json.loads(raw)
    for tf,duration in DURATION.items():
        rows=data[tf]
        for i,b in enumerate(rows):
            if b.get("confirm") is not True or any(not math.isfinite(b[k]) for k in ("open","high","low","close","volQuote")):
                raise ValueError("未确认或非法行情")
            if not 0<b["low"]<=min(b["open"],b["close"])<=max(b["open"],b["close"])<=b["high"]:
                raise ValueError("非法OHLC")
            if i and b["ts"]-rows[i-1]["ts"]!=duration:
                raise ValueError("历史K线缺口")
    spec=importlib.util.spec_from_file_location("comparison_baseline",args.baseline)
    baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
    report={"data_sha256":hashlib.sha256(raw).hexdigest(),
            "first_15m_open_utc":datetime.fromtimestamp(data["15m"][0]["ts"]/1000,timezone.utc).isoformat(),
            "last_15m_close_utc":datetime.fromtimestamp((data["15m"][-1]["ts"]+900000)/1000,timezone.utc).isoformat(),
            "scope":"30分钟收盘采样、下一根开盘重验、只比较PROBE首次退出、24h最长持有；无账户仓位或杠杆乘数",
            "limitations":["同一历史区间已用于开发，不是样本外证明", "不模拟25%/70%和尾仓全周期", "同根双触及按止损计净收益；概率保留区间", "无资金费、精确强平或订单排队；日块bootstrap仍不能消除跨日相关性"]}
    for name,engine in (("baseline",baseline),("risk_v9",monitor)):
        trades,states=replay(data,engine)
        report[name]={"all":summarize(trades),"long":summarize([t for t in trades if t["side"]=="long"]),
                      "short":summarize([t for t in trades if t["side"]=="short"]),"long_state_counts":states}
    serialized=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        Path(args.output).write_text(serialized+"\n",encoding="utf-8")
    print(serialized)


if __name__=="__main__":main()
