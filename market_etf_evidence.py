from calendar import monthrange
import json

# Report dates, disclosed shares, and the fund manager's report.
# Named points sum the two directly named Huijin accounts. An explicitly named
# investment mandate is outside that comparison so that each report uses the same scope.
SHANGHAI = "https://www.sse.com.cn/disclosure/fund/announcement/c/new/"
REPORTS = {
    "510300": (
        ("2025-06-30", "named", 35_654_598_859 + 37_858_474_974,
         SHANGHAI + "2025-08-30/510300_20250830_9SZQ.pdf#page=58"),
        ("2025-12-31", "named", 35_654_598_859 + 37_858_474_974,
         SHANGHAI + "2026-03-31/510300_20260331_W6CZ.pdf#page=75"),
        ("2026-03-31", "fund", 44_828_587_690,
         SHANGHAI + "2026-04-22/510300_20260422_XODY.pdf#page=2"),
        ("2026-06-30", "institutions", 11_660_211_416,
         "https://static.howbuy.com/gkxx/ggfile/2026/2026-8/2026-08-29/5497945.pdf#page=60"),
        ("2026-09-30", "fund", 24_119_587_690,
         "https://pdf.dfcfw.com/pdf/H2_AN202610261830938404_1.pdf#page=2"),
    ),
    "510310": (
        ("2025-12-31", "named", 27_355_388_757 + 28_538_696_695,
         "https://pdf.dfcfw.com/pdf/H2_AN202603301820873936_1.pdf#page=75"),
        ("2026-09-30", "fund", 12_031_903_922,
         "https://pdf.dfcfw.com/pdf/H2_AN202610261830944912_1.pdf#page=2"),
    ),
    "510050": (
        ("2025-06-30", "named", 28_791_513_899 + 19_967_517_877,
         SHANGHAI + "2025-08-30/510050_20250830_PB5F.pdf"),
        ("2025-12-31", "named", 28_791_513_899 + 19_967_517_877,
         "https://stockmc.xueqiu.com/202603/510050_20260331_O8IB.pdf#page=53"),
        ("2026-06-30", "institutions", 2_032_171_243,
         "https://www.9fzt.com/detail/fund_510050_2_10808363.html"),
        ("2026-09-30", "fund", 6_938_766_757,
         "https://dataclouds.cninfo.com.cn/sjother2/fund/2026/20261026/1af566669b456161971779d473d6c9b6.pdf#page=3"),
    ),
    "510500": (
        ("2025-06-30", "named", 8_235_101_633 + 5_981_977_518,
         SHANGHAI + "2025-08-30/510500_20250830_XOCN.pdf#page=56"),
        ("2025-12-31", "named", 8_235_101_633 + 5_981_977_518,
         "https://big5.sse.com.cn/site/cht/www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-31/510500_20260331_Q394.pdf#page=90"),
        ("2026-06-30", "institutions", 1_824_187_872,
         "https://www.9fzt.com/detail/fund_510500_2_10808674.html"),
        ("2026-09-30", "fund", 5_611_368_617,
         "https://dataclouds.cninfo.com.cn/sjother2/fund/2026/20261026/d792c41f2751310415558300f8abeff2.pdf#page=3"),
    ),
    "588080": (
        ("2025-12-31", "named", 11_806_564_625 + 9_800_143_835,
         "https://stockmc.xueqiu.com/202603/588080_20260331_9RCR.pdf#page=64"),
        ("2026-03-31", "fund", 29_068_628_122,
         SHANGHAI + "2026-04-22/588080_20260422_FVAH.pdf#page=2"),
        # The institutional total explicitly includes the ETF feeder fund.
        ("2026-06-30", "institutions_excluding_feeder", 13_910_026_765 - 9_541_617_238,
         "https://stockmc.xueqiu.com/202608/588080_20260831_NRQ7.pdf#page=47"),
        ("2026-09-30", "fund", 27_103_628_122,
         "https://stockmc.xueqiu.com/202610/588080_20261027_ONJN.pdf#page=2"),
    ),
}


def holdings(funds):
    return [
        {"code": code, "label": f"{funds[code].index} · {funds[code].provider}",
         "points": [{"day": day, "kind": kind, "shares": value, "url": url}
                    for day, kind, value, url in reports]}
        for code, reports in REPORTS.items()
    ]


def daily_history(db, through, primary):
    first_year = through.year - 3
    first_day = min(through.day, monthrange(first_year, through.month)[1])
    first = through.replace(year=first_year, day=first_day).isoformat()
    # Include a prior disclosure to calculate the first plotted change.
    previous = db.execute(
        "SELECT max(day) FROM etf_share_disclosures WHERE day < ?", (first,)
    ).fetchone()[0]
    start = previous or first
    end = through.isoformat()
    days = [row[0] for row in db.execute("""
        SELECT day FROM (
            SELECT day FROM etf_share_disclosures WHERE day BETWEEN ? AND ?
            UNION SELECT day FROM bars WHERE span=1 AND day BETWEEN ? AND ?
            UNION SELECT day FROM bars WHERE span=5 AND day BETWEEN ? AND ?
            UNION SELECT day FROM events WHERE day BETWEEN ? AND ?
        ) ORDER BY day
    """, (start, end, start, end, start, end, start, end))]
    shares = {}
    for row in db.execute("SELECT day,code,shares FROM etf_share_disclosures WHERE day BETWEEN ? AND ?", (start, end)):
        shares.setdefault(row["day"], {})[row["code"]] = row["shares"]
    events = {}
    for row in db.execute("SELECT * FROM events WHERE day BETWEEN ? AND ? ORDER BY day,peak,amount DESC", (first, end)):
        events.setdefault(row["day"], []).append({
            "group": row["group_id"], "kind": row["kind"], "codes": json.loads(row["codes"]),
            "peak": f"{row['peak']//60:02d}:{row['peak']%60:02d}", "amount": row["amount"],
        })
    rows = []
    prior = {}
    for day in days:
        current = shares.get(day, {})
        matched = [code for code in primary if code in current and code in prior]
        changes = {code: current[code] - prior[code] for code in matched}
        flagged = [code for code, change in changes.items() if abs(change) >= prior[code] * .35]
        coverage = len(changes) - len(flagged)
        reported = sum(code in current for code in primary)
        if day >= first:
            rows.append({
                "day": day, "reported": reported, "coverage": coverage, "flagged": flagged,
                "net": sum(changes.values()) if coverage == len(primary) else None,
                "total": sum(current[code] for code in primary) if reported == len(primary) else None,
                "events": events.get(day, []),
            })
        prior = current
    return rows
