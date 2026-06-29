from django.contrib import admin
from .models import Congregation, CongUser, CongBackupTable, UserBackupTable, Metadata


@admin.register(Congregation)
class CongregationAdmin(admin.ModelAdmin):
    list_display = ('cong_name', 'cong_id', 'cong_number', 'country_code', 'created_at')
    search_fields = ('cong_name', 'cong_id', 'cong_number')


@admin.register(CongUser)
class CongUserAdmin(admin.ModelAdmin):
    list_display = ('firstname', 'lastname', 'congregation', 'cong_role', 'created_at')
    list_filter = ('congregation',)
    search_fields = ('firstname', 'lastname')


@admin.register(CongBackupTable)
class CongBackupTableAdmin(admin.ModelAdmin):
    list_display = ('congregation', 'table_name', 'updated_at')
    list_filter = ('congregation', 'table_name')


@admin.register(UserBackupTable)
class UserBackupTableAdmin(admin.ModelAdmin):
    list_display = ('cong_user', 'table_name', 'updated_at')
    list_filter = ('table_name',)


@admin.register(Metadata)
class MetadataAdmin(admin.ModelAdmin):
    list_display = ('congregation', 'cong_user', 'key', 'value')
    list_filter = ('congregation', 'key')
