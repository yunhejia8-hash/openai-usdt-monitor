import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor


def sample(price=164.55, volume=1.5, has_position=False, previous=None):
    frame={"asof_ts":1800000,"open":164.48,"low":164.46,"high":164.65,
           "close":164.55,"atr10":0.318,"trend":"bullish","supertrend_direction":"up",
           "supertrend_10_3":163.338,"macd_hist_okx":0.1,"rsi6":60,
           "volume_ratio_vs_20":volume,"ma20_extension_atr":1.0,
           "structure":{"label":"HH_HL"}}
    frames={tf:copy.deepcopy(frame) for tf in ("15m","1H","4H")}
    levels={"supports":[{"level":164.49,"sources":["4H recent low","1H MA20","15m MA10","15m SuperTrend"],"strength":4},
                         {"level":163.338,"sources":["4H SuperTrend"],"strength":1}],
            "resistances":[{"level":168.0,"sources":["1H recent high"],"strength":1}]}
    return {"ticker":{"last":price},"frames":frames,"levels":levels,
            "strategy":monitor.strategy_state(price,frames,levels,previous,has_position)}


def short_sample(price=164.45, volume=1.5, has_short_position=False):
    frame={"asof_ts":1800000,"open":164.60,"low":164.30,"high":164.55,
           "close":164.45,"atr10":0.318,"trend":"bearish","supertrend_direction":"down",
           "supertrend_10_3":165.0,"macd_hist_okx":-0.1,"rsi6":40,
           "volume_ratio_vs_20":volume,"ma20_extension_atr":-1.0,
           "structure":{"label":"LH_LL"}}
    frames={tf:copy.deepcopy(frame) for tf in ("15m","1H","4H")}
    levels={"supports":[{"level":161.5,"sources":["1H recent low"],"strength":1}],
            "resistances":[{"level":164.49,"sources":["4H recent high","1H MA20","15m MA10","15m SuperTrend"],"strength":4},
                           {"level":165.6,"sources":["4H SuperTrend"],"strength":1}]}
    return {"ticker":{"last":price},"frames":frames,"levels":levels,
            "strategy":monitor.strategy_state(price,frames,levels,None,False,has_short_position)}

    return snap["strategy"]["low_risk_entry"]


def entry(snap):
    return snap["strategy"]["low_risk_entry"]


