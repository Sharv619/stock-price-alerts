"""Background scheduler: checks all alerts every 60 seconds."""

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from app.price_checker import check_all_alerts

load_dotenv()

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

scheduler = BackgroundScheduler()
scheduler.add_job(
    check_all_alerts,
    "interval",
    seconds=CHECK_INTERVAL_SECONDS,
    id="price_check",
    max_instances=1,
    coalesce=True,
)


def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        logger.info(
            "Scheduler started — checking prices every %ds", CHECK_INTERVAL_SECONDS
        )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def is_running():
    return scheduler.running
