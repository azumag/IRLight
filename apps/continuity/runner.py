from __future__ import annotations

import logging
import os
import time

from continuity import ContinuityPipeline, atomic_write_json
from standby_asset import (
    NODE_DEFAULT_IMAGE_PATH,
    gst_standby_source,
    public_standby_status,
    resolve_standby_asset,
)


LOG = logging.getLogger("irlight.continuity")
_SYNTHETIC_SOURCE = "videotestsrc name=standby_video is-live=true pattern=black !"


class StandbyAwareContinuityPipeline(ContinuityPipeline):
    def __init__(self) -> None:
        self.standby_selection = resolve_standby_asset(
            os.getenv("STANDBY_IMAGE_PATH"),
            os.getenv("STANDBY_FALLBACK_IMAGE_PATH", NODE_DEFAULT_IMAGE_PATH),
        )
        super().__init__()
        self.standby_status_path = self.state_dir / "standby.json"

    def _output_description(self, egress_literal: str, key_int: int) -> str:
        description = super()._output_description(egress_literal, key_int)
        replacement = gst_standby_source(self.standby_selection)
        if replacement == _SYNTHETIC_SOURCE:
            return description
        if _SYNTHETIC_SOURCE not in description:
            raise RuntimeError("continuity standby source template changed unexpectedly")
        return description.replace(_SYNTHETIC_SOURCE, replacement)

    def _write_standby_status(self) -> None:
        atomic_write_json(
            self.standby_status_path,
            {
                **public_standby_status(self.standby_selection),
                "selected_at": time.time(),
            },
        )

    def run(self) -> None:
        self._write_standby_status()
        info = public_standby_status(self.standby_selection)
        LOG.info(
            "standby source selected source=%s fallback_reason=%s custom_configured=%s",
            info["source"],
            info["fallback_reason"],
            info["custom_configured"],
        )
        super().run()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    StandbyAwareContinuityPipeline().run()


if __name__ == "__main__":
    main()
