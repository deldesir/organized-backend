"""Backup sync service.

Core logic for retrieveUserBackup (GET) and saveUserBackup (POST).
Replicates sws2apps-api/src/v3/controllers/users_controller.ts.
"""

import logging

from api.services.roles import get_user_roles

logger = logging.getLogger('organized.backup')

# Congregation-scoped backup tables
CONG_TABLES = {
    'persons', 'field_service_groups', 'upcoming_events',
    'speakers_congregations', 'visiting_speakers',
    'cong_field_service_reports', 'sources', 'schedules',
    'meeting_attendance', 'incoming_reports',
    'branch_cong_analysis', 'branch_field_service_reports',
    'public_sources', 'public_schedules',
}

# User-scoped backup tables
USER_TABLES = {
    'user_bible_studies', 'user_field_service_reports',
    'delegated_field_service_reports',
}


def retrieve_user_backup(cong_user, incoming_metadata):
    """Build the backup GET response for a VIP user.

    Args:
        cong_user: CongUser instance (with congregation prefetched)
        incoming_metadata: dict of {table_name: iso_timestamp} from request header

    Returns:
        dict matching upstream response shape
    """
    from api.models import CongBackupTable, UserBackupTable, Metadata

    cong = cong_user.congregation
    roles = get_user_roles(cong_user.cong_role)
    result = {}
    result_metadata = {}

    data_sync = (cong.cong_settings or {}).get('data_sync', {}).get('value', True)

    # Always include app_settings
    result['app_settings'] = {
        'user_settings': {
            'firstname': cong_user.firstname,
            'lastname': cong_user.lastname,
            'cong_role': cong_user.cong_role,
            'user_local_uid': cong_user.user_local_uid,
            'user_members_delegate': cong_user.user_members_delegate,
        },
        'cong_settings': cong.get_settings_for_user(cong_user),
    }

    if not data_sync:
        result['metadata'] = result_metadata
        return result

    # Field service groups (always for authenticated users)
    _maybe_add_table(result, result_metadata, cong, 'field_service_groups',
                     incoming_metadata, CongBackupTable)

    # Upcoming events (always)
    _maybe_add_table(result, result_metadata, cong, 'upcoming_events',
                     incoming_metadata, CongBackupTable)

    # Persons (full for personViewer, minimal for others)
    if roles['personViewer']:
        _maybe_add_table(result, result_metadata, cong, 'persons',
                         incoming_metadata, CongBackupTable)

    # Elder-only tables
    if roles['elderRole']:
        _maybe_add_table(result, result_metadata, cong, 'speakers_congregations',
                         incoming_metadata, CongBackupTable)
        _maybe_add_table(result, result_metadata, cong, 'visiting_speakers',
                         incoming_metadata, CongBackupTable)

    # Report editor
    if roles['reportEditorRole']:
        _maybe_add_table(result, result_metadata, cong, 'cong_field_service_reports',
                         incoming_metadata, CongBackupTable)

    # Public talk editor
    if roles['publicTalkEditor']:
        outgoing = cong.outgoing_speakers or {}
        result['speakers_key'] = outgoing.get('speakers_key', '')
        result['outgoing_talks'] = outgoing.get('outgoing_talks', [])

    # User-scoped tables (admin or publisher)
    if roles['adminRole'] or roles['isPublisher']:
        for table in USER_TABLES:
            _maybe_add_user_table(result, result_metadata, cong_user, table,
                                  incoming_metadata, UserBackupTable)

    # Person minimal (stripped person data for non-viewers)
    if roles['personMinimal']:
        _maybe_add_table(result, result_metadata, cong, 'public_sources',
                         incoming_metadata, CongBackupTable)
        _maybe_add_table(result, result_metadata, cong, 'public_schedules',
                         incoming_metadata, CongBackupTable)

    # Schedule editor or elder
    if roles['scheduleEditor'] or roles['elderRole']:
        _maybe_add_table(result, result_metadata, cong, 'sources',
                         incoming_metadata, CongBackupTable)
        _maybe_add_table(result, result_metadata, cong, 'schedules',
                         incoming_metadata, CongBackupTable, response_key='sched')

    # Attendance tracker
    if roles['attendanceTracker']:
        _maybe_add_table(result, result_metadata, cong, 'meeting_attendance',
                         incoming_metadata, CongBackupTable)

    # Secretary
    if roles['secretaryRole']:
        ir = cong.incoming_reports
        local_date = _get_metadata(cong, None, 'incoming_reports', Metadata)
        incoming_date = incoming_metadata.get('incoming_reports', '')
        if local_date != incoming_date:
            result['incoming_reports'] = ir if ir else []
            result_metadata['incoming_reports'] = local_date

    # Admin-only
    if roles['adminRole']:
        _maybe_add_table(result, result_metadata, cong, 'branch_cong_analysis',
                         incoming_metadata, CongBackupTable)
        _maybe_add_table(result, result_metadata, cong, 'branch_field_service_reports',
                         incoming_metadata, CongBackupTable)

        # cong_users list for admin
        result['cong_users'] = [
            {
                'id': str(m.id),
                'local_uid': m.user_local_uid,
                'role': m.cong_role,
            }
            for m in cong.members.all()
        ]

    result['metadata'] = result_metadata
    return result


