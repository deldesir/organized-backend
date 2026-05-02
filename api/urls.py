"""API URL configuration."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"congregations", views.CongregationViewSet)
router.register(r"sync", views.SyncRecordViewSet)

urlpatterns = [
    path("health/", views.health_check, name="health-check"),
    path("", include(router.urls)),
]
