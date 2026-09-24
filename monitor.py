import json, math, statistics
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

def low_risk_entry(price,frames,lvls):
    f15,f1,f4=frames["15m"],frames["1H"],frames["4H"]
    supports=lvls["supports"]; resistances=lvls["resistances"]
    s=supports[0] if supports else None; r=resistances[0] if resistances else None
    s_level=s["level"] if s else None; r_level=r["level"] if r else None
    atr=max(float(f15["atr10"]),1e-9)

    # Location: 100 near support, 0 near resistance. Hard gate prevents late chasing.
    if s_level is not None and r_level is not None and r_level>s_level:
        pos=(price-s_level)/(r_level-s_level)
        location=clamp(100*(1-pos))
    elif s_level is not None:
        location=clamp(100-50*max(0,(price-s_level)/atr))
    else:
        location=50.0

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
    volume=clamp(50+(float(f15["volume_ratio_vs_20"])-1)*35)

    # Support quality is deterministic and deliberately conservative.
    support_quality=0 if not s else clamp(30+8*len(s["sources"])+(15 if any(x.startswith("4H ") for x in s["sources"]) else 0)+(10 if any("recent low" in x for x in s["sources"]) else 0))
    resistance_validity=0 if not r else clamp(30+8*len(r["sources"])+(15 if any(x.startswith("4H ") for x in r["sources"]) else 0)+(10 if any("recent high" in x for x in r["sources"]) else 0))
    resistance_bull=100-resistance_validity
    buy_score=clamp(.25*support_quality+.20*resistance_bull+.20*clamp(trend)+.15*clamp(momentum)+.10*volume+.10*clamp(struct))

    # Pullback confirmation is rule based: touch/near support, no decisive break,
    # reclaim above support, HL structure, plus ST or volume confirmation.
    near_support=s_level is not None and abs(price-s_level)<=0.60*atr
    decisive_break=s_level is not None and price < s_level-0.20*atr
    reclaimed=s_level is not None and price>=s_level
    hl=f15["structure"]["label"]=="HH_HL"
    confirm_st=f15["supertrend_direction"]=="up"
    confirm_vol=float(f15["volume_ratio_vs_20"])>=1.10
    pullback_confirmed=near_support and not decisive_break and reclaimed and hl and (confirm_st or confirm_vol)

    # Anti-chase penalty: extended price cannot be rescued by bullish indicators.
    ext=float(f15.get("ma20_extension_atr",0))
    chase_penalty=25 if ext>2 else 0
    hard_no_chase=ext>3 or location<55
    entry_score=clamp(.45*location+.20*support_quality+.15*clamp(trend)+.10*clamp(momentum)+.10*volume-chase_penalty)

    # Breakout-retest: do not buy the initial breakout. A former resistance must
    # first be below price and close enough to act as support on a later snapshot.
    retest_candidate=False
    if r_level is not None and price>r_level:
        retest_candidate=(price-r_level)<=0.50*atr and f15["supertrend_direction"]=="up" and hl and (confirm_vol or f15["macd_hist_okx"]>0)

    if pullback_confirmed and buy_score>=65 and entry_score>=70 and not hard_no_chase:
        entry_state="CONFIRMED"
        entry_mode="PULLBACK"
    elif retest_candidate and buy_score>=65 and entry_score>=68 and ext<=2:
        entry_state="CONFIRMED"
        entry_mode="BREAKOUT_RETEST"
    elif near_support and not decisive_break:
        entry_state="SETUP"
        entry_mode="PULLBACK"
    else:
        entry_state="WAIT"
        entry_mode=None

    return {
        "buy_score":round(buy_score,1),"entry_score":round(entry_score,1),"location_score":round(location,1),
        "support_quality":round(support_quality,1),"resistance_validity":round(resistance_validity,1),
        "entry_state":entry_state,"entry_mode":entry_mode,"hard_no_chase":hard_no_chase,
        "ma20_extension_atr":round(ext,3),"chase_penalty":chase_penalty,
        "rules":{"near_support":near_support,"decisive_break":decisive_break,"reclaimed_support":reclaimed,
                 "15m_HL":hl,"15m_supertrend_up":confirm_st,"volume_ratio_ge_1_2":confirm_vol,
                 "breakout_retest_candidate":retest_candidate},
        "thresholds":{"buy_score_min":65,"entry_score_pullback_min":70,"location_min":55,
                      "near_support_atr":0.60,"break_buffer_atr":0.20,"volume_ratio_confirm":1.10,
                      "chase_penalty_above_ma20_atr":2.0,"hard_no_chase_above_ma20_atr":3.0}
    }

