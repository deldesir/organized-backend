"""Celery application for organized_backend."""
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "organized_backend.settings")

app = Celery("organized_backend")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
