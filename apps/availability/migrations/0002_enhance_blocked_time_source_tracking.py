"""
# Enhanced BlockedTime with Source Tracking

1. New Features
   - Added source field to track origin of blocked times
   - Added external_id for linking to external calendar events
   - Added external_updated_at for sync tracking

2. Conflict Resolution
   - Distinguishes between manual and synced blocked times
   - Prevents accidental overwriting of manual blocks
   - Enables proper reconciliation during calendar sync

3. Performance
   - Added indexes for efficient querying by source and external_id
   - Optimized queries for calendar sync operations
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('availability', '0001_initial'),
    ]

    operations = [
        # Add source tracking to BlockedTime
        migrations.AddField(
            model_name='blockedtime',
            name='source',
            field=models.CharField(
                choices=[
                    ('manual', 'Manual'),
                    ('google_calendar', 'Google Calendar'),
                    ('outlook_calendar', 'Outlook Calendar'),
                    ('apple_calendar', 'Apple Calendar'),
                    ('external_sync', 'External Sync'),
                ],
                default='manual',
                max_length=20
            ),
        ),
        migrations.AddField(
            model_name='blockedtime',
            name='external_id',
            field=models.CharField(
                blank=True,
                help_text='ID from external calendar system',
                max_length=200
            ),
        ),
        migrations.AddField(
            model_name='blockedtime',
            name='external_updated_at',
            field=models.DateTimeField(
                blank=True,
                help_text='Last updated time from external system',
                null=True
            ),
        ),
        
        # Add indexes for performance
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_blocked_time_source_external ON blocked_times(organizer_id, source, external_id);",
            reverse_sql="DROP INDEX IF EXISTS idx_blocked_time_source_external;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_blocked_time_datetime_range ON blocked_times(organizer_id, start_datetime, end_datetime);",
            reverse_sql="DROP INDEX IF EXISTS idx_blocked_time_datetime_range;"
        ),
    ]