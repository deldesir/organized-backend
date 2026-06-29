from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

class MockUserMeView(APIView):
    def get(self, request):
        return Response({"id": "test_user", "name": "Admin", "congregation": "Test Congregation"}, status=status.HTTP_200_OK)

class MockCongregationsView(APIView):
    def get(self, request):
        return Response([{"id": "test_cong", "name": "Test Congregation"}], status=status.HTTP_200_OK)

class MockSourcesView(APIView):
    def get(self, request):
        return Response([], status=status.HTTP_200_OK)

from api.serializers import BackupPayloadSerializer

class MockPocketsBackupView(APIView):
    def get(self, request):
        from api.models import Publisher, MeetingSchedule
        
        persons_data = [pub.data for pub in Publisher.objects.all() if pub.data]
        schedules_data = [sched.data for sched in MeetingSchedule.objects.all() if sched.data]
            
        backup_payload = {
            "cong_backup": [
                {"table": "persons", "data": persons_data},
                {"table": "schedules", "data": schedules_data}
            ]
        }
        return Response(backup_payload, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = BackupPayloadSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response({"message": "backup_received"}, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

import json
from django.core.cache import cache

class ChunkedBackupView(APIView):
    def post(self, request, user_id):
        upload_id = request.data.get('uploadId')
        chunk_index = request.data.get('chunkIndex')
        total_chunks = request.data.get('totalChunks')
        chunk_data = request.data.get('chunkData')

        if not all(x is not None for x in [upload_id, chunk_index, total_chunks, chunk_data]):
            return Response({"message": "missing_parameters"}, status=status.HTTP_400_BAD_REQUEST)

        # Store chunk in cache
        cache_key = f"upload_{upload_id}_chunk_{chunk_index}"
        cache.set(cache_key, chunk_data, timeout=3600)

        # Reassemble if this is the last chunk
        if chunk_index == total_chunks - 1:
            full_json_str = ""
            for i in range(total_chunks):
                chunk = cache.get(f"upload_{upload_id}_chunk_{i}")
                if chunk is None:
                    return Response({"message": f"missing_chunk_{i}"}, status=status.HTTP_400_BAD_REQUEST)
                full_json_str += chunk

            try:
                payload = json.loads(full_json_str)
                serializer = BackupPayloadSerializer(data={"cong_backup": payload})
                if serializer.is_valid():
                    serializer.save()
                    # Clean up cache
                    for i in range(total_chunks):
                        cache.delete(f"upload_{upload_id}_chunk_{i}")
                    return Response({"message": "backup_received"}, status=status.HTTP_200_OK)
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            except json.JSONDecodeError:
                return Response({"message": "invalid_json"}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": "CHUNK_UPLOADED"}, status=status.HTTP_200_OK)

class VisitingSpeakersView(APIView):
    def get(self, request):
        return Response([], status=status.HTTP_200_OK)
        
class VisitingSpeakersCongregationsView(APIView):
    def get(self, request):
        return Response([], status=status.HTTP_200_OK)

class HermesWebhookView(APIView):
    def post(self, request):
        intent = request.data.get('intent')
        assignment_id = request.data.get('assignment_id')
        
        if not intent or not assignment_id:
            return Response({"error": "Missing intent or assignment_id"}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            from api.models import Assignment
            assignment = Assignment.objects.get(id=assignment_id)
            if intent == 'assignment_decline':
                assignment.data['status'] = 'needs_substitute'
                assignment.save()
            elif intent == 'assignment_accept':
                assignment.data['status'] = 'accepted'
                assignment.save()
                
            return Response({"message": "Webhook processed"}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
