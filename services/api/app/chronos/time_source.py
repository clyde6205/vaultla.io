"""Trusted time for Chronos.

Threat: a client (or a compromised host clock) lies about "now" to open a vault early.
Defence: the server never trusts any client time, and does not trust a single NTP host
either. We query several independent servers, validate each response, require a quorum,
take the MEDIAN, and reject the reading if the sources disagree by more than a tolerance.
Callers must treat TimeConsensusError as "do not mature anything".

Pure standard library (SNTPv4 over UDP/123) so the core is dependency-free and testable.
For production hardening, prefer NTS-authenticated sources (RFC 8915) or Amazon Time Sync.
"""
from __future__ import annotations

import logging
import secrets
import socket
import statistics
import struct
import time as _time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence

from app.core.errors import TimeConsensusError

log = logging.getLogger("vaultla.chronos.time")

_NTP_UNIX_DELTA = 2_208_988_800  # seconds between 1900-01-01 and 1970-01-01
_PACKET = struct.Struct("!BBBb11I")


@dataclass(frozen=True, slots=True)
class TimeSample:
    server: str
    unix_time: float          # server time corrected for half the round trip
    round_trip: float


class TimeSource(Protocol):
    def now(self) -> datetime: ...


def _from_ntp(sec: int, frac: int) -> float:
    return (sec - _NTP_UNIX_DELTA) + frac / 2**32


Transport = Callable[[str, bytes, float], bytes]


def _udp_transport(server: str, payload: bytes, timeout: float) -> bytes:
    """Default transport. Resolves and queries over UDP; rejects packets from other peers."""
    family, socktype, proto, _, addr = socket.getaddrinfo(server, 123, type=socket.SOCK_DGRAM)[0]
    with socket.socket(family, socktype, proto) as s:
        s.settimeout(timeout)
        s.sendto(payload, addr)
        data, peer = s.recvfrom(512)
        if peer[0] != addr[0]:
            raise ValueError("response from unexpected peer")
        return data


def query_ntp(server: str, timeout: float = 2.0, *,
              transport: Transport = _udp_transport,
              clock: Callable[[], float] = _time.time) -> TimeSample:
    """Single SNTP exchange with full response validation.

    `clock` is only used to measure the local round trip; the returned time is derived
    from the server, never from the local wall clock.
    """
    nonce = secrets.randbits(64)                 # anti-spoof: must be echoed as 'originate'
    nsec, nfrac = (nonce >> 32) & 0xFFFFFFFF, nonce & 0xFFFFFFFF
    li_vn_mode = (0 << 6) | (4 << 3) | 3         # LI=0, VN=4, mode=3 (client)
    words = [0] * 11
    words[9], words[10] = nsec, nfrac            # our transmit timestamp = random nonce
    request = _PACKET.pack(li_vn_mode, 0, 0, 0, *words)

    t1 = clock()
    raw = transport(server, request, timeout)
    t4 = clock()

    if len(raw) < _PACKET.size:
        raise ValueError(f"{server}: short NTP packet ({len(raw)} bytes)")
    b0, stratum, _poll, _prec, *w = _PACKET.unpack(raw[: _PACKET.size])
    leap, version, mode = b0 >> 6, (b0 >> 3) & 7, b0 & 7
    if mode != 4:
        raise ValueError(f"{server}: unexpected mode {mode}")
    if version < 3:
        raise ValueError(f"{server}: unsupported version {version}")
    if leap == 3:
        raise ValueError(f"{server}: server clock unsynchronised")
    if stratum == 0:
        raise ValueError(f"{server}: kiss-of-death packet")
    if stratum > 15:
        raise ValueError(f"{server}: invalid stratum {stratum}")
    # After the 4 header bytes: w[0] root delay, w[1] root dispersion, w[2] refid,
    # w[3:5] reference ts, w[5:7] originate ts, w[7:9] receive ts, w[9:11] transmit ts.
    orig, rx, tx = (w[5], w[6]), (w[7], w[8]), (w[9], w[10])
    if orig != (nsec, nfrac):
        raise ValueError(f"{server}: originate timestamp mismatch (spoof/replay)")
    t_rx, t_tx = _from_ntp(*rx), _from_ntp(*tx)
    if tx == (0, 0) or rx == (0, 0):
        raise ValueError(f"{server}: zero timestamp")
    rtt = max((t4 - t1) - (t_tx - t_rx), 0.0)
    return TimeSample(server=server, unix_time=t_tx + rtt / 2.0, round_trip=rtt)


class QuorumTimeSource:
    """Median-of-N trusted time with quorum and spread checks."""

    def __init__(self, servers: Sequence[str], *, min_sources: int = 3,
                 max_spread_seconds: float = 2.0, timeout: float = 2.0,
                 query: Callable[[str], TimeSample] | None = None) -> None:
        if min_sources < 2:
            raise ValueError("min_sources must be >= 2 for a meaningful quorum")
        if len(servers) < min_sources:
            raise ValueError("fewer configured servers than min_sources")
        self._servers = tuple(servers)
        self._min = min_sources
        self._spread = max_spread_seconds
        self._query = query or (lambda s: query_ntp(s, timeout))

    def samples(self) -> list[TimeSample]:
        ok: list[TimeSample] = []
        with ThreadPoolExecutor(max_workers=len(self._servers)) as pool:
            futures = [(pool.submit(self._query, s), s) for s in self._servers]
            for fut, server in futures:
                try:
                    ok.append(fut.result())
                except Exception as exc:  # noqa: BLE001 - one bad source must not abort the rest
                    log.warning("ntp source %s failed: %s", server, exc)
        return ok

    def now(self) -> datetime:
        ok = self.samples()
        if len(ok) < self._min:
            raise TimeConsensusError(
                f"Only {len(ok)} of {len(self._servers)} time sources answered; need {self._min}.")
        times = sorted(s.unix_time for s in ok)
        if times[-1] - times[0] > self._spread:
            median = statistics.median(times)
            inliers = [t for t in times if abs(t - median) <= self._spread]
            if len(inliers) < self._min:
                raise TimeConsensusError(
                    f"Time sources disagree by {times[-1] - times[0]:.3f}s (limit {self._spread}s).")
            times = inliers
        return datetime.fromtimestamp(statistics.median(times), tz=timezone.utc)
