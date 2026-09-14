"""Entrypoint: MQ consume (legacy) and/or HTTP claim agent (WORK_QUEUE_MODE=claim)."""

from __future__ import annotations

import logging
import sys

from app.work_agent import run_forever as run_claim_agent
from app.work_agent import work_queue_mode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mq_consumer")


def _run_mq() -> None:
    from openfarm_common.mq import connection_label, consume_forever
    from openfarm_common.settings import settings

    from app.handler import handle_task_message

    if not settings.cloudamqp_url:
        logger.error("CLOUDAMQP_URL is required for mq_consumer legacy mode")
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
    if mode == "claim":
        run_claim_agent()
        return
    # legacy (default) and dual: keep CloudAMQP consumer.
    # dual on download would double-run if claim also polled — claim is API-side only
    # until cutover; enable claim mode explicitly on download host when ready.
    _run_mq()


if __name__ == "__main__":
    main()
