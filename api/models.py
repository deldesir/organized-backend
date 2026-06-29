"""Organized backend models.

Phase 1 adds CongUser (auth) and extends Congregation with settings.
Phase 2 will add CongBackupTable, UserBackupTable, Metadata, and
remove the legacy Publisher/MeetingSchedule/Assignment models.
"""

import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models


class Congregation(models.Model):
    """One per congregation. Multi-tenancy root."""
    cong_id = models.CharField(max_length=255, unique=True)
    country_code = models.CharField(max_length=10, default='', blank=True)
    cong_name = models.CharField(max_length=255)
    cong_number = models.CharField(max_length=20, default='', blank=True)
    cong_prefix = models.CharField(max_length=20, default='', blank=True)
    cong_settings = models.JSONField(default=dict, blank=True)
    outgoing_speakers = models.JSONField(default=dict, blank=True)
    incoming_reports = models.JSONField(default=list, blank=True)
    data = models.JSONField(default=dict, blank=True)  # legacy, kept for migration
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'organized_congregation'

    def __str__(self):
        return f"{self.cong_name} ({self.cong_id})"

    def get_settings_for_user(self, cong_user):
        """Return settings shaped for API response, respecting role."""
        s = self.cong_settings.copy() if self.cong_settings else {}
        master_key_roles = [
            'admin', 'coordinator', 'secretary', 'elder', 'service_overseer'
        ]
        needs_master_key = any(r in (cong_user.cong_role or []) for r in master_key_roles)

        return {
            'cong_access_code': s.get('cong_access_code', ''),
            'cong_master_key': s.get('cong_master_key') if needs_master_key else None,
            'data_sync': s.get('data_sync', {'value': True}),
            'cong_name': self.cong_name,
            'cong_prefix': self.cong_prefix,
            'cong_number': self.cong_number,
            'country_code': self.country_code,
        }


class CongUser(models.Model):
    """A user within a congregation.

    Maps to upstream's User class. auth_user links to Django's User
    for VIP users; pocket users have auth_user=None and use
    pocket_invitation_code for authentication.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    auth_user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='cong_profile'
    )
    congregation = models.ForeignKey(
        Congregation, on_delete=models.CASCADE, related_name='members'
    )
    firstname = models.CharField(max_length=100, default='')
    lastname = models.CharField(max_length=100, default='')
    user_local_uid = models.CharField(max_length=64, default='', blank=True)
    cong_role = ArrayField(
        models.CharField(max_length=50), default=list, blank=True
    )
    user_members_delegate = ArrayField(
        models.CharField(max_length=64), default=list, blank=True
    )
    pocket_invitation_code = models.CharField(
        max_length=100, null=True, blank=True, unique=True
    )
    sessions = models.JSONField(default=list, blank=True)
    settings_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'organized_cong_user'

    def __str__(self):
        return f"{self.firstname} {self.lastname} ({self.congregation.cong_name})"


class CongBackupTable(models.Model):
    """Opaque blob storage for congregation-scoped backup tables."""
    congregation = models.ForeignKey(
        Congregation, on_delete=models.CASCADE, related_name='backup_tables'
    )
    table_name = models.CharField(max_length=64)
    data = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'organized_cong_backup_table'
        unique_together = ['congregation', 'table_name']

    def __str__(self):
        return f"{self.congregation.cong_name}/{self.table_name}"


class UserBackupTable(models.Model):
    """Opaque blob storage for user-scoped backup tables."""
    cong_user = models.ForeignKey(
        CongUser, on_delete=models.CASCADE, related_name='backup_tables'
    )
    table_name = models.CharField(max_length=64)
    data = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'organized_user_backup_table'
        unique_together = ['cong_user', 'table_name']


class Metadata(models.Model):
    """Per-table metadata timestamps for incremental sync.

    Shared between congregation-level and user-level metadata.
    congregation is always set. cong_user is set for user-level metadata.
    """
    congregation = models.ForeignKey(
        Congregation, on_delete=models.CASCADE, related_name='metadata_entries'
    )
    cong_user = models.ForeignKey(
        CongUser, on_delete=models.CASCADE, null=True, blank=True,
        related_name='metadata_entries'
    )
    key = models.CharField(max_length=64)
    value = models.CharField(max_length=64, default='')

    class Meta:
        db_table = 'organized_metadata'
        unique_together = ['congregation', 'cong_user', 'key']


# === Legacy models (Phase 2 will remove these) ===

class Publisher(models.Model):
    congregation = models.ForeignKey(
        Congregation, on_delete=models.CASCADE, related_name='publishers'
    )
    person_uid = models.CharField(max_length=255, unique=True)
    display_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=50, blank=True, null=True)
    data = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.display_name


class MeetingSchedule(models.Model):
    congregation = models.ForeignKey(
        Congregation, on_delete=models.CASCADE, related_name='schedules'
    )
    week_of = models.CharField(max_length=50)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ('congregation', 'week_of')

    def __str__(self):
        return f"{self.congregation.cong_name} - {self.week_of}"


class Assignment(models.Model):
    schedule = models.ForeignKey(
        MeetingSchedule, on_delete=models.CASCADE, related_name='assignments'
    )
    publisher = models.ForeignKey(
        Publisher, on_delete=models.SET_NULL, null=True, related_name='assignments'
    )
    assignment_code = models.IntegerField()
    assignment_type = models.CharField(max_length=100)
    data = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return (
            f"{self.assignment_type} - "
            f"{self.publisher.display_name if self.publisher else 'Unassigned'}"
        )
