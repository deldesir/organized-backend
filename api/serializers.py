from rest_framework import serializers
from .models import Congregation, Publisher, MeetingSchedule, Assignment

class BackupPayloadSerializer(serializers.Serializer):
    cong_backup = serializers.ListField(child=serializers.DictField())

    def create(self, validated_data):
        payloads = validated_data.get('cong_backup', [])
        
        # Extract cong_id dynamically from the payload's settings table
        cong_id = "DEFAULT"
        cong_name = "Default Congregation"
        for table_data in payloads:
            if table_data.get('table') == 'app_settings':
                for record in table_data.get('data', []):
                    user_settings = record.get('user_settings', {})
                    if user_settings.get('cong_id'):
                        cong_id = user_settings.get('cong_id')
                    
        cong, _ = Congregation.objects.get_or_create(cong_id=cong_id, defaults={"cong_name": cong_name})
        
        for table_data in payloads:
            table_name = table_data.get('table')
            records = table_data.get('data', [])
            
            if table_name == 'persons':
                for record in records:
                    person_uid = record.get('person_uid')
                    if person_uid:
                        Publisher.objects.update_or_create(
                            person_uid=person_uid,
                            defaults={
                                'congregation': cong,
                                'display_name': record.get('person_data', {}).get('person_display_name', {}).get('value', 'Unknown'),
                                'data': record
                            }
                        )
            
            elif table_name == 'schedules':
                for record in records:
                    week_of = record.get('weekOf')
                    if week_of:
                        sched, _ = MeetingSchedule.objects.update_or_create(
                            congregation=cong,
                            week_of=week_of,
                            defaults={'data': record}
                        )
                        # We could parse assignments here or let a Celery task do it
                        # For now, we store the full schedule JSON
                        
        return validated_data
