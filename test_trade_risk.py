import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import monitor
import trade_risk as risk
from test_monitor import sample, entry


def iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()


def plan_row(ts=0, side="long"):
    return {"generated_at_utc":iso(ts), "state":"PROBE" if side=="long" else "SHORT_PROBE",
            "probe_plan":{"version":risk.VERSION,"side":side,"stop":99 if side=="long" else 101,
                          "target":102 if side=="long" else 98,"entry_zone":[99.8,100.2],
                          "atr":1,"ma20":100,"regime_4h":"neutral",
                          "fee_bps_per_side":5,"slippage_bps_per_side":5}}


def path():
    return [{"ts":i*risk.BAR_MS,"open":100,"high":100.3,"low":99.7,"close":100.1,"confirm":True}
            for i in range(1,97)]


class RiskTests(unittest.TestCase):
    def test_costs_reduce_rr_and_are_symmetric(self):
        a=risk.trade_math(100,99,102)
        self.assertLess(a["net_rr"],2)
        self.assertGreater(a["stop_loss_pct"],1)
        self.assertLess(risk.trade_math(100,101,98,"short")["net_rr"],2)
        for stop,target in ((101,102),(99,98),(None,102)):
            self.assertFalse(risk.trade_math(100,stop,target)["valid"])

    def test_unreclaimed_and_first_breakout_do_not_probe(self):
        s=sample(164.45,volume=.9)
        self.assertFalse(entry(s)["rules"]["probe_candidate"])
        prev=sample();prev["frames"]["15m"].update(asof_ts=900000,close=164.4)
        prev["levels"]["resistances"].insert(0,{"level":164.49,"sources":["1H MA20"]})
        self.assertNotIn(entry(sample(previous=prev))["entry_state"],("PROBE","CONFIRMED","ADD"))

    def test_live_extension_not_stale_closed_extension(self):
        s=sample();s["frames"]["15m"]["ma20"]=163.0
        e=monitor.low_risk_entry(164.55,s["frames"],s["levels"])
        self.assertTrue(e["hard_no_chase"])
        self.assertNotIn(e["entry_state"],("PROBE","CONFIRMED","ADD"))

    def test_short_setup_does_not_hide_long_probe_and_conflict_abstains(self):
        s=sample(volume=.9)
        short=copy.deepcopy(s["strategy"]["low_risk_short"])
        short["entry_state"]="SHORT_SETUP"
        with patch.object(monitor,"low_risk_short",return_value=short):
            self.assertEqual(monitor.strategy_state(164.55,s["frames"],s["levels"])["state"],"试仓候选")
        short["entry_state"]="SHORT_PROBE"
        with patch.object(monitor,"low_risk_short",return_value=short):
            result=monitor.strategy_state(164.55,s["frames"],s["levels"])
        self.assertTrue(result["signal_conflict"])
        self.assertFalse(result["triggers"]["probe"]["enabled"])
        self.assertFalse(result["triggers"]["short_probe"]["enabled"])

    def test_complete_path_and_no_lookahead_or_overlap(self):
        rows=[plan_row(),plan_row(risk.BAR_MS)]
        bars=path();bars[0]["low"]=98.9
        end=iso(97*risk.BAR_MS)
        events,diag=risk.probe_outcomes(rows,bars,end,[])
        self.assertEqual(diag["selected_nonoverlapping_plans"],1)
        self.assertEqual(len(events),2)
        self.assertTrue(all(e["outcome"]=="stop" for e in events))
        self.assertEqual(risk.probe_outcomes(rows,bars,iso(16*risk.BAR_MS),[])[0],[])
        self.assertEqual(risk.probe_outcomes(rows,bars,end,events)[0],[])
        missing,_=risk.probe_outcomes(rows,bars[1:],end,[])
        self.assertEqual(missing,[])
        future,_=risk.probe_outcomes(rows,[dict(b,confirm=False) for b in bars],end,[])
        self.assertEqual(future,[])

    def test_both_barriers_is_unknown_and_small_sample_has_no_probability(self):
        bars=path();bars[0].update(high=103,low=98)
        now=iso(97*risk.BAR_MS)
        events,_=risk.probe_outcomes([plan_row()],bars,now,[])
        self.assertEqual(events[0]["outcome"],"ambiguous")
        stat=risk.stop_statistics(events,now,"long","neutral")["4"]
        self.assertEqual(stat["samples"],1)
        self.assertEqual(stat["observed_rate_bounds"],[0,1])
        self.assertIsNone(stat["probability_estimate"])
        self.assertEqual(risk.stop_statistics(events,iso(16*risk.BAR_MS),"long","neutral")["4"]["samples"],0)
        self.assertEqual(risk.stop_statistics(events,now,"short","neutral")["4"]["samples"],0)

    def test_gap_stop_uses_open_and_invalid_entry_is_not_trade(self):
        bars=path();bars[1]["open"]=98.5;bars[1]["low"]=98.4
        now=iso(97*risk.BAR_MS)
        events,_=risk.probe_outcomes([plan_row()],bars,now,[])
        self.assertEqual(events[0]["outcome"],"stop")
        self.assertLess(events[0]["return_pct_lower"],-1.5)
        bars[0]["open"]=101
        events,_=risk.probe_outcomes([plan_row()],bars,now,[])
        self.assertEqual(events[0]["outcome"],"not_entered")
        self.assertEqual(risk.stop_statistics(events,now,"long","neutral")["24"]["samples"],0)

    def test_legacy_has_no_invented_stop(self):
        rows=[{"state":"PROBE","generated_at_utc":iso(0),"entry_price":100}]
        events,diag=risk.probe_outcomes(rows,path(),iso(97*risk.BAR_MS),[])
        self.assertEqual(events,[])
        self.assertEqual(diag["excluded_legacy_without_frozen_plan"],1)

    def test_statistics_deduplicate_and_filter_versions(self):
        now=iso(97*risk.BAR_MS)
        events,_=risk.probe_outcomes([plan_row()],path(),now,[])
        event=events[0]
        cohort=[dict(event,evaluation_id=str(i),outcome="stop" if i<15 else "target") for i in range(100)]
        stat=risk.stop_statistics(cohort+cohort,now,"long","neutral")["4"]
        self.assertEqual(stat["samples"],100)
        self.assertEqual(stat["probability_estimate"],.15)
        self.assertLess(stat["wilson_95_interval"][0],.15)
        self.assertGreater(stat["wilson_95_interval"][1],.15)
        cohort[0]["version"]="legacy"
        self.assertEqual(risk.stop_statistics(cohort,now,"long","neutral")["4"]["samples"],99)

    def test_partial_exits_use_current_position_and_never_invent_fill(self):
        s=sample();p={"side":"long","quantity":100,"initial_stop":160,"stop":160,
                      "tp1":165,"tp2":168,"tp1_done":False,"tp2_done":False}
        first=risk.exit_plan(p,165.5,s["frames"],s["levels"])
        self.assertEqual(first["action"],"TAKE_PROFIT_1")
        self.assertEqual(first["reduce_current_fraction"],.25)
        self.assertFalse(p["tp1_done"])
        p.update(quantity=95,tp1_done=True)
        second=risk.exit_plan(p,168.5,s["frames"],s["levels"])
        self.assertEqual(second["action"],"TAKE_PROFIT_2")
        self.assertAlmostEqual(95*(1-second["reduce_current_fraction"]),28.5)
        self.assertEqual(second["proposed_tp2"],168)
        self.assertEqual(risk.exit_plan(p,159,s["frames"],s["levels"])["action"],"EXIT_ALL")

    def test_tail_stop_never_widens_and_missing_position_has_no_sell(self):
        s=sample();p={"side":"long","quantity":30,"initial_stop":160,"stop":164,
                      "tp1":165,"tp2":168,"tp1_done":True,"tp2_done":True}
        result=risk.exit_plan(p,169,s["frames"],s["levels"])
        self.assertEqual(result["proposed_stop"],164)
        self.assertFalse(result["allow_add_on_new_grade_only"])
        self.assertEqual(risk.exit_plan(None,169,s["frames"],s["levels"])["status"],"position_context_required")

    def test_stale_data_blocks_signals_and_repeats_are_not_new_entries(self):
        snap=sample(volume=.9);snap.update(generated_at_utc=iso(18000000))
        snap["ticker"]["ts"]=18000000
        for tf,duration in (("15m",900000),("1H",3600000),("4H",14400000)):
            snap["frames"][tf]["asof_ts"]=(18000000//duration-1)*duration
        first=risk.assess_snapshot(snap)
        self.assertTrue(first["rhythm"]["long"]["new_actionable_signal"])
        prev=copy.deepcopy(snap);prev["risk_assessment"]=first
        self.assertFalse(risk.assess_snapshot(snap,prev)["rhythm"]["long"]["new_actionable_signal"])
        snap["ticker"]["ts"]=0
        result=risk.assess_snapshot(snap)
        self.assertIn("STALE_OR_UNVERIFIED_MARKET_DATA",result["warnings"])
        self.assertEqual(entry(snap)["entry_state"],"WAIT")
        self.assertFalse(snap["strategy"]["triggers"]["probe"]["enabled"])

    def test_exit_and_tail_take_priority_over_new_entries(self):
        for tail in (False,True):
            snap=sample(has_position=True);snap.update(generated_at_utc=iso(18000000))
            snap["ticker"]["ts"]=18000000
            for tf,duration in (("15m",900000),("1H",3600000),("4H",14400000)):
                snap["frames"][tf]["asof_ts"]=(18000000//duration-1)*duration
            p={"side":"long","quantity":95,"initial_stop":160,"stop":160,
               "tp1":164,"tp2":168,"tp1_done":tail,"tp2_done":tail,
               "last_filled_entry_state":"PROBE"}
            snap["execution_plan"]=risk.exit_plan(p,164.55,snap["frames"],snap["levels"])
            r=risk.assess_snapshot(snap,position=p)
            self.assertEqual(entry(snap)["entry_state"],"WAIT")
            self.assertFalse(snap["strategy"]["triggers"]["add"]["enabled"])
            self.assertFalse(r["rhythm"]["long"]["position_grade_upgrade_candidate"])

    def test_risk_and_exit_changes_are_material_without_price_change(self):
        old=sample();new=copy.deepcopy(old)
        new["risk_assessment"]={"warnings":["LONG_4H_STOP_RISK_UNCERTAIN"]}
        new["execution_plan"]={"status":"advisory_only","action":"TAKE_PROFIT_1"}
        reasons=monitor.detect_material_change(old,new)["reasons"]
        self.assertTrue(any(x.startswith("risk_warnings_changed") for x in reasons))
        self.assertTrue(any(x.startswith("exit_action") for x in reasons))


if __name__ == "__main__":
    unittest.main()
