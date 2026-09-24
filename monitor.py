import json, math, os, statistics
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

INST_ID="OPENAI-USDT-SWAP"
BASE="https://www.okx.com"
OUT=Path("output"); OUT.mkdir(exist_ok=True)
TFS={"15m":("15m",240,96),"1H":("1H",240,72),"4H":("4H",240,42)}

def get(path, params):
    r=requests.get(BASE+path,params=params,timeout=20,headers={"User-Agent":"openai-usdt-monitor/1.0"})
    r.raise_for_status(); p=r.json()
    if p.get("code")!="0": raise RuntimeError(p)
    return p["data"]

def ticker():
    d=get("/api/v5/market/ticker",{"instId":INST_ID})[0]
    out={k:d.get(k) for k in ["last","lastSz","askPx","askSz","bidPx","bidSz","open24h","high24h","low24h","vol24h","volCcy24h","ts"]}
    for k in out:
        if k!="ts" and out[k] not in (None,""): out[k]=float(out[k])
    out["ts"]=int(out["ts"]); return out

def candles(bar,limit):
    rows=get("/api/v5/market/history-candles",{"instId":INST_ID,"bar":bar,"limit":str(limit)})
    out=[{"ts":int(x[0]),"open":float(x[1]),"high":float(x[2]),"low":float(x[3]),"close":float(x[4]),"vol":float(x[5]),"volCcy":float(x[6]),"volQuote":float(x[7]),"confirm":x[8]=="1"} for x in rows]
    out=sorted([x for x in out if x["confirm"]],key=lambda x:x["ts"])
    if len(out)<60: raise RuntimeError(f"not enough {bar} candles")
    return out

def sma(v,n): return sum(v[-n:])/n

def ema(v,n):
    a=2/(n+1); out=[v[0]]
    for x in v[1:]: out.append(a*x+(1-a)*out[-1])
    return out

def rsi(v,n):
    ds=[v[i]-v[i-1] for i in range(1,len(v))]
    gs=[max(x,0) for x in ds]; ls=[max(-x,0) for x in ds]
    ag=sum(gs[:n])/n; al=sum(ls[:n])/n
    for g,l in zip(gs[n:],ls[n:]):
        ag=(ag*(n-1)+g)/n; al=(al*(n-1)+l)/n
    if al==0: return 100.0
    rs=ag/al; return 100-100/(1+rs)

def atr_series(c,n):
    tr=[]
    for i,x in enumerate(c):
        if i==0: t=x["high"]-x["low"]
        else:
            pc=c[i-1]["close"]
            t=max(x["high"]-x["low"],abs(x["high"]-pc),abs(x["low"]-pc))
        tr.append(t)
    out=[None]*len(c); prev=sum(tr[:n])/n; out[n-1]=prev
    for i in range(n,len(c)):
        prev=(prev*(n-1)+tr[i])/n; out[i]=prev
    return out

def supertrend(c,n=10,m=3.0):
    atr=atr_series(c,n); up=[None]*len(c); lo=[None]*len(c); st=[None]*len(c); dr=[None]*len(c)
    for i,x in enumerate(c):
        if atr[i] is None: continue
        hl2=(x["high"]+x["low"])/2; bu=hl2+m*atr[i]; bl=hl2-m*atr[i]
        if i==0 or up[i-1] is None:
            up[i]=bu; lo[i]=bl
            if x["close"]>=hl2: st[i],dr[i]=bl,"up"
            else: st[i],dr[i]=bu,"down"
            continue
        pc=c[i-1]["close"]; pu=up[i-1]; pl=lo[i-1]
        up[i]=bu if (bu<pu or pc>pu) else pu
        lo[i]=bl if (bl>pl or pc<pl) else pl
        if dr[i-1]=="down":
            st[i],dr[i]=(lo[i],"up") if x["close"]>up[i] else (up[i],"down")
        else:
            st[i],dr[i]=(up[i],"down") if x["close"]<lo[i] else (lo[i],"up")
    for i in range(len(c)-1,-1,-1):
        if st[i] is not None: return float(st[i]),dr[i]
    raise RuntimeError("supertrend failed")

