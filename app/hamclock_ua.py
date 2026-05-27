"""
hamclock_ua.py — Parse HamClock User-Agent strings in a uWSGI app.

HamClock UA format:  HamClock-<platform>/<version>
Examples:
    HamClock-linux/4.22
    HamClock-linux/3.10
    ESPHamClock/3.10
    Mozilla/5.0 ...          (not HamClock)
"""

import re
from typing import Optional
from dataclasses import dataclass


# Matches: HamClock-<platform>/<v> or ESPHamClock/<v>
_UA_PATTERN = re.compile(
    r'^(?:HamClock-(?P<platform>\w+)|ESPHamClock)/(?P<major>\d+)\.(?P<minor>\d+)',
    re.IGNORECASE
)


@dataclass
class HamClockUA:
    is_hamclock: bool
    raw: str
    platform: Optional[str] = None
    major: Optional[int] = None
    minor: Optional[int] = None

    @property
    def version(self) -> Optional[str]:
        """Version as a string, e.g. '4.22'"""
        if self.major is not None and self.minor is not None:
            return f"{self.major}.{self.minor}"
        return None

    @property
    def version_tuple(self) -> Optional[tuple]:
        """Version as a comparable tuple, e.g. (4, 22)"""
        if self.major is not None and self.minor is not None:
            return (self.major, self.minor)
        return None

    def is_version_gte(self, major: int, minor: int) -> bool:
        """True if HamClock version >= (major, minor)"""
        if not self.is_hamclock or self.version_tuple is None:
            return False
        return self.version_tuple >= (major, minor)
        
    def is_version_lt(self, major: int, minor: int) -> bool:
        """True if HamClock version < (major, minor)"""
        if not self.is_hamclock or self.version_tuple is None:
            return False
        return self.version_tuple < (major, minor)

    def __str__(self):
        if not self.is_hamclock:
            return f"HamClockUA(not HamClock, raw={self.raw!r})"
        return f"HamClockUA(platform={self.platform}, version={self.version})"


def parse_ua(environ: dict) -> HamClockUA:
    """
    Parse the User-Agent from a uWSGI environ dict.

    Usage:
        ua = parse_ua(environ)
        if ua.is_hamclock:
            print(ua.version)        # '4.22'
            print(ua.version_tuple)  # (4, 22)
    """
    return parse_ua_string(environ.get('HTTP_USER_AGENT', ''))


def parse_ua_string(ua_string: str) -> HamClockUA:
    """Parse a raw UA string directly (useful for testing)."""
    raw = ua_string.strip()
    match = _UA_PATTERN.match(raw)
    if not match:
        return HamClockUA(is_hamclock=False, raw=raw)
    return HamClockUA(
        is_hamclock=True,
        raw=raw,
        platform=match.group('platform').lower() if match.group('platform') else 'ESPHamClock',
        major=int(match.group('major')),
        minor=int(match.group('minor')),
    )


# ------------------------------------------------------------------ #
#  Quick self-test  (python hamclock_ua.py)                          #
# ------------------------------------------------------------------ #
if __name__ == '__main__':
    tests = [
        "HamClock-linux/4.22",
        "HamClock-linux/3.10",
        "ESPHamClock/3.10",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "",
    ]
    for ua in tests:
        parsed = parse_ua_string(ua)
        print(parsed)
        if parsed.is_hamclock:
            print(f"  version       : {parsed.version}")
            print(f"  version_tuple : {parsed.version_tuple}")
            print(f"  >= 4.22?      : {parsed.is_version_gte(4, 22)}")
            print(f"  >= 3.10?      : {parsed.is_version_gte(3, 10)}")
            print(f"  <  4.22?      : {parsed.is_version_lt(4, 22)}")
        # test parse_ua() with simulated uWSGI environ dicts
    print("\n--- environ dict tests ---")
    for ua in tests:
        environ = {'HTTP_USER_AGENT': ua}
        parsed = parse_ua(environ)
        print(parsed)
        if parsed.is_hamclock:
            print(f"  version       : {parsed.version}")
            print(f"  version_tuple : {parsed.version_tuple}")
            print(f"  >= 4.22?      : {parsed.is_version_gte(4, 22)}")
            print(f"  >= 3.10?      : {parsed.is_version_gte(3, 10)}")
            print(f"  <  4.22?      : {parsed.is_version_lt(4, 22)}")

    # test missing key (simulates no UA header sent)
    print("\n--- environ dict test (no UA key) ---")
    parsed = parse_ua({})
    print(parsed)
