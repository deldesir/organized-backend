"""Signal handlers for the organized backend.

Phase 1: No-op. The legacy Assignment-based signal is removed.
Phase 3 will add CongBackupTable-based diff detection signals.
"""

# No active signals during Phase 1-2.
# Phase 3 will register:
#   @receiver(post_save, sender=CongBackupTable)
#   def backup_table_saved(sender, instance, **kwargs): ...
