"""Celery tasks for the Organized backend."""
from celery import shared_task
from django.conf import settings
import logging
import requests

logger = logging.getLogger(__name__)


@shared_task
def notify_congregation_update(congregation_id, record_type, action):
    """
    Notify RapidPro when a congregation record changes,
    enabling automated communication workflows.
    """
    if not settings.RAPIDPRO_API_TOKEN:
        logger.debug("RAPIDPRO_API_TOKEN not set, skipping notification")
        return

    flow_uuid = getattr(settings, "ORGANIZED_NOTIFICATION_FLOW_UUID", "")
    if not flow_uuid:
        return

    try:
        response = requests.post(
            f"{settings.RAPIDPRO_URL}/api/v2/flow_starts.json",
            headers={
                "Authorization": f"Token {settings.RAPIDPRO_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "flow": flow_uuid,
                "extra": {
                    "congregation_id": str(congregation_id),
                    "record_type": record_type,
                    "action": action,
                },
            },
            timeout=10,
        )
        response.raise_for_status()
        logger.info("RapidPro notification sent for %s:%s", record_type, action)
    except requests.RequestException as exc:
        logger.warning("Failed to notify RapidPro: %s", exc)