def pivots(c,w=2):
    hs=[]; ls=[]
    for i in range(w,len(c)-w):
        h=c[i]["high"]; l=c[i]["low"]
        if all(h>c[j]["high"] for j in range(i-w,i)) and all(h>=c[j]["high"] for j in range(i+1,i+w+1)): hs.append((i,h))
        if all(l<c[j]["low"] for j in range(i-w,i)) and all(l<=c[j]["low"] for j in range(i+1,i+w+1)): ls.append((i,l))
    return hs,ls

def structure(c):
    hs,ls=pivots(c); label="insufficient"
    if len(hs)>=2 and len(ls)>=2:
        ph,lh=hs[-2][1],hs[-1][1]; pl,ll=ls[-2][1],ls[-1][1]
        if lh>ph and ll>pl: label="HH_HL"
        elif lh<ph and ll<pl: label="LH_LL"
        elif lh>ph and ll<pl: label="expanding"
        else: label="contracting"
    return {"label":label,"last_highs":[round(x[1],4) for x in hs[-3:]],"last_lows":[round(x[1],4) for x in ls[-3:]]}

def metrics(c,recent):
    closes=[x["close"] for x in c]; vols=[x["volQuote"] for x in c]; last=c[-1]
    ma5,ma10,ma20=sma(closes,5),sma(closes,10),sma(closes,20)
    sd=statistics.pstdev(closes[-20:]); bu=ma20+2*sd; bl=ma20-2*sd
    e12,e26=ema(closes,12),ema(closes,26); diff=[a-b for a,b in zip(e12,e26)]; dea=ema(diff,9)
    av=atr_series(c,10); atr10=next(x for x in reversed(av) if x is not None); st,sdirection=supertrend(c)
    look=c[-recent:]; vh=sma(vols,20); vr=last["volQuote"]/vh if vh else math.nan
    bullish=last["close"]>ma20 and ma5>ma10>ma20 and sdirection=="up"
    bearish=last["close"]<ma20 and ma5<ma10<ma20 and sdirection=="down"
    return {
        "asof_ts":last["ts"],"close":round(last["close"],6),
        "open":last["open"],"low":last["low"],"high":last["high"],
        "ma5":round(ma5,6),"ma10":round(ma10,6),"ma20":round(ma20,6),
        "boll_mid":round(ma20,6),"boll_upper":round(bu,6),"boll_lower":round(bl,6),
        "rsi6":round(rsi(closes,6),3),"rsi12":round(rsi(closes,12),3),"rsi24":round(rsi(closes,24),3),
        "macd_diff":round(diff[-1],6),"macd_dea":round(dea[-1],6),"macd_hist_okx":round(2*(diff[-1]-dea[-1]),6),
        "atr10":round(float(atr10),6),"ma20_extension_atr":round((last["close"]-ma20)/atr10,3),"supertrend_10_3":round(st,6),"supertrend_direction":sdirection,
        "volume_quote":round(last["volQuote"],3),"volume_20_avg":round(vh,3),"volume_ratio_vs_20":round(vr,3),
        "recent_high":round(max(x["high"] for x in look),6),"recent_low":round(min(x["low"] for x in look),6),
        "structure":structure(c),"trend":"bullish" if bullish else "bearish" if bearish else "neutral"
    }

def levels(price,frames):
    cand=[]
    for tf,m in frames.items():
        for k,label in [("ma10","MA10"),("ma20","MA20"),("boll_upper","BOLL upper"),("boll_lower","BOLL lower"),("supertrend_10_3","SuperTrend"),("recent_high","recent high"),("recent_low","recent low")]:
            v=m[k]
            if isinstance(v,(int,float)) and math.isfinite(v): cand.append((float(v),f"{tf} {label}"))
    cand.sort(); tol=max(price*0.0035,0.25); clusters=[]
    for item in cand:
        if not clusters: clusters.append([item]); continue
        center=sum(x[0] for x in clusters[-1])/len(clusters[-1])
        clusters[-1].append(item) if abs(item[0]-center)<=tol else clusters.append([item])
    packed=[{"level":round(sum(x[0] for x in c)/len(c),4),"sources":[x[1] for x in c],"strength":len(c)} for c in clusters]
    sup=sorted([x for x in packed if x["level"]<price],key=lambda x:x["level"],reverse=True)[:4]
    res=sorted([x for x in packed if x["level"]>price],key=lambda x:x["level"])[:4]
    return {"supports":sup,"resistances":res}