def save_user_backup(cong_user, cong_backup):
    """Process a backup POST with conflict detection.

    Args:
        cong_user: CongUser instance
        cong_backup: dict from request body {metadata, app_settings, persons, ...}

    Returns:
        (True, None) on success
        (False, 'BACKUP_OUTDATED') on conflict
    """
    from api.models import CongBackupTable, UserBackupTable, Metadata

    cong = cong_user.congregation

    incoming_metadata = cong_backup.get('metadata', {})

    # Build current server metadata
    current_cong_meta = {
        m.key: m.value
        for m in Metadata.objects.filter(congregation=cong, cong_user__isnull=True)
    }
    current_user_meta = {
        m.key: m.value
        for m in Metadata.objects.filter(congregation=cong, cong_user=cong_user)
    }
    current_metadata = {**current_cong_meta, **current_user_meta}

    # Check for outdated backup (conflict detection)
    for key, value in incoming_metadata.items():
        if key in current_metadata and current_metadata[key] > value:
            logger.warning(
                f"BACKUP_OUTDATED for {cong_user} on table '{key}': "
                f"server={current_metadata[key]} > client={value}"
            )
            return False, 'BACKUP_OUTDATED'

    # Save app_settings if present
    if 'app_settings' in cong_backup:
        _save_app_settings(cong, cong_user, cong_backup['app_settings'])

    # Capture old schedules data for diff engine (Phase 3)
    old_schedules_data = None
    if 'schedules' in cong_backup or 'sched' in cong_backup:
        try:
            old_bt = CongBackupTable.objects.get(
                congregation=cong, table_name='schedules'
            )
            old_schedules_data = old_bt.data
        except CongBackupTable.DoesNotExist:
            old_schedules_data = []

    # Save congregation-scoped tables
    for table_name in CONG_TABLES:
        if table_name in cong_backup:
            _save_cong_table(cong, table_name, cong_backup[table_name],
                             CongBackupTable)
            _update_metadata(cong, None, table_name,
                             incoming_metadata.get(table_name, ''), Metadata)

    # Handle 'sched' → 'schedules' key mapping
    if 'sched' in cong_backup:
        _save_cong_table(cong, 'schedules', cong_backup['sched'],
                         CongBackupTable)
        _update_metadata(cong, None, 'schedules',
                         incoming_metadata.get('schedules', ''), Metadata)

    # Trigger schedule diff engine (Phase 3)
    new_schedules_data = cong_backup.get('schedules') or cong_backup.get('sched')
    if old_schedules_data is not None and new_schedules_data is not None:
        try:
            from api.tasks import detect_schedule_changes
            detect_schedule_changes.delay(
                cong.pk, old_schedules_data, new_schedules_data
            )
        except Exception as e:
            logger.warning(f"Failed to queue schedule diff: {e}")

    # Save user-scoped tables
    for table_name in USER_TABLES:
        if table_name in cong_backup:
            _save_user_table(cong_user, table_name, cong_backup[table_name],
                             UserBackupTable)
            _update_metadata(cong, cong_user, table_name,
                             incoming_metadata.get(table_name, ''), Metadata)

    # Save incoming_reports if present
    if 'incoming_reports' in cong_backup:
        cong.incoming_reports = cong_backup['incoming_reports']
        cong.save(update_fields=['incoming_reports'])
        _update_metadata(cong, None, 'incoming_reports',
                         incoming_metadata.get('incoming_reports', ''), Metadata)

    logger.info(
        f"Backup saved for {cong_user} — "
        f"{len([k for k in cong_backup if k not in ('metadata', 'app_settings')])} tables"
    )
    return True, None


