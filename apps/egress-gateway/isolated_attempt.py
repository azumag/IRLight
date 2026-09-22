from __future__ import annotations

import logging
import math
import multiprocessing
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from attempt_supervisor import (
    AttemptSupervisorError,
    ChildAttemptResult,
    InvalidAttemptResult,
    parse_child_attempt_result,
    reap_child,
)
from destination_guard import (
    DestinationGuardError,
    read_verified_peer_ip,
    validate_destination_runtime,
)
from rtmp_sink import destination_url_for_sink
from secret_inputs import read_destination_url, read_input_uri


LOG = logging.getLogger("irlight.egress.isolated-attempt")

CANARY_ENV = "EGRESS_LEGACY_PROCESS_ISOLATION_CANARY"
TEARDOWN_TIMEOUT_ENV = "EGRESS_ISOLATED_TEARDOWN_TIMEOUT_SECONDS"
TERMINATE_TIMEOUT_ENV = "EGRESS_ISOLATED_TERMINATE_TIMEOUT_SECONDS"
KILL_TIMEOUT_ENV = "EGRESS_ISOLATED_KILL_TIMEOUT_SECONDS"

_DEFAULT_TEARDOWN_TIMEOUT_SECONDS = 8.0
_DEFAULT_TERMINATE_TIMEOUT_SECONDS = 2.0
_DEFAULT_KILL_TIMEOUT_SECONDS = 2.0
_PARENT_POLL_SECONDS = 0.1
_NATURAL_REAP_SECONDS = 0.5


class InvalidChildMessage(ValueError):
    """Raised when an isolated attempt emits an unexpected IPC message."""


class MessageConnection(Protocol):
    def poll(self, timeout: float = 0.0) -> bool: ...

    def recv(self) -> object: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ChildProcessConfig:
    """Non-secret configuration safe to serialize to the spawned child."""

    destination_file: str
    sink_factory: str
    connect_timeout_seconds: float
    status_heartbeat_seconds: float


@dataclass(frozen=True)
class ChildMessage:
    kind: str
    rendered_buffers: int | None = None
    result: ChildAttemptResult | None = None


def legacy_isolation_enabled(flag: object, sink_factory: str) -> bool:
    """Enable only the explicit legacy canary; rtmp2sink stays untouched."""

    return flag == "1" and sink_factory != "rtmp2sink"