def summary(frames):
    trends={tf:m["trend"] for tf,m in frames.items()}
    b=sum(v=="bullish" for v in trends.values()); s=sum(v=="bearish" for v in trends.values())
    if b>=2 and trends["4H"]!="bearish": regime="bullish_alignment"
    elif s>=2 and trends["4H"]!="bullish": regime="bearish_alignment"
    else: regime="mixed"
    return {"regime":regime,"timeframe_trends":trends}

def clamp(v, lo=0.0, hi=100.0):
    return max(lo,min(hi,float(v)))

def low_risk_entry(price,frames,lvls,previous=None,has_position=False):
    f15,f1,f4=frames["15m"],frames["1H"],frames["4H"]
    supports=lvls["supports"]; resistances=lvls["resistances"]
    s=supports[0] if supports else None; r=resistances[0] if resistances else None
    s_level=s["level"] if s else None; r_level=r["level"] if r else None
    raw_atr=float(f15["atr10"])
    valid_atr=math.isfinite(raw_atr) and raw_atr>0
    atr=raw_atr if valid_atr else 1e-9
    previous=previous or {}
    prev_entry=previous.get("strategy",{}).get("low_risk_entry",{})
    prev15=previous.get("frames",{}).get("15m",{})
    # 跌到旧支撑略下方时保留锚点，避免 levels 将它移入阻力后丢失回踩区域。
    old_support=prev_entry.get("support_anchor") or previous.get("strategy",{}).get("support_primary")
    if old_support is not None and abs(price-old_support)<=0.8*atr:
        old_s=next((x for x in previous.get("levels",{}).get("supports",[]) if x["level"]==old_support),None)
        if old_s is None and prev_entry.get("support_anchor_sources"):
            old_s={"level":old_support,"sources":prev_entry["support_anchor_sources"]}
        if old_s:
            s=old_s; s_level=old_support

    # 突破记录来自旧阻力；本根收盘突破仅登记，后续已收盘 K 线才可确认回踩。
    pending=prev_entry.get("breakout_level")
    breakout_ts=prev_entry.get("breakout_ts")
    if pending is not None and (price<pending-0.2*atr or f15["asof_ts"]-(breakout_ts or 0)>24*60*60*1000):
        pending=None; breakout_ts=None
    if pending is None and f15["asof_ts"]>prev15.get("asof_ts",f15["asof_ts"]):
        crossed=[x["level"] for x in previous.get("levels",{}).get("resistances",[])
                 if prev15.get("close",price)<=x["level"]<f15["close"]]
        if crossed:
            pending=max(crossed); breakout_ts=f15["asof_ts"]
    retest_touch=(pending is not None and f15["asof_ts"]>(breakout_ts or f15["asof_ts"])
                  and pending-0.2*atr<=f15.get("low",math.inf)<=pending+0.6*atr
                  and f15["close"]>=pending and price>=pending)
    if retest_touch and abs(price-pending)<=0.8*atr:
        s_level=pending
        s={"level":pending,"sources":["15m former resistance","15m retest"],"strength":2}
    distance=(price-s_level)/atr if valid_atr and s_level is not None else None
    location=clamp(100-50*abs(distance)) if distance is not None else 0.0
    next_res=[x for x in resistances if x["level"]>max(price,s_level or price)]
    r=next_res[0] if next_res else None
    r_level=r["level"] if r else None
    lower_supports=[x["level"] for x in supports if s_level is not None and x["level"]<s_level-0.2*atr]
    invalidation=max(lower_supports) if lower_supports else (s_level-0.2*atr if s_level is not None else None)
    limit_zone=[round(s_level-0.2*atr,6),round(s_level+0.6*atr,6)] if s_level is not None and valid_atr else None
    confirmed_zone=[round(s_level,6),round(s_level+0.6*atr,6)] if limit_zone else None
    risk=price-invalidation if invalidation is not None else None
    reward=r_level-price if r_level is not None else None
    rr=reward/risk if risk is not None and risk>0 and reward is not None and reward>0 else None
    rr_ok=rr is not None and rr>=1.5

    # Direction score: every component is normalized so higher = more bullish.
    trend=50
    trend += 15 if f4["supertrend_direction"]=="up" else -15
    trend += 15 if f1["trend"]=="bullish" else (-15 if f1["trend"]=="bearish" else 0)
    trend += 10 if f15["trend"]=="bullish" else (-10 if f15["trend"]=="bearish" else 0)

    momentum=50
    momentum += 15 if f1["macd_hist_okx"]>0 else -15
    momentum += 10 if f15["macd_hist_okx"]>0 else -10
    momentum += 10 if 50<=f15["rsi6"]<=70 else (-10 if f15["rsi6"]<40 else -5 if f15["rsi6"]>75 else 0)

    struct=50
    struct += 20 if f15["structure"]["label"]=="HH_HL" else (-20 if f15["structure"]["label"]=="LH_LL" else 0)
    struct += 15 if f1["structure"]["label"]=="HH_HL" else (-15 if f1["structure"]["label"]=="LH_LL" else 0)

    volume_ratio=float(f15["volume_ratio_vs_20"])
    volume=clamp(50+(volume_ratio-1)*35)

    # Support / resistance quality.
    support_quality=0 if not s else clamp(
        30+8*len(s["sources"])
        +(15 if any(x.startswith("4H ") for x in s["sources"]) else 0)
        +(10 if any("recent low" in x for x in s["sources"]) else 0)
    )
    resistance_validity=0 if not r else clamp(
        30+8*len(r["sources"])
        +(15 if any(x.startswith("4H ") for x in r["sources"]) else 0)
        +(10 if any("recent high" in x for x in r["sources"]) else 0)
    )
    resistance_bull=100-resistance_validity
    buy_score=clamp(
        .25*support_quality+.20*resistance_bull+.20*clamp(trend)
        +.15*clamp(momentum)+.10*volume+.10*clamp(struct)
    )

    # Shared confirmations.
    near_support=s_level is not None and abs(price-s_level)<=0.60*atr
    near_support_probe=s_level is not None and abs(price-s_level)<=0.80*atr
    decisive_break=s_level is not None and price < s_level-0.20*atr
    reclaimed=s_level is not None and price>=s_level
    hl=f15["structure"]["label"]=="HH_HL"
    confirm_st=f15["supertrend_direction"]=="up"
    macd15_pos=f15["macd_hist_okx"]>0
    confirm_vol=volume_ratio>=1.10
    probe_vol=volume_ratio>=0.85
    rsi_probe_ok=45<=float(f15["rsi6"])<=75
    probe_signal_count=sum(bool(x) for x in (confirm_st,macd15_pos,probe_vol,rsi_probe_ok))

    # Anti-chase penalty: extended price cannot be rescued by bullish indicators.
    ext=float(f15.get("ma20_extension_atr",0))
    chase_penalty=25 if ext>2 else 0
    hard_no_chase=not valid_atr or distance is None or distance>1.2 or ext>3
    entry_score=clamp(
        .45*location+.20*support_quality+.15*clamp(trend)
        +.10*clamp(momentum)+.10*volume-chase_penalty
    )

    # 预挂许可独立于当前入场状态；轻微跌到支撑下方允许小仓候选。
    limit_allowed=(
        limit_zone is not None and not decisive_break and ext<=3
        and support_quality>=38 and buy_score>=55 and probe_signal_count>=2
    )
    probe_candidate=(
        limit_allowed and not hard_no_chase
        and location>=60 and support_quality>=38
        and buy_score>=55 and entry_score>=58
        and probe_signal_count>=2
    )

    # 以已收盘 K 线的触及、收回及阳线作为回踩证据，不能仅凭现价靠近支撑。
    pullback_touch=(s_level is not None and s_level-0.2*atr<=f15.get("low",math.inf)<=s_level+0.6*atr
                    and f15["close"]>=s_level and f15["close"]>f15.get("open",math.inf))
    pullback_confirmed=(
        location>=70 and not decisive_break and reclaimed and hl and pullback_touch
        and confirm_st and confirm_vol and macd15_pos and rr_ok
        and buy_score>=65 and entry_score>=70 and not hard_no_chase
    )

    # 旧阻力角色翻转后同样需要全部确认及收益风险比过滤。
    retest_candidate=(retest_touch and s_level==pending and pullback_touch
                      and location>=70 and confirm_st and hl and confirm_vol and macd15_pos
                      and buy_score>=65 and entry_score>=68 and ext<=2 and rr_ok and not hard_no_chase)
    # 已登记突破时必须等下一根，不能从普通回踩分支绕过首次突破限制。
    if pending is not None and (breakout_ts==f15["asof_ts"] or (s_level==pending and not retest_candidate)):
        pullback_confirmed=False

    confirmed_candidate=pullback_confirmed or retest_candidate

    # ADD 必须由调用者明确提供已有仓位，不能从上次信号推断成交。
    add_candidate=(
        confirmed_candidate and has_position is True
        and f4["supertrend_direction"]=="up"
        and f1["trend"]!="bearish"
        and f15["trend"]=="bullish"
        and hl and confirm_st and confirm_vol and macd15_pos
        and (f1["macd_hist_okx"]>0 or f1["trend"]=="bullish")
        and buy_score>=72 and entry_score>=75
        and ext<=1.5 and location>=70 and rr is not None and rr>=2.0
    )

    if add_candidate:
        entry_state="ADD"
        entry_mode="BREAKOUT_RETEST" if retest_candidate else "PULLBACK"
    elif confirmed_candidate:
        entry_state="CONFIRMED"
        entry_mode="BREAKOUT_RETEST" if retest_candidate else "PULLBACK"
    elif probe_candidate:
        entry_state="PROBE"
        entry_mode="PULLBACK"
    else:
        entry_state="WAIT"
        entry_mode=None

    return {
        "buy_score":round(buy_score,1),
        "entry_score":round(entry_score,1),
        "location_score":round(location,1),
        "support_quality":round(support_quality,1),
        "resistance_validity":round(resistance_validity,1),
        "entry_state":entry_state,
        "entry_mode":entry_mode,
        "entry_label":"LIMIT/PROBE" if entry_state=="PROBE" else entry_state,
        "support_anchor":s_level,
        "support_anchor_sources":s["sources"] if s else [],
        "support_distance_atr":round(distance,6) if distance is not None else None,
        "support_distance_score":round(location,1),
        "limit_order_zone":limit_zone,
        "confirmed_entry_zone":confirmed_zone,
        "limit_order_allowed":limit_allowed,
        "limit_order_note":"仅预挂小仓；成交时可能尚未确认支撑，不能视为正式买入确认",
        "invalidation_level":round(invalidation,6) if invalidation is not None else None,
        "risk_reward_ratio":round(rr,4) if rr is not None else None,
        "risk_reward_target":r_level,
        "has_position":has_position is True,
        "breakout_level":pending,
        "breakout_ts":breakout_ts,
        "hard_no_chase":hard_no_chase,
        "ma20_extension_atr":round(ext,3),
        "chase_penalty":chase_penalty,
        "probe_signal_count":probe_signal_count,
        "rules":{
            "near_support":near_support,
            "valid_atr":valid_atr,
            "pullback_touch":pullback_touch,
            "risk_reward_ok":rr_ok,
            "near_support_probe":near_support_probe,
            "decisive_break":decisive_break,
            "reclaimed_support":reclaimed,
            "15m_HL":hl,
            "15m_supertrend_up":confirm_st,
            "15m_macd_positive":macd15_pos,
            "15m_rsi_probe_ok":rsi_probe_ok,
            "volume_ratio_ge_probe":probe_vol,
            "volume_ratio_ge_confirm":confirm_vol,
            "probe_candidate":probe_candidate,
            "pullback_confirmed":pullback_confirmed,
            "breakout_retest_candidate":retest_candidate,
            "add_candidate":add_candidate
        },
        "thresholds":{
            "probe_buy_score_min":55,
            "probe_entry_score_min":58,
            "probe_location_min":60,
            "probe_support_quality_min":38,
            "probe_signal_count_min":2,
            "probe_near_support_atr":0.80,
            "confirmed_buy_score_min":65,
            "confirmed_entry_score_pullback_min":70,
            "confirmed_entry_score_retest_min":68,
            "add_buy_score_min":72,
            "add_entry_score_min":75,
            "add_max_ma20_extension_atr":1.5,
            "near_support_atr":0.60,
            "break_buffer_atr":0.20,
            "volume_ratio_probe":0.85,
            "volume_ratio_confirm":1.10,
            "chase_penalty_above_ma20_atr":2.0,
            "hard_no_chase_above_ma20_atr":3.0,
            "hard_no_chase_support_distance_atr":1.2,
            "confirmed_rr_min":1.5,
            "add_rr_min":2.0
        }
    }

