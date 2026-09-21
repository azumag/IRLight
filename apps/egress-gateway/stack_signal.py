from __future__ import annotations

import faulthandler
import signal
import sys
from typing import Callable, TextIO


STACK_SIGNAL_NAME = "SIGUSR2"


def install_stack_signal_handler(
    *,
    register: Callable[..., None] = faulthandler.register,
    output: TextIO | None = None,
) -> bool:
    """Install a secret-safe all-thread traceback signal handler.

    ``faulthandler`` writes stack metadata only (file, function and line), not
    Python local variables. Returning False instead of raising keeps this
    diagnostic optional and prevents it from affecting the media path.
    """

    signum = getattr(signal, STACK_SIGNAL_NAME, None)
    if signum is None:
        return False
    try:
        register(
            signum,
            file=output if output is not None else sys.stderr,
            all_threads=True,
            chain=False,
        )
    except Exception:
        return False
    return True