def _finite_number(value: object, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number")
    return parsed


def _nonnegative_timeout(value: object, *, name: str) -> float:
    parsed = _finite_number(value, name=name)
    if parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return parsed


def _legacy_nonnegative_timer(value: object, *, name: str) -> float:
    """Preserve EgressAttempt's historical negative-to-zero timer semantics."""

    return max(0.0, _finite_number(value, name=name))


def _env_timeout(name: str, default: float) -> float:
    raw = os.getenv(name)
    return _nonnegative_timeout(default if raw is None else raw, name=name)


def parse_child_message(payload: object) -> ChildMessage:
    """Validate the deliberately small, secret-free child IPC envelope."""

    if not isinstance(payload, Mapping):
        raise InvalidChildMessage("child message must be an object")
    kind = payload.get("type")
    if kind in {"connected", "progress"}:
        if frozenset(payload.keys()) != {"type", "rendered_buffers"}:
            raise InvalidChildMessage("child progress fields are invalid")
        rendered = payload.get("rendered_buffers")
        if type(rendered) is not int or rendered < 0:
            raise InvalidChildMessage("child rendered_buffers is invalid")
        return ChildMessage(kind=str(kind), rendered_buffers=rendered)

    if kind in {"teardown", "result"}:
        if frozenset(payload.keys()) != {"type", "result"}:
            raise InvalidChildMessage("child result fields are invalid")
        try:
            result = parse_child_attempt_result(payload.get("result"))
        except (InvalidAttemptResult, TypeError) as exc:
            raise InvalidChildMessage("child result payload is invalid") from exc
        return ChildMessage(kind=str(kind), result=result)

    raise InvalidChildMessage("child message type is invalid")


def _result_payload(result: object) -> dict[str, object]:
    return {
        "reason_code": str(getattr(result, "reason_code", "LOCAL_PIPELINE_FAILED")),
        "connected_once": bool(getattr(result, "connected_once", False)),
        "rendered_buffers": int(getattr(result, "rendered_buffers", 0)),
        "terminal": bool(getattr(result, "terminal", False)),
        "error_domain": getattr(result, "error_domain", None),
        "error_code": getattr(result, "error_code", None),
    }


def _safe_send(
    connection: Any,
    payload: dict[str, object],
    lock: threading.Lock | None = None,
) -> None:
    try:
        if lock is None:
            connection.send(payload)
        else:
            with lock:
                connection.send(payload)
    except (BrokenPipeError, EOFError, OSError):
        # The parent may already have fenced the child. Never render payload or
        # exception text because a future implementation mistake could place a
        # credential-bearing value in either object.
        return


def _watch_for_teardown(
    attempt: object,
    connection: Any,
    send_lock: threading.Lock,
    done: threading.Event,
) -> None:
    """Emit a pre-teardown result snapshot if the GLib loop stops but run blocks."""

    saw_running = False
    initial_reason = str(getattr(attempt.result, "reason_code", ""))
    while not done.wait(0.01):
        try:
            running = bool(attempt.loop.is_running())
            poll_installed = getattr(attempt, "_poll_source_id", None) is not None
            reason_changed = (
                str(getattr(attempt.result, "reason_code", "")) != initial_reason
            )
        except Exception:
            return
        if running:
            saw_running = True
            continue
        if saw_running or (poll_installed and reason_changed):
            _safe_send(
                connection,
                {"type": "teardown", "result": _result_payload(attempt.result)},
                send_lock,
            )
            return


def _send_local_failure(connection: Any, send_lock: threading.Lock) -> None:
    _safe_send(
        connection,
        {
            "type": "result",
            "result": {
                "reason_code": "LOCAL_PIPELINE_FAILED",
                "connected_once": False,
                "rendered_buffers": 0,
                "terminal": True,
                "error_domain": None,
                "error_code": None,
            },
        },
        send_lock,
    )


def _child_attempt_main(
    connection: Any,
    stop_event: Any,
    config: ChildProcessConfig,
) -> None:
    """Run one GStreamer attempt in a fresh interpreter.

    Only non-secret configuration reaches this function through multiprocessing
    arguments. Credential-bearing input and destination values are re-read from
    the existing runtime secret sources inside the child.
    """

    send_lock = threading.Lock()
    done = threading.Event()
    monitor: threading.Thread | None = None
    try:
        # Import the GStreamer-heavy module only in the spawned interpreter.
        # This avoids forking after the parent has initialized Gst/GLib.
        import egress

        egress.Gst.init(None)
        input_uri = read_input_uri()
        destination_url = read_destination_url(Path(config.destination_file))
        destination_url = destination_url_for_sink(
            destination_url,
            sink_factory=config.sink_factory,
            librtmp_timeout_raw=os.getenv("EGRESS_LIBRTMP_SESSION_TIMEOUT_SECONDS"),
        )
        peer_file_raw = os.getenv(
            "EGRESS_VERIFIED_PEER_IP_FILE",
            "/run/irlight/egress-secrets/egress_verified_peer_ip",
        )
        peer_file = Path(peer_file_raw) if peer_file_raw else None
        expected_peer_ip = read_verified_peer_ip(peer_file)
        validate_destination_runtime(
            destination_url,
            expected_peer_ip=expected_peer_ip,
            allow_private_targets=os.getenv("EGRESS_ALLOW_PRIVATE_TARGETS", "") == "1",
        )

        attempt = egress.EgressAttempt(
            input_uri,
            destination_url,
            stop_event,
            connect_timeout_seconds=config.connect_timeout_seconds,
            status_heartbeat_seconds=config.status_heartbeat_seconds,
            sink_factory=config.sink_factory,
        )
        monitor = threading.Thread(
            target=_watch_for_teardown,
            args=(attempt, connection, send_lock, done),
            name="irlight-egress-teardown-monitor",
            daemon=True,
        )
        monitor.start()

        result = attempt.run(
            on_connected=lambda rendered: _safe_send(
                connection,
                {"type": "connected", "rendered_buffers": int(rendered)},
                send_lock,
            ),
            on_progress=lambda rendered: _safe_send(
                connection,
                {"type": "progress", "rendered_buffers": int(rendered)},
                send_lock,
            ),
        )
        done.set()
        monitor.join(timeout=0.2)
        _safe_send(
            connection,
            {"type": "result", "result": _result_payload(result)},
            send_lock,
        )
    except DestinationGuardError as exc:
        _safe_send(
            connection,
            {
                "type": "result",
                "result": {
                    "reason_code": exc.reason_code,
                    "connected_once": False,
                    "rendered_buffers": 0,
                    "terminal": exc.terminal,
                    "error_domain": None,
                    "error_code": None,
                },
            },
            send_lock,
        )
    except Exception:
        # Raw exception text can contain a credentialed sink URI. Collapse every
        # other child failure to one fixed local reason instead of serializing or
        # logging exception details.
        _send_local_failure(connection, send_lock)
    finally:
        done.set()
        if monitor is not None:
            monitor.join(timeout=0.2)
        try:
            connection.close()
        except OSError:
            pass


def _local_failure(
    *, connected_once: bool, rendered_buffers: int
) -> ChildAttemptResult:
    return ChildAttemptResult(
        reason_code="LOCAL_PIPELINE_FAILED",
        connected_once=connected_once,
        rendered_buffers=rendered_buffers,
        terminal=True,
    )


def supervise_attempt_child(
    process: Any,
    connection: MessageConnection,
    child_stop_event: Any,
    parent_stop_event: Any,
    *,
    connect_timeout_seconds: float,
    teardown_timeout_seconds: float,
    terminate_timeout_seconds: float,
    kill_timeout_seconds: float,
    on_connected: Callable[[int], None] | None = None,
    on_progress: Callable[[int], None] | None = None,
    poll_seconds: float = _PARENT_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> ChildAttemptResult:
    """Own one child until a validated result is received or the PID is fenced."""

    connect_timeout = _legacy_nonnegative_timer(
        connect_timeout_seconds, name="connect_timeout_seconds"
    )
    teardown_timeout = _nonnegative_timeout(
        teardown_timeout_seconds, name="teardown_timeout_seconds"
    )
    terminate_timeout = _nonnegative_timeout(
        terminate_timeout_seconds, name="terminate_timeout_seconds"
    )
    kill_timeout = _nonnegative_timeout(
        kill_timeout_seconds, name="kill_timeout_seconds"
    )
    poll_interval = _nonnegative_timeout(poll_seconds, name="poll_seconds")

    started_at = monotonic()
    startup_deadline = (
        started_at + connect_timeout + teardown_timeout
        if connect_timeout > 0
        else None
    )
    teardown_deadline: float | None = None
    stop_deadline: float | None = None
    pending_result: ChildAttemptResult | None = None
    connected_once = False
    rendered_buffers = 0

    def fence(*, natural_timeout: float) -> None:
        reap_child(
            process,
            natural_timeout_seconds=natural_timeout,
            terminate_timeout_seconds=terminate_timeout,
            kill_timeout_seconds=kill_timeout,
        )

    while True:
        now = monotonic()
        if parent_stop_event.is_set() and stop_deadline is None:
            child_stop_event.set()
            stop_deadline = now + teardown_timeout

        received = False
        try:
            received = connection.poll(poll_interval)
        except (EOFError, OSError):
            received = False

        if received:
            try:
                raw_message = connection.recv()
                message = parse_child_message(raw_message)
            except (EOFError, OSError, InvalidChildMessage):
                fence(natural_timeout=0.0)
                return _local_failure(
                    connected_once=connected_once,
                    rendered_buffers=rendered_buffers,
                )

            if message.kind in {"connected", "progress"}:
                assert message.rendered_buffers is not None
                rendered_buffers = max(rendered_buffers, message.rendered_buffers)
                if message.kind == "connected":
                    connected_once = True
                    startup_deadline = None
                    if on_connected is not None:
                        on_connected(rendered_buffers)
                elif on_progress is not None:
                    on_progress(rendered_buffers)
            else:
                assert message.result is not None
                pending_result = message.result
                connected_once = connected_once or pending_result.connected_once
                rendered_buffers = max(rendered_buffers, pending_result.rendered_buffers)
                if message.kind == "teardown":
                    teardown_deadline = monotonic() + teardown_timeout
                else:
                    fence(natural_timeout=_NATURAL_REAP_SECONDS)
                    return pending_result

        # Always evaluate deadlines after one IPC message. A misbehaving child
        # must not keep user-stop or teardown fencing alive forever by flooding
        # the parent with otherwise valid progress messages.
        now = monotonic()
        if stop_deadline is not None and now >= stop_deadline:
            fence(natural_timeout=0.0)
            return ChildAttemptResult(
                reason_code="STOPPED",
                connected_once=connected_once,
                rendered_buffers=rendered_buffers,
                terminal=False,
            )

        if teardown_deadline is not None and now >= teardown_deadline:
            fence(natural_timeout=0.0)
            return pending_result or _local_failure(
                connected_once=connected_once,
                rendered_buffers=rendered_buffers,
            )

        if startup_deadline is not None and now >= startup_deadline:
            fence(natural_timeout=0.0)
            return ChildAttemptResult(
                reason_code="TIMEOUT",
                connected_once=False,
                rendered_buffers=rendered_buffers,
                terminal=False,
            )

        if not process.is_alive():
            try:
                if connection.poll(0.0):
                    continue
            except (EOFError, OSError):
                pass
            process.join(0.0)
            return _local_failure(
                connected_once=connected_once,
                rendered_buffers=rendered_buffers,
            )


class IsolatedEgressAttempt:
    """Drop-in, opt-in parent-side wrapper for one legacy EgressAttempt."""

    def __init__(
        self,
        input_uri: str,
        destination_url: str,
        stop_event: Any,
        *,
        connect_timeout_seconds: float,
        status_heartbeat_seconds: float,
        sink_factory: str | None = None,
    ) -> None:
        # The parent already holds these values in the existing Gateway. Never
        # copy them into the spawned Process args; the child re-reads its secret
        # sources instead.
        del input_uri, destination_url
        self.stop_event = stop_event
        self.config = ChildProcessConfig(
            destination_file=os.getenv(
                "EGRESS_URL_FILE", "/run/irlight/egress-secrets/egress_url"
            ),
            sink_factory=str(sink_factory or "rtmpsink"),
            connect_timeout_seconds=_legacy_nonnegative_timer(
                connect_timeout_seconds, name="connect_timeout_seconds"
            ),
            status_heartbeat_seconds=_legacy_nonnegative_timer(
                status_heartbeat_seconds, name="status_heartbeat_seconds"
            ),
        )
        self.teardown_timeout_seconds = _env_timeout(
            TEARDOWN_TIMEOUT_ENV, _DEFAULT_TEARDOWN_TIMEOUT_SECONDS
        )
        self.terminate_timeout_seconds = _env_timeout(
            TERMINATE_TIMEOUT_ENV, _DEFAULT_TERMINATE_TIMEOUT_SECONDS
        )
        self.kill_timeout_seconds = _env_timeout(
            KILL_TIMEOUT_ENV, _DEFAULT_KILL_TIMEOUT_SECONDS
        )

    def run(
        self,
        *,
        on_connected: Callable[[int], None] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> Any:
        # spawn avoids forking a process after Gst.init()/GLib initialization.
        context = multiprocessing.get_context("spawn")
        recv_connection, send_connection = context.Pipe(duplex=False)
        child_stop_event = context.Event()
        process = context.Process(
            target=_child_attempt_main,
            args=(send_connection, child_stop_event, self.config),
            name="irlight-egress-attempt",
        )
        try:
            process.start()
        except Exception:
            recv_connection.close()
            send_connection.close()
            LOG.error("failed to start isolated egress attempt")
            from egress import AttemptResult

            return AttemptResult(
                "LOCAL_PIPELINE_FAILED", False, 0, terminal=True
            )

        send_connection.close()
        try:
            result = supervise_attempt_child(
                process,
                recv_connection,
                child_stop_event,
                self.stop_event,
                connect_timeout_seconds=self.config.connect_timeout_seconds,
                teardown_timeout_seconds=self.teardown_timeout_seconds,
                terminate_timeout_seconds=self.terminate_timeout_seconds,
                kill_timeout_seconds=self.kill_timeout_seconds,
                on_connected=on_connected,
                on_progress=on_progress,
            )
        except BaseException:
            child_stop_event.set()
            if process.is_alive():
                try:
                    reap_child(
                        process,
                        natural_timeout_seconds=0.0,
                        terminate_timeout_seconds=self.terminate_timeout_seconds,
                        kill_timeout_seconds=self.kill_timeout_seconds,
                    )
                except AttemptSupervisorError:
                    pass
            raise
        finally:
            recv_connection.close()

        from egress import AttemptResult

        return AttemptResult(
            result.reason_code,
            result.connected_once,
            result.rendered_buffers,
            terminal=result.terminal,
            error_domain=result.error_domain,
            error_code=result.error_code,
        )