def strategy_state(price, frames, lvls, previous=None, has_position=False):
    f15=frames["15m"]; f1=frames["1H"]; f4=frames["4H"]
    supports=lvls["supports"]; resistances=lvls["resistances"]
    s1=supports[0]["level"] if supports else None
    s2=supports[1]["level"] if len(supports)>1 else None
    r1=resistances[0]["level"] if resistances else None
    r2=resistances[1]["level"] if len(resistances)>1 else None
    entry=low_risk_entry(price,frames,lvls,previous,has_position)

    score=0
    score += 2 if f4["supertrend_direction"]=="up" else -2
    score += 2 if f1["trend"]=="bullish" else (-2 if f1["trend"]=="bearish" else 0)
    score += 1 if f15["trend"]=="bullish" else (-1 if f15["trend"]=="bearish" else 0)
    score += 1 if f15["structure"]["label"]=="HH_HL" else (-1 if f15["structure"]["label"]=="LH_LL" else 0)
    score += 1 if f1["macd_hist_okx"]>0 else -1
    bias="bullish" if score>=4 else ("bearish" if score<=-4 else "mixed")

    entry_state=entry["entry_state"]
    if entry_state=="ADD":
        state="加仓候选"
    elif entry_state=="CONFIRMED":
        state="正式买入候选"
    elif entry_state=="PROBE":
        state="试仓候选"
    elif entry_state=="SETUP":
        state="等待低风险确认"
    elif bias=="bearish" and s1 is not None and price<s1:
        state="减仓"
    else:
        state="观望"

    # Risk layer remains conservative. ADD is never treated as permission to chase.
    invalidation=entry["invalidation_level"]

    return {
        "state":state,
        "bias":bias,
        "score":score,
        "support_primary":s1,
        "support_secondary":s2,
        "resistance_primary":r1,
        "resistance_secondary":r2,
        "invalidation_level":invalidation,
        "limit_order_zone":entry["limit_order_zone"],
        "confirmed_entry_zone":entry["confirmed_entry_zone"],
        "low_risk_entry":entry,
        "triggers":{
            "probe":{
                "enabled":entry_state=="PROBE",
                "condition":entry["limit_order_note"],
                "limit_order_allowed":entry["limit_order_allowed"],
                "position_size_class":"small"
            },
            "confirmed_buy":{
                "enabled":entry_state in ("CONFIRMED","ADD"),
                "condition":"回踩强支撑完成结构确认，或突破后回踩原阻力完成确认；禁止第一次突破追涨",
                "trigger_above":r1,
                "confirmation_above":r2
            },
            "add":{
                "enabled":entry_state=="ADD",
                "condition":"仅针对已有仓位：多周期方向未转弱、15m HH_HL + SuperTrend向上 + 放量 + MACD为正，并且价格未明显远离MA20",
                "requires_existing_position":True
            },
            "buy_or_add":{
                "enabled":entry_state in ("CONFIRMED","ADD"),
                "condition":"兼容旧字段：正式买入或加仓候选；试仓请读取 triggers.probe"
            },
            "reduce_risk":{
                "condition":"15m有效跌破第一支撑且1H继续弱势；跌破第二支撑视为结构进一步恶化",
                "trigger_below":s1,
                "hard_invalidation_below":invalidation
            }
        },
        "reason_codes":[
            "4H_ST_"+f4["supertrend_direction"],
            "1H_"+f1["trend"],
            "15m_"+f15["trend"],
            "15m_structure_"+f15["structure"]["label"],
            "1H_MACD_POS" if f1["macd_hist_okx"]>0 else "1H_MACD_NEG",
            "ENTRY_"+entry_state
        ]
    }

