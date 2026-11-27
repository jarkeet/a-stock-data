from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

import market_etf_monitor as monitor


class EtfMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.sqlite3"
        monitor.init_db(self.path)

    def test_gap_and_counter_reset_do_not_create_turnover(self):
        day = "2026-11-27"
        code = "510300"
        with closing(monitor.connect(self.path)) as db, db:
            db.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?)", [
                (day, 580, code, 580 * 60 - 3, 1_000, 4.1),
                (day, 581, code, 581 * 60 - 3, 1_300, 4.2),
                (day, 583, code, 583 * 60 - 3, 9_000, 4.3),
                (day, 584, code, 584 * 60 - 3, 140, 4.4),
                (day, 585, code, 585 * 60 - 30, 160, 4.4),
            ])
            for minute in range(581, 586):
                monitor.finalize_minute(db, day, minute)
            self.assertEqual(
                [tuple(r) for r in db.execute("SELECT minute,amount FROM bars")], [(581, 300)]
            )
            self.assertEqual(monitor.window_history(db, day, 585, 1), {})
            self.assertEqual(monitor.session_start(691), None)
            self.assertEqual(monitor.session_start(898), None)

    def test_group_uses_matching_cohort_and_deduplicates(self):
        codes = ("510300", "510310")
        day = "2026-11-27"
        start = date(2026, 10, 1)
        with closing(monitor.connect(self.path)) as db, db:
            for n in range(50):
                past = start + timedelta(days=n)
                if past.weekday() < 5:
                    db.executemany("INSERT INTO bars VALUES (5,?,?,?,?,?,?)",
                                   [(past.isoformat(), end, c, 10_000_000, 4.2, "test")
                                    for end in (600, 605, 620) for c in codes])
            for end in (600, 605, 620):
                db.executemany("INSERT INTO bars VALUES (1,?,?,?,?,?,?)", [
                    (day, minute, c, 5_000_000, 4.3, "test")
                    for minute in range(end - 4, end + 1) for c in codes
                ])
            for end in (600, 600, 601, 605, 620):
                stats, history = monitor.score_windows(db, day, end)
                monitor.record_events(db, day, end, stats, history)
            results = [tuple(row) for row in db.execute(
                "SELECT kind,first,last,amount FROM events ORDER BY first")]
            self.assertEqual(results, [("group", 600, 605, 50_000_000), ("group", 620, 620, 50_000_000)])
            stats, _ = monitor.score_windows(db, day, 595)
            self.assertFalse(stats[codes[0]]["trigger"])

    def test_fewer_than_thirty_days_and_missing_replay(self):
        stats = monitor.dashboard_data(self.path)
        self.assertEqual(len(stats["funds"]), 30)
        self.assertEqual(sum(f["role"] == "main" for f in stats["funds"]), 19)
        self.assertEqual(monitor.replay_data(self.path, "2026-11-27", "510300")["points"], [])
        with self.assertRaises(ValueError):
            monitor.replay_data(self.path, "2026-11-27", "999999")

    def test_uses_unrounded_cumulative_quote_amount(self):
        fields = [""] * 38
        fields[2], fields[3], fields[30] = "510300", "4.123", "20261127134518"
        fields[6], fields[7], fields[8] = "500", "300", "190"
        fields[32], fields[35], fields[37] = "2", "4.123/500/1723456", "172"
        body = ('v_sh510300="' + "~".join(fields) + '";').encode()
        stamp = datetime(2026, 11, 27, 13, 45, 20, tzinfo=monitor.TZ)
        quote, = monitor.parse_quotes(body, stamp)
        self.assertEqual(quote.amount, 1_723_456)
        self.assertEqual(quote.code, "510300")
        self.assertEqual(quote.net_buy_estimate, round(1_723_456 * 110 / 500))

    def test_daily_assessment_requires_fresh_direction_and_matching_turnover(self):
        trade_day = date(2026, 11, 27)
        day = trade_day.isoformat()
        stamp = datetime(2026, 11, 27, 9, 45, tzinfo=monitor.TZ)
        codes = monitor.PRIMARY_CODES
        controls = monitor.CONTROL_CODES
        with closing(monitor.connect(self.path)) as db, db:
            history = [(5, (trade_day - timedelta(days=i)).isoformat(), end, code, 1_000_000, 4, "test")
                       for i in range(1, 32) for end in (580, 585) for code in (*codes, *controls)]
            session = [(1, day, minute, code, 400_000, 4, "test")
                       for minute in range(576, 586) for code in codes]
            control_bars = [(1, day, minute, code, 200_000, 4, "test")
                            for minute in range(576, 586) for code in controls]
            db.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", history + session + control_bars)
            main_quotes = [monitor.Quote(code, stamp, 4, 0, 4_000_000, 800_000) for code in codes]
            monitor.save_quotes(db, main_quotes, day)
            primary_only = monitor.daily_assessment(db, day, 585, stamp)
            self.assertEqual((primary_only["rating"], primary_only["score"]), ("strong_buy", 100))
            self.assertNotIn("controlMultiple", primary_only)
            monitor.save_quotes(db, [monitor.Quote(code, stamp, 4, 0, 2_000_000, 0) for code in controls], day)
            result = monitor.daily_assessment(db, day, 585, stamp)
            self.assertEqual((result["rating"], result["multiple"], result["coverage"]), ("strong_buy", 2.0, 19))
            self.assertEqual(result["score"], primary_only["score"])
            self.assertEqual(result["comparableAmount"], 76_000_000)
            self.assertEqual(result["netBuyEstimate"], 15_200_000)
            self.assertEqual(monitor.daily_assessment(db, day, 585, stamp + timedelta(minutes=2))["reason"], "stale")
            db.execute("UPDATE daily_direction SET net_buy_estimate=-net_buy_estimate")
            sell = monitor.daily_assessment(db, day, 585, stamp)
            self.assertEqual((sell["rating"], sell["netBuyEstimate"], sell["score"]), ("strong_sell", -15_200_000, 0))
            placeholders = ",".join("?" for _ in controls)
            db.execute(f"UPDATE bars SET amount=amount*2 WHERE span=1 AND code IN ({placeholders})", controls)
            db.execute(f"UPDATE daily_direction SET net_buy_estimate=-400000 WHERE code IN ({placeholders})", controls)
            broad = monitor.daily_assessment(db, day, 585, stamp)
            self.assertEqual((broad["rating"], broad["controlMultiple"], broad["score"]), ("strong_sell", 2.0, 0))
            db.execute("UPDATE daily_direction SET net_buy_estimate=NULL WHERE code IN ('510300','510310','510330','159919')")
            incomplete = monitor.daily_assessment(db, day, 585, stamp)
            self.assertEqual((incomplete["rating"], incomplete["reason"]), ("unknown", "limited_coverage"))
            self.assertIsNone(incomplete["score"])
            self.assertNotIn("netBuyEstimate", incomplete)

    def test_pressure_score_symmetry_and_strength_requirements(self):
        self.assertEqual(monitor.pressure_score(0, 4, 0, 2), 50)
        self.assertEqual(monitor.pressure_score(.2, 7, 0, 2), 100)
        self.assertEqual(monitor.pressure_score(-.2, 0, 7, 2), 0)
        self.assertEqual(monitor.pressure_score(.2, 2, 0, 2), 59)
        self.assertEqual(monitor.pressure_score(-.2, 0, 2, 2), 41)
        self.assertEqual(monitor.pressure_score(.2, 7, 0, 1.49), 79)
        self.assertEqual(monitor.pressure_score(-.2, 0, 7, None), 21)
        self.assertEqual(monitor.pressure_score(.08, 5, 1, 1.4) + monitor.pressure_score(-.08, 1, 5, 1.4), 100)

    def test_verified_closes_keep_asynchronous_quotes_in_the_same_cohort(self):
        day = "2026-11-27"
        stamp = datetime(2026, 11, 27, 15, 1, tzinfo=monitor.TZ)
        codes = monitor.PRIMARY_CODES + monitor.CONTROL_CODES
        with closing(monitor.connect(self.path)) as db, db:
            db.executemany("INSERT INTO samples VALUES (?,900,?,54000,4000000,4)", [(day, code) for code in codes])
            # Different provider stamps can still represent the identical 15:00 close.
            quotes = [monitor.Quote(code, stamp + timedelta(seconds=(index % 3) * 60),
                                    4, 0, 4_000_000, 800_000) for index, code in enumerate(codes)]
            monitor.save_quotes(db, quotes, day)
            now = stamp + timedelta(minutes=5)
            first = monitor.daily_assessment(db, day, None, now)
            self.assertEqual((first["coverage"], first["quoteBasis"]), (19, "close"))
            self.assertIn("T15:00:", first["asOf"])
            # An after-hours amount cannot replace the known close in the direction table.
            monitor.save_quotes(db, [monitor.Quote(codes[0], now, 4, 0, 4_010_000, None)], day)
            self.assertEqual(monitor.daily_assessment(db, day, None, now)["netBuyEstimate"], first["netBuyEstimate"])
            # Quote and close prices must agree when the saved quote is available.
            db.execute("UPDATE quotes SET price=4.01 WHERE code=?", (codes[1],))
            self.assertEqual(monitor.daily_assessment(db, day, None, now)["coverage"], 18)
            db.execute("DELETE FROM samples WHERE minute=900")
            pending = monitor.daily_assessment(db, day, None, now)
            self.assertEqual((pending["rating"], pending["reason"]), ("unknown", "closing_pending"))

    def test_tencent_close_bar_uses_cumulative_amount_at_1500(self):
        client = Mock()
        client.get.return_value.json.return_value = {"data": {"sh510300": {"data": [{
            "date": "20261127",
            "data": ["1456 4.0 1 100", "1457 4.0 2 200", "1458 4.0 3 210",
                     "1459 4.0 3 210", "1500 4.1 5 450", "1530 4.1 7 650"],
        }]}}}
        day = "2026-11-27"
        now = datetime(2026, 11, 27, 15, 31, tzinfo=monitor.TZ)
        closes = []
        bars = list(monitor.tencent_history(client, monitor.FUNDS["510300"], now, closes))
        self.assertEqual(closes, [(day, 900, "510300", 54000, 450, 4.1)])
        self.assertEqual(bars[-1], (1, day, 900, "510300", 250, 4.1, "tencent_close_auction"))
        self.assertEqual([bar[2] for bar in bars], [897, 900])
        self.assertIsNone(monitor.session_start(900))
        with closing(monitor.connect(self.path)) as db, db:
            db.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", bars)
        replay = monitor.replay_data(self.path, day, "510300")
        self.assertTrue(replay["closeAuction"])
        self.assertEqual(replay["points"][-1], ["15:00", 250, 4.1])

    def test_official_shares_never_fill_missing_day_or_treat_jump_as_flow(self):
        with closing(monitor.connect(self.path)) as db, db:
            monitor_codes = ("510300", "159919")
            for day in ("2026-11-24", "2026-11-25", "2026-11-26"):
                db.execute("INSERT INTO bars VALUES (5,?,585,'510300',1000,4,'test')", (day,))
            db.executemany("INSERT INTO etf_share_disclosures VALUES (?,?,?,?)", [
                ("2026-11-24","510300",1000,"SSE"), ("2026-11-26","510300",1010,"SSE"),
                ("2026-11-25","159919",2000,"SZSE"), ("2026-11-26","159919",4000,"SZSE"),
            ])
            latest, series = monitor.shares.disclosure_view(db, "2026-11-26", monitor_codes)
            self.assertIsNone(latest["510300"]["change"])
            self.assertTrue(latest["159919"]["unusual"])
            self.assertIsNone(series[-1]["net"])
            self.assertEqual(monitor.shares.individual_shares("5,610,288.77"), 56_102_887_700)

    def test_recent_direction_does_not_make_a_historical_conclusion(self):
        stamp = datetime(2026, 11, 27, 14, 12, tzinfo=monitor.TZ)
        with closing(monitor.connect(self.path)) as db, db:
            monitor.save_quotes(db, [monitor.Quote("510300", stamp, 4, 0, 1_000_000, 700_000)], "2026-11-27")
            self.assertEqual(monitor.daily_assessment(db, "2026-11-26", None, stamp)["reason"], "no_direction")


if __name__ == "__main__":
    unittest.main()
