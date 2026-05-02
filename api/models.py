"""
Organized API models.

These models provide a local synchronization layer for the
Organized PWA application, enabling offline-first congregation
management with server-side persistence.
"""
from django.conf import settings
from django.db import models


class Congregation(models.Model):
    """A congregation managed through the Organized app."""

    name = models.CharField(max_length=255)
    number = models.CharField(max_length=20, unique=True, blank=True, null=True)
    country = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class UserProfile(models.Model):
    """Extended profile linking Django user to a congregation."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organized_profile",
    )
    congregation = models.ForeignKey(
        Congregation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
    )
    role = models.CharField(
        max_length=50,
        choices=[
            ("admin", "Administrator"),
            ("coordinator", "Coordinator"),
            ("publisher", "Publisher"),
            ("viewer", "Viewer"),
        ],
        default="viewer",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class SyncRecord(models.Model):
    """
    Tracks data synchronization between the Organized PWA
    and the backend, enabling conflict-free offline-first updates.
    """

    congregation = models.ForeignKey(
        Congregation,
        on_delete=models.CASCADE,
        related_name="sync_records",
    )
    record_type = models.CharField(max_length=100, db_index=True)
    record_id = models.CharField(max_length=255)
    data = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    last_synced_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        unique_together = ["congregation", "record_type", "record_id"]

    def __str__(self):
        return f"{self.record_type}:{self.record_id} v{self.version}"
