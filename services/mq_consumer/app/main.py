"""Entrypoint: MQ consume (legacy/dual) or HTTP claim agent (WORK_QUEUE_MODE=claim)."""

from __future__ import annotations

import logging
import sys

from openfarm_common.trace import install_stdlib_trace_log_record

from app.work_agent import run_forever as run_claim_agent
from app.work_agent import should_run_claim_agent, work_queue_mode

install_stdlib_trace_log_record()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mq_consumer")


def _run_mq() -> None:
    from openfarm_common.mq import connection_label, consume_forever
    from openfarm_common.settings import settings

    from app.handler import handle_task_message

    if not settings.cloudamqp_url:
        logger.error("CLOUDAMQP_URL is required for mq_consumer legacy/dual mode")
        sys.exit(1)
    logger.info(
        "starting mq_consumer broker=%s download_queue=%s",
        connection_label(),
        settings.cloudamqp_download_queue,
    )
    consume_forever(
        settings.cloudamqp_download_queue,
        handle_task_message,
        prefetch=1,
    )


def main() -> None:
    mode = work_queue_mode()
    logger.info("WORK_QUEUE_MODE=%s", mode)
    if should_run_claim_agent():
        # Optional: mq_consumer as claim agent only when WORK_QUEUE_MODE=claim.
        run_claim_agent()
        return
    if mode == "dual":
        # Harden: dual on API fills work_items + MQ; download must not also claim.
        logger.info(
            "dual mode: MQ consumer only (claim agent disabled to prevent double-dispatch)"
        )
    _run_mq()


if __name__ == "__main__":
    main()