def strategy_state(price, frames, lvls):
    f15=frames["15m"]; f1=frames["1H"]; f4=frames["4H"]
    supports=lvls["supports"]; resistances=lvls["resistances"]
    s1=supports[0]["level"] if supports else None
    s2=supports[1]["level"] if len(supports)>1 else None
    r1=resistances[0]["level"] if resistances else None
    r2=resistances[1]["level"] if len(resistances)>1 else None
    entry=low_risk_entry(price,frames,lvls)

    score=0
    score += 2 if f4["supertrend_direction"]=="up" else -2
    score += 2 if f1["trend"]=="bullish" else (-2 if f1["trend"]=="bearish" else 0)
    score += 1 if f15["trend"]=="bullish" else (-1 if f15["trend"]=="bearish" else 0)
    score += 1 if f15["structure"]["label"]=="HH_HL" else (-1 if f15["structure"]["label"]=="LH_LL" else 0)
    score += 1 if f1["macd_hist_okx"]>0 else -1
    bias="bullish" if score>=4 else ("bearish" if score<=-4 else "mixed")

    if entry["entry_state"]=="CONFIRMED": state="低风险买入/加仓候选"
    elif entry["entry_state"]=="SETUP": state="等待低风险确认"
    elif bias=="bearish" and s1 is not None and price<s1: state="减仓"
    else: state="观望"

    invalidation=s2 if bias=="bullish" and s2 is not None else (s1 if s1 is not None else f4["supertrend_10_3"])
    return {
        "state":state,"bias":bias,"score":score,
        "support_primary":s1,"support_secondary":s2,"resistance_primary":r1,"resistance_secondary":r2,
        "invalidation_level":invalidation,"low_risk_entry":entry,
        "triggers":{
            "buy_or_add":{"enabled":entry["entry_state"]=="CONFIRMED",
                "condition":"仅允许两类低风险入场：回踩强支撑确认，或突破后回踩原阻力确认；禁止初次突破追涨",
                "trigger_above":r1,"confirmation_above":r2},
            "reduce_risk":{"condition":"15m有效跌破第一支撑且1H继续弱势；跌破第二支撑视为结构进一步恶化",
                "trigger_below":s1,"hard_invalidation_below":invalidation}},
        "reason_codes":["4H_ST_"+f4["supertrend_direction"],"1H_"+f1["trend"],"15m_"+f15["trend"],
            "15m_structure_"+f15["structure"]["label"],"1H_MACD_POS" if f1["macd_hist_okx"]>0 else "1H_MACD_NEG",
            "ENTRY_"+entry["entry_state"]]
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
        "schema_version":4,"instrument":INST_ID,
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "generated_at_sgt":datetime.now(ZoneInfo("Asia/Singapore")).isoformat(),
        "ticker":t,"summary":summary(frames),"frames":frames,"levels":lvls,
        "strategy":strategy_state(float(t["last"]),frames,lvls),
        "notes":["low-risk entry v2: calibrated pullback + breakout-retest, anti-chase hard gate","confirmed candles only","SuperTrend 10,3","BOLL 20,2","MACD histogram = 2*(DIFF-DEA)"]
    }
    snap["change"]=detect_material_change(previous,snap)
    (OUT/"latest.json").write_text(json.dumps(snap,ensure_ascii=False,indent=2),encoding="utf-8")
    rows="".join(f"<tr><td>{tf}</td><td>{m['close']:.4f}</td><td>{m['trend']}</td><td>{m['structure']['label']}</td><td>{m['rsi6']:.1f}</td><td>{m['supertrend_10_3']:.4f}</td></tr>" for tf,m in frames.items())
    html=f"""<!doctype html><meta charset="utf-8"><title>OPENAI-USDT-SWAP Monitor</title>
    <h1>OPENAI-USDT-SWAP</h1><p>Last: <b>{t['last']}</b> · Regime: <b>{snap['summary']['regime']}</b> · Strategy: <b>{snap['strategy']['state']}</b></p>
    <p>Material change: <b>{snap['change']['material']}</b> · Reasons: {', '.join(snap['change']['reasons']) or 'none'}</p>
    <p>Updated: {snap['generated_at_sgt']} (UTC+8)</p>
    <table border="1" cellpadding="6" cellspacing="0"><tr><th>周期</th><th>收盘</th><th>趋势</th><th>结构</th><th>RSI6</th><th>SuperTrend</th></tr>{rows}</table>
    <h2>支撑</h2><pre>{json.dumps(snap['levels']['supports'],ensure_ascii=False,indent=2)}</pre>
    <h2>阻力</h2><pre>{json.dumps(snap['levels']['resistances'],ensure_ascii=False,indent=2)}</pre>"""
    (OUT/"index.html").write_text(html,encoding="utf-8")

if __name__=="__main__": main()