def load_previous_snapshot():
    path=OUT/"latest.json"
    if not path.exists():
        return None
    try:
        previous=json.loads(path.read_text(encoding="utf-8"))
        return previous if isinstance(previous,dict) else None
    except Exception:
        return None

def detect_material_change(previous,current):
    if not previous or "strategy" not in previous:
        return {
            "material":True,
            "reasons":["baseline_initialized"],
            "previous_generated_at":previous.get("generated_at_sgt") if isinstance(previous,dict) else None
        }

    reasons=[]
    prev_s=previous["strategy"]; cur_s=current["strategy"]
    prev_price=float(previous.get("ticker",{}).get("last",current["ticker"]["last"]))
    cur_price=float(current["ticker"]["last"])
    level_tol=max(cur_price*0.004, float(current["frames"]["15m"]["atr10"])*0.75)

    if prev_s.get("state")!=cur_s.get("state"):
        reasons.append(f"state:{prev_s.get('state')}->{cur_s.get('state')}")

    prev_entry=prev_s.get("low_risk_entry",{}).get("entry_state")
    cur_entry=cur_s.get("low_risk_entry",{}).get("entry_state")
    if prev_entry!=cur_entry:
        reasons.append(f"entry_state:{prev_entry}->{cur_entry}")
    old_entry=prev_s.get("low_risk_entry",{}); new_entry=cur_s.get("low_risk_entry",{})
    for key,thresholds in (("buy_score",(55,65,72)),("entry_score",(58,68,70,75))):
        old=old_entry.get(key); new=new_entry.get(key)
        if isinstance(old,(int,float)) and isinstance(new,(int,float)):
            for threshold in thresholds:
                if (old>=threshold)!=(new>=threshold):
                    reasons.append(f"{key}_crossed_{threshold}:{old}->{new}")
    zone_tol=max(0.000001,float(current["frames"]["15m"]["atr10"])*0.1)
    for key in ("limit_order_zone","confirmed_entry_zone"):
        old=old_entry.get(key); new=new_entry.get(key)
        if (old is None)!=(new is None) or (old is not None and new is not None and any(abs(a-b)>=zone_tol for a,b in zip(old,new))):
            reasons.append(f"{key}_changed:{old}->{new}")
    for key in ("limit_order_allowed","hard_no_chase"):
        if old_entry.get(key)!=new_entry.get(key):
            reasons.append(f"{key}:{old_entry.get(key)}->{new_entry.get(key)}")
    if prev_s.get("bias")!=cur_s.get("bias"):
        reasons.append(f"bias:{prev_s.get('bias')}->{cur_s.get('bias')}")

    for tf in ("1H","4H"):
        prev_trend=previous.get("frames",{}).get(tf,{}).get("trend")
        cur_trend=current.get("frames",{}).get(tf,{}).get("trend")
        if prev_trend and cur_trend and prev_trend!=cur_trend:
            reasons.append(f"{tf}_trend:{prev_trend}->{cur_trend}")

    prev_4h_st=previous.get("frames",{}).get("4H",{}).get("supertrend_direction")
    cur_4h_st=current.get("frames",{}).get("4H",{}).get("supertrend_direction")
    if prev_4h_st and cur_4h_st and prev_4h_st!=cur_4h_st:
        reasons.append(f"4H_supertrend:{prev_4h_st}->{cur_4h_st}")

    for key,label in (("support_primary","support"),("resistance_primary","resistance"),("invalidation_level","invalidation")):
        old=prev_s.get(key); new=cur_s.get(key)
        if isinstance(old,(int,float)) and isinstance(new,(int,float)) and abs(new-old)>=level_tol:
            reasons.append(f"{label}_shift:{old}->{new}")

    prev_res=prev_s.get("resistance_primary")
    prev_sup=prev_s.get("support_primary")
    if isinstance(prev_res,(int,float)) and prev_price<=prev_res<cur_price:
        reasons.append(f"price_crossed_resistance_up:{prev_res}")
    if isinstance(prev_sup,(int,float)) and prev_price>=prev_sup>cur_price:
        reasons.append(f"price_crossed_support_down:{prev_sup}")

    prev_score=prev_s.get("score"); cur_score=cur_s.get("score")
    if isinstance(prev_score,(int,float)) and isinstance(cur_score,(int,float)) and abs(cur_score-prev_score)>=3:
        reasons.append(f"score_change:{prev_score}->{cur_score}")

    return {
        "material":bool(reasons),
        "reasons":reasons,
        "level_tolerance":round(level_tol,6),
        "previous_generated_at":previous.get("generated_at_sgt"),
        "previous_price":prev_price,
        "current_price":cur_price
    }

