"""真实临时账本和时钟验证一夜：叫醒不续杯，硬撑不能回血。"""
import importlib
import json
from pathlib import Path
import tempfile
import unittest

from organs.heart import energy, sleep


class RestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rest = importlib.import_module('organs.heart.rest')

    def at(self, clock, day=7):
        return f'2026-11-{day:02d}T{clock}:00+09:00'

    def advance(self, clock, day=7, **kwargs):
        return self.rest.advance(self.root, self.at(clock, day), **kwargs)

    def test_group_or_unfamiliar_does_not_wake_and_reads_do_not_create_history(self):
        self.assertEqual('untracked', self.rest.state(self.root)['phase'])
        self.assertEqual([], list(self.root.iterdir()))
        self.assertEqual('asleep', self.advance('23:00')['phase'])
        before = (self.root/'rest.json').read_bytes()
        self.assertEqual('asleep', self.advance('23:01', can_wake=False)['phase'])
        self.assertEqual(before, (self.root/'rest.json').read_bytes())
        self.assertEqual([], sleep.overview(self.root)['nights'])

    def test_short_wake_does_not_extend_then_locks_until_morning_across_reload(self):
        self.advance('23:00')
        hold = self.advance('23:05', can_wake=True)
        self.assertEqual('holding', hold['phase'])
        self.assertEqual(self.at('23:20'), hold['hold_until'])
        self.assertEqual(self.at('08:00', 8), hold['wake_at'])
        self.assertEqual(hold, self.advance('23:19', can_wake=True))
        self.assertEqual('deep', self.advance('23:20', can_wake=True)['phase'])
        self.rest = importlib.reload(self.rest)
        self.assertEqual('deep', self.advance('07:59', 8, can_wake=True)['phase'])
        self.assertEqual('awake', self.advance('08:00', 8, can_wake=True)['phase'])
        nights = sleep.overview(self.root)['nights']
        self.assertEqual(1, len(nights))
        self.assertEqual(525, nights[0]['duration_min'])
        self.advance('08:01', 8)
        self.assertEqual(1, len(sleep.overview(self.root)['nights']))

    def test_holding_uses_awake_energy_then_deep_sleep_recovers(self):
        self.advance('23:00')
        self.advance('23:05', can_wake=True)
        value = energy.read(self.root, self.at('23:05'))['value']
        later = energy.read(self.root, self.at('23:15'))['value']
        self.assertAlmostEqual(value - 4/6, later, delta=.11)
        self.advance('23:20')
        deep = energy.read(self.root, self.at('23:20'))['value']
        self.assertGreater(energy.read(self.root, self.at('23:30'))['value'], deep)

    def test_current_awake_duration_follows_rest_without_counting_sleep(self):
        self.advance('08:00')
        checkpoints = [
            ('22:00', 7, False, 840),
            ('23:00', 7, False, 0),
            ('23:05', 7, True, 0),
            ('23:15', 7, False, 10),
            ('23:20', 7, False, 0),
            ('07:59', 8, False, 0),
            ('08:00', 8, False, 0),
            ('08:30', 8, False, 30),
        ]
        for clock, day, can_wake, expected in checkpoints:
            with self.subTest(clock=clock, day=day):
                self.advance(clock, day, can_wake=can_wake)
                before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
                at = self.at(clock, day)
                self.assertEqual(expected, sleep.awake_minutes(self.root, at))
                self.assertEqual(expected, sleep.overview(self.root, at)['awake_minutes'])
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertEqual(525, sleep.overview(self.root, at)['nights'][-1]['duration_min'])

    def test_transition_settles_even_when_elapsed_hours_round_to_zero(self):
        self.rest.advance(self.root, '2026-11-07T23:00:30+09:00')
        before = json.loads((self.root/'energy.json').read_text())['value']
        self.rest.advance(self.root, '2026-11-07T23:00:45+09:00', can_wake=True)
        current = json.loads((self.root/'energy.json').read_text())
        self.assertEqual('2026-11-07T23:00:45+09:00', current['updated_at'])
        self.assertAlmostEqual(before + .25, current['value'])

    def test_starting_during_rest_does_not_invent_earlier_sleep(self):
        self.advance('03:00', 8)
        self.advance('08:00', 8)
        night = sleep.overview(self.root)['nights'][0]
        self.assertEqual(self.at('03:00', 8), night['asleep_at'])
        self.assertEqual(300, night['duration_min'])

    def test_holding_restart_and_early_panel_read_use_saved_deadlines(self):
        self.advance('23:00')
        self.advance('23:05', can_wake=True)
        self.rest = importlib.reload(self.rest)
        # 页面可先于日子线程读取精力，不得把整个晚上结算为硬撑。
        self.assertEqual(100, energy.read(self.root, self.at('08:00', 8))['value'])
        self.assertEqual('awake', self.advance('09:00', 8)['phase'])
        night = sleep.overview(self.root)['nights'][0]
        self.assertEqual(525, night['duration_min'])
        self.assertEqual(self.at('08:00', 8), night['awake_at'])
        self.assertEqual(96, energy.read(self.root, self.at('09:00', 8))['value'])

    def test_first_close_message_can_wake_without_waiting_for_a_tick(self):
        self.advance('22:59')
        self.assertEqual('holding', self.advance('23:00', can_wake=True)['phase'])

    def test_short_night_drift_does_not_put_just_awakened_bot_back_to_sleep(self):
        self.advance('03:00', 8)
        self.advance('08:00', 8)
        self.assertEqual('awake', self.advance('08:01', 8)['phase'])

    def test_morning_before_hold_expiry_ends_night_and_energy_zero_ends_hold(self):
        self.advance('23:00')
        self.advance('07:55', 8, can_wake=True)
        self.assertEqual('awake', self.advance('08:00', 8)['phase'])
        self.assertEqual(535, sleep.overview(self.root)['nights'][0]['duration_min'])
        self.advance('23:00', 8)
        self.advance('23:05', 8, can_wake=True)
        p = self.root/'energy.json'
        data = json.loads(p.read_text()); data['value'] = 0; p.write_text(json.dumps(data))
        self.assertEqual('deep', self.advance('23:06', 8, can_wake=True)['phase'])


class SleepNoticeTest(unittest.TestCase):
    def test_receipts_survive_restart_and_expire_on_next_sleep(self):
        from tempfile import TemporaryDirectory
        from organs.heart import rest
        with TemporaryDirectory() as root:
            wake = '2026-11-09T08:00:00+09:00'
            self.assertFalse(rest.notice_sent('one',wake,root))
            rest.notice_sent('one',wake,root,mark=True)
            self.assertTrue(rest.notice_sent('one',wake,root))
            self.assertFalse(rest.notice_sent('two',wake,root))
            self.assertFalse(rest.notice_sent('one','2026-11-10T08:00:00+09:00',root))
