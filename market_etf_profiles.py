"""Comparable ETF turnover calculated from the same complete five-minute intervals."""
from collections import defaultdict
from datetime import date, timedelta
import math
from statistics import median

# Omit the first morning bar, which can include opening auction prints.
SESSION_ENDS = tuple(range(580, 691, 5)) + tuple(range(785, 896, 5))
SPARK_ENDS = set(SESSION_ENDS) - {785}


def aligned_windows(db, start_day, end_day, window):
    return db.execute("""
        SELECT day, code, (minute+4)/5*5 AS ending, sum(amount) AS amount,
               max(CASE WHEN minute % 5=0 THEN price END) AS price
        FROM bars WHERE span=1 AND day BETWEEN ? AND ? AND minute<=?
          AND (minute BETWEEN 576 AND 690 OR minute BETWEEN 781 AND 895)
        GROUP BY day,code,ending HAVING count(*)=5
    """, (start_day, end_day, window))


def archive_minutes(db, first, last):
    rows = [(5, r["day"], r["ending"], r["code"], r["amount"], r["price"], "tencent_5m_archive")
            for r in aligned_windows(db, first, last, 895)]
    db.executemany("INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?)", rows)


class TurnoverProfiles:
    def __init__(self, db, day, minute, *, minimum, limit):
        self.minimum = minimum
        self.minute = minute
        self.ends = {end for end in SESSION_ENDS if end <= minute}
        self.current = defaultdict(dict)
        self.history = defaultdict(lambda: defaultdict(dict))
        oldest = (date.fromisoformat(day) - timedelta(days=140)).isoformat()
        for row in db.execute("""
            SELECT day,code,minute,amount FROM bars
            WHERE span=5 AND day BETWEEN ? AND ? ORDER BY day DESC
        """, (oldest, day)):
            if row["day"] == day:
                if row["minute"] in self.ends:
                    self.current[row["code"]][row["minute"]] = row["amount"]
            elif row["day"] in self.history or len(self.history) < limit:
                self.history[row["day"]][row["code"]][row["minute"]] = row["amount"]
        for row in aligned_windows(db, day, day, minute):
            self.current[row["code"]][row["ending"]] = row["amount"]

    def common(self, codes):
        return self.ends.intersection(*(self.current.get(code, {}) for code in codes))

    def matching_sessions(self, codes, ends):
        return [session for session in self.history.values()
                if all(ends <= session.get(code, {}).keys() for code in codes)]

    def measure(self, codes, ends=None, sessions=None):
        ends = self.common(codes) if ends is None else ends
        if not codes or len(ends) < max(2, math.ceil(.8 * len(self.ends))):
            return {}
        if sessions is None:
            sessions = self.matching_sessions(codes, ends)
        observations = [sum(session[code][end] for code in codes for end in ends) for session in sessions]
        baseline = median(observations) if len(observations) >= self.minimum else None
        total = sum(self.current[code][end] for code in codes for end in ends)
        return {
            "multiple": round(total / baseline, 2) if baseline else None,
            "baseline": baseline, "comparableAmount": total, "baselineDays": len(observations),
            "comparableThrough": f"{max(ends)//60:02d}:{max(ends)%60:02d}",
            "matchedMinutes": len(ends) * 5,
        }

    def spark(self, code):
        days = [(day, sum(buckets[end] for end in SPARK_ENDS) if SPARK_ENDS <= buckets.keys() else None)
                for day, session in list(self.history.items())[:30] for buckets in (session.get(code, {}),)]
        return list(reversed(days)) if any(amount is not None for _, amount in days) else []