def main():
    previous=load_previous_snapshot()
    t=ticker(); frames={}
    for tf,(bar,limit,recent) in TFS.items(): frames[tf]=metrics(candles(bar,limit),recent)
    lvls=levels(float(t["last"]),frames)
    snap={
        "schema_version":6,"instrument":INST_ID,
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "generated_at_sgt":datetime.now(ZoneInfo("Asia/Singapore")).isoformat(),
        "ticker":t,"summary":summary(frames),"frames":frames,"levels":lvls,
        "strategy":strategy_state(float(t["last"]),frames,lvls,previous,os.getenv("HAS_POSITION","false").lower()=="true"),
        "notes":["low-risk entry v4: LIMIT/PROBE -> CONFIRMED -> ADD; WAIT forbids immediate entry; limit_order_allowed is a separate pending-order decision","confirmed candles only","SuperTrend 10,3","BOLL 20,2","MACD histogram = 2*(DIFF-DEA)"]
    }
    snap["change"]=detect_material_change(previous,snap)
    (OUT/"latest.json").write_text(json.dumps(snap,ensure_ascii=False,indent=2),encoding="utf-8")
    rows="".join(f"<tr><td>{tf}</td><td>{m['close']:.4f}</td><td>{m['trend']}</td><td>{m['structure']['label']}</td><td>{m['rsi6']:.1f}</td><td>{m['supertrend_10_3']:.4f}</td></tr>" for tf,m in frames.items())
    html=f"""<!doctype html><meta charset="utf-8"><title>OPENAI-USDT-SWAP Monitor</title>
    <h1>OPENAI-USDT-SWAP</h1><p>Last: <b>{t['last']}</b> · Regime: <b>{snap['summary']['regime']}</b> · Strategy: <b>{snap['strategy']['state']}</b></p>
    <p>Material change: <b>{snap['change']['material']}</b> · Reasons: {', '.join(snap['change']['reasons']) or 'none'}</p>
    <p>Updated: {snap['generated_at_sgt']} (UTC+8)</p>
    <h2>入场计划（候选信号，不自动下单）</h2><pre>{json.dumps(snap['strategy']['low_risk_entry'],ensure_ascii=False,indent=2)}</pre>
    <table border="1" cellpadding="6" cellspacing="0"><tr><th>周期</th><th>收盘</th><th>趋势</th><th>结构</th><th>RSI6</th><th>SuperTrend</th></tr>{rows}</table>
    <h2>支撑</h2><pre>{json.dumps(snap['levels']['supports'],ensure_ascii=False,indent=2)}</pre>
    <h2>阻力</h2><pre>{json.dumps(snap['levels']['resistances'],ensure_ascii=False,indent=2)}</pre>"""
    (OUT/"index.html").write_text(html,encoding="utf-8")

if __name__=="__main__": main()
