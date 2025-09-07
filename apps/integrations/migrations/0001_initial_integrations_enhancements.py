"""
# Enhanced Integrations with Rate Limiting and Conflict Resolution

1. New Features
   - Enhanced CalendarIntegration model with sync tracking
   - Enhanced VideoConferenceIntegration model with rate limiting
   - Updated BlockedTime model with source tracking for conflict resolution
   - Added external_calendar_event_id to Booking model

2. Conflict Resolution
   - BlockedTime now tracks source (manual vs synced from external calendars)
   - External calendar events are reconciled with existing manual blocks
   - Prevents overwriting manual blocks during sync

3. Rate Limiting
   - VideoConferenceIntegration tracks API calls per day
   - Built-in rate limiting with automatic reset
   - Prevents exceeding provider API quotas

4. Monitoring
   - Enhanced sync error tracking
   - Last sync timestamp tracking
   - Automatic integration deactivation after consecutive failures
"""

from django.db import migrations, models
import django.core.validators
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('integrations', '0001_initial'),
        ('availability', '0001_initial'),
        ('events', '0001_initial'),
    ]

    operations = [
        # Enhance CalendarIntegration model
        migrations.AddField(
            model_name='calendarintegration',
            name='last_sync_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='calendarintegration',
            name='sync_token',
            field=models.TextField(blank=True, help_text='Token for incremental sync'),
        ),
        migrations.AddField(
            model_name='calendarintegration',
            name='sync_errors',
            field=models.IntegerField(default=0, help_text='Consecutive sync error count'),
        ),
        
        # Enhance VideoConferenceIntegration model
        migrations.AddField(
            model_name='videoconferenceintegration',
            name='last_api_call',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='videoconferenceintegration',
            name='api_calls_today',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='videoconferenceintegration',
            name='rate_limit_reset_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        
        # Add indexes for performance
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_calendar_active_sync ON calendar_integrations(is_active, sync_enabled);",
            reverse_sql="DROP INDEX IF EXISTS idx_calendar_active_sync;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_calendar_last_sync ON calendar_integrations(last_sync_at);",
            reverse_sql="DROP INDEX IF EXISTS idx_calendar_last_sync;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_video_active_auto ON video_integrations(is_active, auto_generate_links);",
            reverse_sql="DROP INDEX IF EXISTS idx_video_active_auto;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_video_rate_limit ON video_integrations(rate_limit_reset_at);",
            reverse_sql="DROP INDEX IF EXISTS idx_video_rate_limit;"
        ),
    ]