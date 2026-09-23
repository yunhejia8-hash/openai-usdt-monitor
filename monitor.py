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
        "atr10":round(float(atr10),6),"supertrend_10_3":round(st,6),"supertrend_direction":sdirection,
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

def strategy_state(price, frames, lvls):
    f15=frames["15m"]; f1=frames["1H"]; f4=frames["4H"]
    supports=lvls["supports"]; resistances=lvls["resistances"]
    s1=supports[0]["level"] if supports else None
    s2=supports[1]["level"] if len(supports)>1 else None
    r1=resistances[0]["level"] if resistances else None
    r2=resistances[1]["level"] if len(resistances)>1 else None

    score=0
    score += 2 if f4["supertrend_direction"]=="up" else -2
    score += 2 if f1["trend"]=="bullish" else (-2 if f1["trend"]=="bearish" else 0)
    score += 1 if f15["trend"]=="bullish" else (-1 if f15["trend"]=="bearish" else 0)
    score += 1 if f15["structure"]["label"]=="HH_HL" else (-1 if f15["structure"]["label"]=="LH_LL" else 0)
    score += 1 if f1["macd_hist_okx"]>0 else -1

    bias="bullish" if score>=4 else ("bearish" if score<=-4 else "mixed")
    if bias=="bullish" and r1 is not None and price>r1:
        state="可加仓"
    elif bias=="bullish":
        state="可试仓"
    elif bias=="bearish" and s1 is not None and price<s1:
        state="减仓"
    else:
        state="观望"

    invalidation=s2 if bias=="bullish" and s2 is not None else (s1 if s1 is not None else f4["supertrend_10_3"])

    return {
        "state":state,
        "bias":bias,
        "score":score,
        "support_primary":s1,
        "support_secondary":s2,
        "resistance_primary":r1,
        "resistance_secondary":r2,
        "invalidation_level":invalidation,
        "triggers":{
            "buy_or_add":{
                "enabled":bias!="bearish",
                "condition":"15m收盘重新站上第一阻力且15m SuperTrend翻多；1H同步转强则确认度更高",
                "trigger_above":r1,
                "confirmation_above":r2
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
            "1H_MACD_POS" if f1["macd_hist_okx"]>0 else "1H_MACD_NEG"
        ]
    }

def main():
    t=ticker(); frames={}
    for tf,(bar,limit,recent) in TFS.items(): frames[tf]=metrics(candles(bar,limit),recent)
    lvls=levels(float(t["last"]),frames)
    snap={
        "schema_version":2,"instrument":INST_ID,
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "generated_at_sgt":datetime.now(ZoneInfo("Asia/Singapore")).isoformat(),
        "ticker":t,"summary":summary(frames),"frames":frames,"levels":lvls,
        "strategy":strategy_state(float(t["last"]),frames,lvls),
        "notes":["confirmed candles only","SuperTrend 10,3","BOLL 20,2","MACD histogram = 2*(DIFF-DEA)"]
    }
    (OUT/"latest.json").write_text(json.dumps(snap,ensure_ascii=False,indent=2),encoding="utf-8")
    rows="".join(f"<tr><td>{tf}</td><td>{m['close']:.4f}</td><td>{m['trend']}</td><td>{m['structure']['label']}</td><td>{m['rsi6']:.1f}</td><td>{m['supertrend_10_3']:.4f}</td></tr>" for tf,m in frames.items())
    html=f"""<!doctype html><meta charset="utf-8"><title>OPENAI-USDT-SWAP Monitor</title>
    <h1>OPENAI-USDT-SWAP</h1><p>Last: <b>{t['last']}</b> · Regime: <b>{snap['summary']['regime']}</b></p>
    <p>Updated: {snap['generated_at_sgt']} (UTC+8)</p>
    <table border="1" cellpadding="6" cellspacing="0"><tr><th>周期</th><th>收盘</th><th>趋势</th><th>结构</th><th>RSI6</th><th>SuperTrend</th></tr>{rows}</table>
    <h2>支撑</h2><pre>{json.dumps(snap['levels']['supports'],ensure_ascii=False,indent=2)}</pre>
    <h2>阻力</h2><pre>{json.dumps(snap['levels']['resistances'],ensure_ascii=False,indent=2)}</pre>"""
    (OUT/"index.html").write_text(html,encoding="utf-8")

if __name__=="__main__": main()
