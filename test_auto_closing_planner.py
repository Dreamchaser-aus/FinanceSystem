import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from auto_closing_planner import due_periods, period_for_instant
UTC=timezone.utc
MY=ZoneInfo('Asia/Kuala_Lumpur')

class AutoClosingPlannerTests(unittest.TestCase):
    def test_summer_malaysia_9pm_boundary(self):
        anchor=datetime(2026,10,26,13,tzinfo=UTC)
        due=due_periods('MRVIP9','bank-1',anchor,datetime(2026,10,27,13,tzinfo=UTC))
        self.assertEqual(len(due),1)
        self.assertEqual(due[0].end_utc.astimezone(MY).hour,21)
        self.assertEqual(due[0].status,'PENDING_VERIFICATION')
    def test_winter_malaysia_10pm_boundary(self):
        anchor=datetime(2026,6,1,14,tzinfo=UTC)
        due=due_periods('MRVIP9','bank-1',anchor,datetime(2026,6,2,14,tzinfo=UTC))
        self.assertEqual(due[0].end_utc.astimezone(MY).hour,22)
    def test_exact_boundary_is_new_period(self):
        instant=datetime(2026,10,27,13,tzinfo=UTC)
        start,end=period_for_instant(instant)
        self.assertEqual(start,instant)
        self.assertGreater(end,instant)
    def test_backlog_recovery(self):
        due=due_periods('p','b',datetime(2026,10,25,13,tzinfo=UTC),datetime(2026,10,28,13,tzinfo=UTC))
        self.assertEqual(len(due),3)
        self.assertEqual(due[0].end_utc,due[1].start_utc)
    def test_no_duplicate_at_same_instant(self):
        anchor=datetime(2026,10,27,13,tzinfo=UTC)
        self.assertEqual(due_periods('p','b',anchor,anchor),[])
    def test_missing_anchor_is_rejected(self):
        with self.assertRaises(Exception):
            due_periods('p','b',None,datetime(2026,10,27,13,tzinfo=UTC))
    def test_separate_platforms(self):
        anchor=datetime(2026,10,26,13,tzinfo=UTC)
        now=datetime(2026,10,27,13,tzinfo=UTC)
        a=due_periods('MRVIP9','b',anchor,now)
        b=due_periods('MONOAUD','b',datetime(2026,10,26,14,tzinfo=UTC),now,hour=1)
        self.assertEqual(len(a),1)
        self.assertEqual(len(b),0)

if __name__=='__main__': unittest.main()
