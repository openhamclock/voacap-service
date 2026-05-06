# cancellable_run.py - subprocess.run with client-disconnect cancellation.
#
# Copyright (C) 2026 Open HamClock Backend (OHB) Contributors
# License: GNU Affero General Public License v3.0 (AGPLv3)
# See LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
#
# Why this exists:
#   When HamClock's client gives up on a request (its 2s fetch timeout fires),
#   the chain lighttpd -> Perl LWP -> nginx -> uWSGI -> Python -> voacapl has
#   no way to propagate that abandonment downward. Python's subprocess.run
#   blocks until voacapl finishes or its own timeout fires (30s / 120s),
#   keeping the uWSGI worker pinned and burning CPU on output nobody will
#   read. Under retry storms this saturates the worker pool within seconds.
#
#   This module replaces subprocess.run with a poll-loop variant that
#   periodically checks the WSGI client socket and SIGKILLs the subprocess
#   (and its process group) the moment the client disappears. The uWSGI
#   worker is freed in milliseconds rather than waiting out the timeout.

import errno
import logging
import os
import select
import signal
import socket
import subprocess
import time
from typing import Optional, Sequence

log = logging.getLogger("voacap_service.cancel")


class ClientDisconnected(Exception):
    """Raised when the upstream client closed the connection mid-request."""


def _get_client_fd(environ) -> Optional[int]:
    """Return the raw socket fd for the inbound client, or None if unavailable.

    Under uWSGI the `uwsgi` Python module exposes connection_fd(); under any
    other WSGI server we have no portable way to get the socket, so we degrade
    to "no early-abort detection" and rely on the existing timeout."""
    if environ is None:
        return None
    try:
        import uwsgi  # type: ignore
        fd = uwsgi.connection_fd()
        return fd if fd is not None and fd >= 0 else None
    except Exception:
        return None


def _client_gone(fd: int) -> bool:
    """Non-blocking check: has the peer closed the connection?

    Uses select() to see if the fd is readable, then MSG_PEEK to look at any
    pending data without consuming it. If the fd is readable and peeks empty,
    the peer has performed an orderly close (EOF). Errors other than EAGAIN
    are also treated as "gone"."""
    try:
        readable, _, errored = select.select([fd], [], [fd], 0)
    except (OSError, ValueError):
        # fd was closed under us, or otherwise invalid -> client is gone.
        return True

    if errored:
        return True
    if not readable:
        return False

    # fd is readable. Either there's a request body (we don't expect one for
    # GETs) or the peer closed. Peek without consuming.
    try:
        data = socket.socket(fileno=fd).recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
    except BlockingIOError:
        return False
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return False
        return True
    # Empty peek == orderly close.
    return len(data) == 0


def run_cancellable(
    cmd: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout: float = 120.0,
    environ: Optional[dict] = None,
    poll_interval: float = 0.25,
    capture_output: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run `cmd` like subprocess.run, but kill it early if the WSGI client
    disconnects.

    Returns a subprocess.CompletedProcess on normal exit (zero or non-zero rc).
    Raises subprocess.TimeoutExpired if `timeout` elapses before the process
    finishes, after killing the process group.
    Raises ClientDisconnected if the upstream client went away, after killing
    the process group. Caller should treat this like a 499 / no-op and clean
    up any temp files.

    The subprocess is started in its own session (`start_new_session=True`) so
    that os.killpg() reaches voacapl plus any helpers it spawned."""
    client_fd = _get_client_fd(environ)
    if client_fd is None:
        # No way to observe the client -> fall back to plain subprocess.run.
        # Same semantics as before this patch landed.
        return subprocess.run(
            cmd, cwd=cwd, timeout=timeout,
            capture_output=capture_output, text=text,
        )

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    deadline = time.monotonic() + timeout

    while True:
        try:
            stdout, stderr = proc.communicate(timeout=poll_interval)
            return subprocess.CompletedProcess(
                args=cmd, returncode=proc.returncode,
                stdout=stdout, stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            pass

        if time.monotonic() > deadline:
            log.warning("subprocess timeout after %.1fs, killing pgid=%d", timeout, pgid)
            _kill_pg(pgid)
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            raise subprocess.TimeoutExpired(cmd, timeout)

        if _client_gone(client_fd):
            log.info("client disconnected, killing pgid=%d", pgid)
            _kill_pg(pgid)
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            raise ClientDisconnected()


def _kill_pg(pgid: int) -> None:
    """Send SIGKILL to a process group, ignoring 'already gone' errors."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as e:
        log.warning("killpg(%d) failed: %s", pgid, e)
