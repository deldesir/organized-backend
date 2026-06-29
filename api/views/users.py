"""User backup views: GET and POST for VIP users.

Replicates the upstream /users/:id/backup endpoints.
"""

import json
import logging

from django.core.cache import cache
from django.http import JsonResponse
from django.views import View

from api.services.backup import retrieve_user_backup, save_user_backup

logger = logging.getLogger('organized.backup')


class UserBackupView(View):
    """GET/POST /api/v3/users/<id>/backup

    GET: Return role-scoped backup data with metadata-based incremental sync.
    POST: Save backup with conflict detection (BACKUP_OUTDATED).
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'USER_ID_INVALID'}, status=400)

        metadata_str = request.META.get('HTTP_METADATA', '{}')
        try:
            incoming_metadata = json.loads(metadata_str)
        except json.JSONDecodeError:
            incoming_metadata = {}

        result = retrieve_user_backup(cong_user, incoming_metadata)
        return JsonResponse(result)

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'USER_ID_INVALID'}, status=400)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        cong_backup = body.get('cong_backup', body)
        success, error = save_user_backup(cong_user, cong_backup)

        if not success:
            return JsonResponse({'message': error}, status=400)

        return JsonResponse({'message': 'BACKUP_SENT'})


class ChunkedBackupView(View):
    """POST /api/v3/users/<id>/backup/chunked

    Accepts chunked backup uploads. Reassembles and processes
    when the final chunk arrives.
    """

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'USER_ID_INVALID'}, status=400)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        upload_id = body.get('uploadId')
        chunk_index = body.get('chunkIndex')
        total_chunks = body.get('totalChunks')
        chunk_data = body.get('chunkData')

        if not all(x is not None for x in [upload_id, chunk_index, total_chunks, chunk_data]):
            return JsonResponse({'message': 'MISSING_PARAMETERS'}, status=400)

        # Store chunk in cache
        cache_key = f"upload_{cong_user.id}_{upload_id}_chunk_{chunk_index}"
        cache.set(cache_key, chunk_data, timeout=3600)

        # Reassemble if this is the last chunk
        if chunk_index == total_chunks - 1:
            full_json_str = ""
            for i in range(total_chunks):
                chunk = cache.get(f"upload_{cong_user.id}_{upload_id}_chunk_{i}")
                if chunk is None:
                    return JsonResponse(
                        {'message': f'MISSING_CHUNK_{i}'}, status=400
                    )
                full_json_str += chunk

            try:
                payload = json.loads(full_json_str)
            except json.JSONDecodeError:
                return JsonResponse({'message': 'INVALID_JSON'}, status=400)

            cong_backup = payload.get('cong_backup', payload)
            success, error = save_user_backup(cong_user, cong_backup)

            # Clean up cache
            for i in range(total_chunks):
                cache.delete(f"upload_{cong_user.id}_{upload_id}_chunk_{i}")

            if not success:
                return JsonResponse({'message': error}, status=409)

            return JsonResponse({'message': 'BACKUP_SENT'})

        return JsonResponse({'message': 'CHUNK_UPLOADED'})


class UserUpdatesView(View):
    """GET /api/v3/users/<id>/updates-routine

    Returns pending updates for the user (e.g., schedule changes).
    Simplified version — returns empty for now.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'USER_ID_INVALID'}, status=400)

        return JsonResponse({'updates': []})
