import struct
import unittest

from app.chronos.time_source import (_NTP_UNIX_DELTA, _PACKET, QuorumTimeSource, TimeSample,
                                     query_ntp)
from app.core.errors import TimeConsensusError


def _ntp_reply(request: bytes, unix_time: float, *, stratum=2, leap=0, mode=4, spoof=False) -> bytes:
    words = list(_PACKET.unpack(request)[4:])
    orig = (words[9], words[10])
    if spoof:
        orig = (1, 2)
    sec = int(unix_time) + _NTP_UNIX_DELTA
    frac = int((unix_time % 1) * 2**32)
    out = [0] * 11
    out[5], out[6] = orig
    out[7], out[8] = sec, frac
    out[9], out[10] = sec, frac
    return _PACKET.pack((leap << 6) | (4 << 3) | mode, stratum, 0, 0, *out)


class NtpQueryTests(unittest.TestCase):
    def _q(self, **kw):
        return query_ntp("srv", transport=lambda s, p, t: _ntp_reply(p, 1_900_000_000.25, **kw),
                         clock=lambda: 0.0)

    def test_valid_reply(self):
        s = self._q()
        self.assertAlmostEqual(s.unix_time, 1_900_000_000.25, places=3)

    def test_rejects_spoofed_originate(self):
        with self.assertRaisesRegex(ValueError, "originate"):
            self._q(spoof=True)

    def test_rejects_kiss_of_death_unsynced_and_bad_mode(self):
        for kw in ({"stratum": 0}, {"leap": 3}, {"mode": 3}, {"stratum": 16}):
            with self.assertRaises(ValueError):
                self._q(**kw)

    def test_rejects_short_packet(self):
        with self.assertRaises(ValueError):
            query_ntp("srv", transport=lambda s, p, t: b"\x00" * 10)


class QuorumTests(unittest.TestCase):
    @staticmethod
    def _src(times, **kw):
        table = {f"s{i}": t for i, t in enumerate(times)}

        def q(server):
            t = table[server]
            if isinstance(t, Exception):
                raise t
            return TimeSample(server, t, 0.01)
        return QuorumTimeSource(list(table), query=q, **kw)

    def test_median_of_agreeing_sources(self):
        now = self._src([1000.0, 1000.4, 1000.2, 1000.1]).now()
        self.assertAlmostEqual(now.timestamp(), 1000.15, places=2)

    def test_single_outlier_is_discarded(self):
        now = self._src([1000.0, 1000.1, 1000.2, 9_999_999.0]).now()
        self.assertLess(abs(now.timestamp() - 1000.1), 1.0)

    def test_quorum_failure_fails_closed(self):
        src = self._src([1000.0, TimeoutError(), TimeoutError(), TimeoutError()])
        with self.assertRaises(TimeConsensusError):
            src.now()

    def test_no_consensus_fails_closed(self):
        with self.assertRaises(TimeConsensusError):
            self._src([1000.0, 2000.0, 3000.0, 4000.0]).now()

    def test_config_guards(self):
        with self.assertRaises(ValueError):
            QuorumTimeSource(["a"], min_sources=1)


if __name__ == "__main__":
    unittest.main()