class EntryTests(unittest.TestCase):
    def test_state_sequence(self):
        states=[entry(sample(164.95))["entry_state"],entry(sample(volume=0.9))["entry_state"],
                entry(sample())["entry_state"],entry(sample(has_position=True))["entry_state"]]
        self.assertEqual(states,["WAIT","PROBE","CONFIRMED","ADD"])

    def test_probe_has_mandatory_stop_and_rr(self):
        s=sample(164.55,volume=0.9)
        e=entry(s)
        self.assertEqual(e["entry_state"],"PROBE")
        self.assertIsNotNone(e["probe_stop"])
        self.assertLess(e["probe_stop"],s["ticker"]["last"])
        self.assertGreaterEqual(e["probe_rr"],1.0)
        self.assertGreater(e["probe_stop_distance"],0)
        self.assertEqual(e["probe_position_size_class"],"small")

    def test_requested_zone_and_no_chase(self):
        e=entry(sample(164.95))
        self.assertTrue(e["hard_no_chase"])
        self.assertTrue(e["limit_order_allowed"])
        self.assertLessEqual(e["limit_order_zone"][0],164.45)
        self.assertGreaterEqual(e["limit_order_zone"][1],164.65)
        for price in (164.45,164.55,164.65):
            self.assertEqual(entry(sample(price,volume=0.9))["entry_state"],"PROBE")

    def test_distance_is_continuous_across_old_gate(self):
        a=entry(sample(164.49+0.598*0.318))
        b=entry(sample(164.49+0.602*0.318))
        self.assertAlmostEqual(a["location_score"]-b["location_score"],0.2)
        self.assertLess(abs(a["entry_score"]-b["entry_score"]),0.2)

    def test_all_confirmations_required(self):
        for key,value in (("supertrend_direction","down"),("macd_hist_okx",-0.1),
                          ("volume_ratio_vs_20",0.9),("structure",{"label":"contracting"}),
                          ("low",164.8),("open",164.7)):
            s=sample(); s["frames"]["15m"][key]=value
            e=monitor.low_risk_entry(164.55,s["frames"],s["levels"])
            self.assertNotIn(e["entry_state"],("CONFIRMED","ADD"),key)

    def test_position_must_be_explicit_boolean(self):
        for position in (False,None,"true",1):
            self.assertNotEqual(entry(sample(has_position=position))["entry_state"],"ADD")

    def test_rr_and_missing_resistance(self):
        for resistance in (164.60,None):
            s=sample()
            s["levels"]["resistances"]=[] if resistance is None else [{"level":resistance,"sources":["1H MA20"]}]
            e=monitor.low_risk_entry(164.55,s["frames"],s["levels"],has_position=True)
            self.assertFalse(e["rules"]["risk_reward_ok"])
            self.assertNotIn(e["entry_state"],("CONFIRMED","ADD"))

    def test_add_has_stricter_rr(self):
        s=sample(); s["levels"]["resistances"][0]["level"]=166.6
        e=monitor.low_risk_entry(164.55,s["frames"],s["levels"],has_position=True)
        self.assertEqual(e["entry_state"],"CONFIRMED")

    def test_invalid_atr_and_no_support_fail_closed(self):
        for atr in (0,-1,float("nan")):
            s=sample(); s["frames"]["15m"]["atr10"]=atr
            self.assertEqual(monitor.low_risk_entry(164.55,s["frames"],s["levels"])["entry_state"],"WAIT")
        s=sample(); s["levels"]["supports"]=[]
        self.assertEqual(monitor.low_risk_entry(164.55,s["frames"],s["levels"])["entry_state"],"WAIT")

    def test_ma_extension_gate(self):
        s=sample(); s["frames"]["15m"]["ma20_extension_atr"]=3.1
        self.assertEqual(monitor.low_risk_entry(164.55,s["frames"],s["levels"])["entry_state"],"WAIT")

    def test_prior_support_survives_role_change(self):
        prev=sample(); s=sample(164.45)
        s["levels"]["supports"].pop(0)
        s["levels"]["resistances"].insert(0,prev["levels"]["supports"][0])
        e=monitor.low_risk_entry(164.45,s["frames"],s["levels"],prev)
        self.assertEqual(e["support_anchor"],164.49)
        self.assertEqual(e["entry_state"],"PROBE")
        s["strategy"]["low_risk_entry"]=e
        repeated=monitor.low_risk_entry(164.45,s["frames"],s["levels"],s)
        self.assertEqual(repeated["support_anchor"],164.49)
        self.assertEqual(repeated["entry_state"],"PROBE")
        e=monitor.low_risk_entry(164.40,s["frames"],s["levels"],prev)
        self.assertEqual(e["entry_state"],"WAIT")

    def test_breakout_requires_later_closed_retest(self):
        prev=sample(); prev["frames"]["15m"].update(asof_ts=900000,close=164.40)
        prev["levels"]["resistances"].insert(0,{"level":164.49,"sources":["1H MA20"]})
        first=sample(previous=prev)
        self.assertEqual(entry(first)["breakout_level"],164.49)
        self.assertNotIn(entry(first)["entry_state"],("CONFIRMED","ADD"))
        same=sample(previous=first)
        self.assertNotIn(entry(same)["entry_state"],("CONFIRMED","ADD"))
        later=sample(); later["frames"]["15m"]["asof_ts"]=2700000
        e=monitor.low_risk_entry(164.55,later["frames"],later["levels"],first)
        self.assertTrue(e["rules"]["breakout_retest_candidate"])
        self.assertEqual(e["entry_mode"],"BREAKOUT_RETEST")
        self.assertEqual(e["entry_state"],"CONFIRMED")

    def test_change_thresholds_both_directions_and_zone_tolerance(self):
        s=sample()
        self.assertFalse(monitor.detect_material_change(s,copy.deepcopy(s))["material"])
        for key,thresholds in (("buy_score",(55,65,72)),("entry_score",(58,68,70,75))):
            for t in thresholds:
                a=copy.deepcopy(s); b=copy.deepcopy(s)
                entry(a)[key]=t-0.1; entry(b)[key]=t
                for old,new in ((a,b),(b,a)):
                    self.assertTrue(any(key+"_crossed" in r for r in monitor.detect_material_change(old,new)["reasons"]))
        b=copy.deepcopy(s); entry(b)["limit_order_zone"][0]+=0.001
        self.assertFalse(monitor.detect_material_change(s,b)["material"])
        entry(b)["limit_order_zone"][0]+=0.04
        self.assertTrue(monitor.detect_material_change(s,b)["material"])
        b=copy.deepcopy(s); entry(b)["confirmed_entry_zone"]=None
        self.assertTrue(monitor.detect_material_change(s,b)["material"])

    def test_legacy_snapshot_and_fields(self):
        s=sample(); old=copy.deepcopy(s)
        old["strategy"]["low_risk_entry"]={}
        self.assertTrue(monitor.detect_material_change(old,s)["material"])
        self.assertTrue({"buy_score","entry_score","rules","thresholds","location_score"}<=entry(s).keys())
        self.assertTrue({"probe","confirmed_buy","add","buy_or_add","reduce_risk"}<=s["strategy"]["triggers"].keys())

    def test_main_writes_json_html_and_loads_previous(self):
        s=sample()
        for f in s["frames"].values():
            for key in ("ma10","ma20","boll_upper","boll_lower","recent_high","recent_low"):
                f[key]=164.49
        with tempfile.TemporaryDirectory() as folder, patch.object(monitor,"OUT",Path(folder)), \
                patch.object(monitor,"ticker",return_value=s["ticker"]), \
                patch.object(monitor,"candles",return_value=[]), \
                patch.object(monitor,"metrics",side_effect=list(s["frames"].values())*2), \
                patch.object(monitor,"levels",return_value=s["levels"]), \
                patch.dict("os.environ",{"HAS_POSITION":"true"}):
            monitor.main()
            written=json.loads((Path(folder)/"latest.json").read_text(encoding="utf-8"))
            self.assertEqual(written["schema_version"],8)
            self.assertEqual(entry(written)["entry_state"],"ADD")
            self.assertIn("limit_order_zone",(Path(folder)/"index.html").read_text(encoding="utf-8"))
            monitor.main()
            self.assertFalse(monitor.load_previous_snapshot()["change"]["material"])


    def test_short_probe_has_stop_and_rr(self):
        s=short_sample(volume=0.9)
        e=s["strategy"]["low_risk_short"]
        self.assertEqual(e["entry_state"],"SHORT_PROBE")
        self.assertGreater(e["short_stop"],s["ticker"]["last"])
        self.assertGreaterEqual(e["short_rr"],1.0)
        self.assertEqual(e["short_position_size_class"],"small")

    def test_short_wait_does_not_follow_long_wait_automatically(self):
        s=sample()
        e=s["strategy"]["low_risk_short"]
        self.assertEqual(e["entry_state"],"WAIT")
        self.assertFalse(s["strategy"]["triggers"]["short_probe"]["enabled"])

    def test_short_confirmed_and_add_need_bearish_confirmation(self):
        s=short_sample()
        self.assertEqual(s["strategy"]["low_risk_short"]["entry_state"],"SHORT_CONFIRMED")
        s=short_sample(has_short_position=True)
        self.assertEqual(s["strategy"]["low_risk_short"]["entry_state"],"SHORT_ADD")
        s=short_sample()
        s["frames"]["15m"]["supertrend_direction"]="up"
        e=monitor.low_risk_short(s["ticker"]["last"],s["frames"],s["levels"])
        self.assertNotIn(e["entry_state"],("SHORT_CONFIRMED","SHORT_ADD"))

    def test_short_no_chase_after_drop(self):
        s=short_sample()
        s["frames"]["15m"]["ma20_extension_atr"]=-3.2
        e=monitor.low_risk_short(s["ticker"]["last"],s["frames"],s["levels"])
        self.assertEqual(e["entry_state"],"WAIT")
        self.assertTrue(e["hard_no_chase"])

    def test_metrics_preserve_indicators_and_closed_candle_evidence(self):
        candles=[{"ts":i*900000,"open":160+i*.01,"low":159.9+i*.01,
                  "high":160.2+i*.01,"close":160.1+i*.01,"volQuote":100+i} for i in range(100)]
        m=monitor.metrics(candles,72)
        self.assertEqual(m["low"],candles[-1]["low"])
        self.assertTrue({"ma5","ma10","ma20","boll_upper","rsi6","macd_hist_okx",
                         "atr10","supertrend_direction","structure","volume_ratio_vs_20"}<=m.keys())


if __name__=="__main__":
    unittest.main()
