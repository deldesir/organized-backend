"""API serializers."""
from rest_framework import serializers
from .models import Congregation, SyncRecord, UserProfile


class CongregationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Congregation
        fields = "__all__"


class UserProfileSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = UserProfile
        fields = ["id", "username", "congregation", "role", "created_at", "updated_at"]


class SyncRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = SyncRecord
        fields = "__all__"
        read_only_fields = ["version", "last_synced_by"]
