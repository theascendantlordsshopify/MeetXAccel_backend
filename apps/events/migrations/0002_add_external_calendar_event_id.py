"""
# Add External Calendar Event ID to Booking

1. New Features
   - Added external_calendar_event_id to link bookings with external calendar events
   - Enables proper update and deletion of calendar events when bookings change

2. Integration Support
   - Supports Google Calendar event IDs
   - Supports Outlook Calendar event IDs
   - Enables bidirectional calendar synchronization
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='external_calendar_event_id',
            field=models.CharField(
                blank=True,
                help_text='ID from external calendar system',
                max_length=200
            ),
        ),
    ]