# --- Private helpers ---

def _maybe_add_table(result, metadata, cong, table_name, incoming_metadata,
                     BackupModel, response_key=None):
    """Add a congregation table to result if metadata differs."""
    from api.models import Metadata as MetadataModel

    key = response_key or table_name
    local_date = _get_metadata(cong, None, table_name, MetadataModel)
    incoming_date = incoming_metadata.get(table_name, '')

    if local_date != incoming_date:
        try:
            bt = BackupModel.objects.get(congregation=cong, table_name=table_name)
            result[key] = bt.data
        except BackupModel.DoesNotExist:
            result[key] = []
        metadata[table_name] = local_date


def _maybe_add_user_table(result, metadata, cong_user, table_name,
                          incoming_metadata, BackupModel):
    """Add a user-scoped table to result if metadata differs."""
    from api.models import Metadata as MetadataModel

    local_date = _get_metadata(
        cong_user.congregation, cong_user, table_name, MetadataModel
    )
    incoming_date = incoming_metadata.get(table_name, '')

    if local_date != incoming_date:
        try:
            bt = BackupModel.objects.get(cong_user=cong_user, table_name=table_name)
            result[table_name] = bt.data
        except BackupModel.DoesNotExist:
            result[table_name] = []
        metadata[table_name] = local_date


def _get_metadata(cong, cong_user, key, MetadataModel):
    """Get a metadata value. Returns empty string if not found."""
    try:
        return MetadataModel.objects.get(
            congregation=cong, cong_user=cong_user, key=key
        ).value
    except MetadataModel.DoesNotExist:
        return ''


def _save_cong_table(cong, table_name, data, BackupModel):
    """Upsert a congregation-scoped backup table."""
    BackupModel.objects.update_or_create(
        congregation=cong, table_name=table_name,
        defaults={'data': data}
    )


def _save_user_table(cong_user, table_name, data, BackupModel):
    """Upsert a user-scoped backup table."""
    BackupModel.objects.update_or_create(
        cong_user=cong_user, table_name=table_name,
        defaults={'data': data}
    )


def _update_metadata(cong, cong_user, key, value, MetadataModel):
    """Upsert a metadata entry."""
    if value:
        MetadataModel.objects.update_or_create(
            congregation=cong, cong_user=cong_user, key=key,
            defaults={'value': value}
        )


def _save_app_settings(cong, cong_user, app_settings):
    """Save user and congregation settings from backup payload."""
    user_settings = app_settings.get('user_settings', {})
    cong_settings = app_settings.get('cong_settings', {})

    # Update user settings
    if user_settings:
        changed = False
        for field in ('firstname', 'lastname', 'user_local_uid'):
            if field in user_settings:
                setattr(cong_user, field, user_settings[field])
                changed = True
        if 'cong_role' in user_settings:
            cong_user.cong_role = user_settings['cong_role']
            changed = True
        if 'user_members_delegate' in user_settings:
            cong_user.user_members_delegate = user_settings['user_members_delegate']
            changed = True
        if changed:
            cong_user.save()

    # Update congregation settings
    if cong_settings:
        merged = cong.cong_settings or {}
        merged.update(cong_settings)
        cong.cong_settings = merged
        cong.save(update_fields=['cong_settings'])
