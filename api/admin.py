from django.contrib import admin
from .models import Congregation, SyncRecord, UserProfile

admin.site.register(Congregation)
admin.site.register(UserProfile)
admin.site.register(SyncRecord)
