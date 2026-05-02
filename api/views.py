"""API views."""
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import Congregation, SyncRecord, UserProfile
from .serializers import CongregationSerializer, SyncRecordSerializer, UserProfileSerializer


class CongregationViewSet(viewsets.ModelViewSet):
    """CRUD for congregations."""

    queryset = Congregation.objects.all()
    serializer_class = CongregationSerializer


class SyncRecordViewSet(viewsets.ModelViewSet):
    """
    Sync record endpoint for offline-first data synchronization.
    Clients push local changes and pull updates since last sync.
    """

    queryset = SyncRecord.objects.all()
    serializer_class = SyncRecordSerializer

    def perform_create(self, serializer):
        serializer.save(last_synced_by=self.request.user)

    def perform_update(self, serializer):
        instance = self.get_object()
        serializer.save(
            last_synced_by=self.request.user,
            version=instance.version + 1,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """Health check endpoint for monitoring."""
    return Response(
        {"status": "ok", "service": "organized-backend"},
        status=status.HTTP_200_OK,
    )
