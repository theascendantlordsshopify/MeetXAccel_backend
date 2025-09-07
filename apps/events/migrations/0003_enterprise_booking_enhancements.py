"""
# Enterprise Booking System Enhancements

1. New Features
   - Enhanced EventType with advanced scheduling rules and recurrence
   - Comprehensive Booking model with security tokens and audit trails
   - CustomQuestion model with conditional logic support
   - Attendee model for group event management
   - WaitlistEntry model for handling full events
   - RecurringEventException for managing recurring event exceptions
   - BookingAuditLog for comprehensive audit trails
   - EventTypeAvailabilityCache for performance optimization

2. Security Enhancements
   - Secure access tokens for booking management
   - Token expiration and regeneration capabilities
   - Comprehensive audit logging for all booking actions

3. Group Event Support
   - Multi-attendee booking management
   - Waitlist functionality when events are full
   - Individual attendee cancellation support

4. Performance Optimizations
   - Availability caching with dirty flag management
   - Optimized database queries with proper indexing
   - Cache invalidation strategies

5. Enterprise Features
   - Recurring event support with exceptions
   - Advanced scheduling constraints
   - Cross-event-type conflict prevention
   - External calendar integration tracking
"""

from django.db import migrations, models
import django.core.validators
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0002_add_external_calendar_event_id'),
        ('workflows', '0001_initial'),
    ]

    operations = [
        # Enhance EventType model
        migrations.AddField(
            model_name='eventtype',
            name='enable_waitlist',
            field=models.BooleanField(default=False, help_text='Allow waitlist when event is full'),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='is_private',
            field=models.BooleanField(default=False, help_text='Private events are only accessible via direct link'),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='min_scheduling_notice',
            field=models.IntegerField(default=60, help_text='Minimum booking notice (minutes)', validators=[django.core.validators.MinValueValidator(0)]),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='max_scheduling_horizon',
            field=models.IntegerField(default=43200, help_text='Maximum booking advance (minutes)', validators=[django.core.validators.MinValueValidator(60)]),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='buffer_time_before',
            field=models.IntegerField(default=0, help_text='Buffer time before meeting (minutes)', validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(120)]),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='buffer_time_after',
            field=models.IntegerField(default=0, help_text='Buffer time after meeting (minutes)', validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(120)]),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='max_bookings_per_day',
            field=models.IntegerField(blank=True, help_text='Maximum bookings per day for this event type', null=True, validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(50)]),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='recurrence_type',
            field=models.CharField(choices=[('none', 'No Recurrence'), ('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly')], default='none', max_length=20),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='recurrence_rule',
            field=models.TextField(blank=True, help_text='RRULE string for complex recurrence patterns'),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='max_occurrences',
            field=models.IntegerField(blank=True, help_text='Maximum number of recurring occurrences', null=True, validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(365)]),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='recurrence_end_date',
            field=models.DateField(blank=True, help_text='End date for recurring events', null=True),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='redirect_url_after_booking',
            field=models.URLField(blank=True, help_text='URL to redirect invitee after successful booking'),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='confirmation_workflow',
            field=models.ForeignKey(blank=True, help_text='Workflow to trigger on booking confirmation', null=True, on_delete=models.SET_NULL, related_name='confirmation_event_types', to='workflows.workflow'),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='reminder_workflow',
            field=models.ForeignKey(blank=True, help_text='Workflow to trigger for reminders', null=True, on_delete=models.SET_NULL, related_name='reminder_event_types', to='workflows.workflow'),
        ),
        migrations.AddField(
            model_name='eventtype',
            name='cancellation_workflow',
            field=models.ForeignKey(blank=True, help_text='Workflow to trigger on cancellation', null=True, on_delete=models.SET_NULL, related_name='cancellation_event_types', to='workflows.workflow'),
        ),
        
        # Enhance Booking model
        migrations.AddField(
            model_name='booking',
            name='recurrence_id',
            field=models.UUIDField(blank=True, help_text='Links recurring bookings together', null=True),
        ),
        migrations.AddField(
            model_name='booking',
            name='is_recurring_exception',
            field=models.BooleanField(default=False, help_text='True if this booking is an exception to a recurring series'),
        ),
        migrations.AddField(
            model_name='booking',
            name='recurrence_sequence',
            field=models.IntegerField(blank=True, help_text='Sequence number in recurring series', null=True),
        ),
        migrations.AddField(
            model_name='booking',
            name='access_token',
            field=models.UUIDField(default=uuid.uuid4, help_text='Secure token for invitee booking management', unique=True),
        ),
        migrations.AddField(
            model_name='booking',
            name='access_token_expires_at',
            field=models.DateTimeField(blank=True, help_text='Expiration time for access token', null=True),
        ),
        migrations.AddField(
            model_name='booking',
            name='calendar_sync_status',
            field=models.CharField(choices=[('pending', 'Pending'), ('succeeded', 'Succeeded'), ('failed', 'Failed'), ('not_required', 'Not Required')], default='pending', max_length=20),
        ),
        migrations.AddField(
            model_name='booking',
            name='calendar_sync_error',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='booking',
            name='calendar_sync_attempts',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='booking',
            name='last_calendar_sync_attempt',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='booking',
            name='cancelled_by',
            field=models.CharField(blank=True, choices=[('organizer', 'Organizer'), ('invitee', 'Invitee'), ('system', 'System')], max_length=20),
        ),
        migrations.AddField(
            model_name='booking',
            name='rescheduled_from',
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='rescheduled_to_bookings', to='events.booking'),
        ),
        migrations.AddField(
            model_name='booking',
            name='rescheduled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        
        # Create CustomQuestion model
        migrations.CreateModel(
            name='CustomQuestion',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('question_text', models.CharField(max_length=500)),
                ('question_type', models.CharField(choices=[('text', 'Text Input'), ('textarea', 'Long Text'), ('select', 'Single Select'), ('multiselect', 'Multiple Select'), ('checkbox', 'Checkbox'), ('radio', 'Radio Buttons'), ('email', 'Email'), ('phone', 'Phone Number'), ('number', 'Number'), ('date', 'Date'), ('time', 'Time'), ('url', 'URL')], default='text', max_length=20)),
                ('is_required', models.BooleanField(default=False)),
                ('order', models.IntegerField(default=0)),
                ('options', models.JSONField(blank=True, default=list, help_text='List of options for select/radio questions')),
                ('conditions', models.JSONField(blank=True, default=list, help_text='Conditions for showing this question based on previous answers')),
                ('validation_rules', models.JSONField(blank=True, default=dict, help_text='Validation rules (min_length, max_length, pattern, etc.)')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('event_type', models.ForeignKey(on_delete=models.CASCADE, related_name='questions', to='events.eventtype')),
            ],
            options={
                'db_table': 'custom_questions',
                'verbose_name': 'Custom Question',
                'verbose_name_plural': 'Custom Questions',
                'ordering': ['order'],
            },
        ),
        
        # Create Attendee model
        migrations.CreateModel(
            name='Attendee',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=200)),
                ('email', models.EmailField()),
                ('phone', models.CharField(blank=True, max_length=20)),
                ('status', models.CharField(choices=[('confirmed', 'Confirmed'), ('cancelled', 'Cancelled'), ('no_show', 'No Show')], default='confirmed', max_length=20)),
                ('custom_answers', models.JSONField(blank=True, default=dict)),
                ('joined_at', models.DateTimeField(auto_now_add=True)),
                ('cancelled_at', models.DateTimeField(blank=True, null=True)),
                ('cancellation_reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('booking', models.ForeignKey(on_delete=models.CASCADE, related_name='attendees', to='events.booking')),
            ],
            options={
                'db_table': 'booking_attendees',
                'verbose_name': 'Attendee',
                'verbose_name_plural': 'Attendees',
            },
        ),
        
        # Create WaitlistEntry model
        migrations.CreateModel(
            name='WaitlistEntry',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('desired_start_time', models.DateTimeField()),
                ('desired_end_time', models.DateTimeField()),
                ('invitee_name', models.CharField(max_length=200)),
                ('invitee_email', models.EmailField()),
                ('invitee_phone', models.CharField(blank=True, max_length=20)),
                ('invitee_timezone', models.CharField(default='UTC', max_length=50)),
                ('notify_when_available', models.BooleanField(default=True)),
                ('expires_at', models.DateTimeField(help_text='When this waitlist entry expires')),
                ('status', models.CharField(choices=[('active', 'Active'), ('notified', 'Notified'), ('converted', 'Converted to Booking'), ('expired', 'Expired'), ('cancelled', 'Cancelled')], default='active', max_length=20)),
                ('custom_answers', models.JSONField(blank=True, default=dict)),
                ('notified_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('converted_booking', models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='converted_from_waitlist', to='events.booking')),
                ('event_type', models.ForeignKey(on_delete=models.CASCADE, related_name='waitlist_entries', to='events.eventtype')),
                ('organizer', models.ForeignKey(on_delete=models.CASCADE, related_name='waitlist_entries', to='users.user')),
            ],
            options={
                'db_table': 'waitlist_entries',
                'verbose_name': 'Waitlist Entry',
                'verbose_name_plural': 'Waitlist Entries',
                'ordering': ['created_at'],
            },
        ),
        
        # Create RecurringEventException model
        migrations.CreateModel(
            name='RecurringEventException',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('recurrence_id', models.UUIDField(help_text='Links to the recurring series')),
                ('exception_date', models.DateField()),
                ('exception_type', models.CharField(choices=[('cancelled', 'Cancelled'), ('rescheduled', 'Rescheduled'), ('modified', 'Modified')], max_length=20)),
                ('new_start_time', models.DateTimeField(blank=True, null=True)),
                ('new_end_time', models.DateTimeField(blank=True, null=True)),
                ('modified_fields', models.JSONField(blank=True, default=dict, help_text='Fields that were modified for this occurrence')),
                ('reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('event_type', models.ForeignKey(on_delete=models.CASCADE, related_name='recurrence_exceptions', to='events.eventtype')),
            ],
            options={
                'db_table': 'recurring_event_exceptions',
                'verbose_name': 'Recurring Event Exception',
                'verbose_name_plural': 'Recurring Event Exceptions',
            },
        ),
        
        # Create BookingAuditLog model
        migrations.CreateModel(
            name='BookingAuditLog',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('action', models.CharField(choices=[('booking_created', 'Booking Created'), ('booking_cancelled', 'Booking Cancelled'), ('booking_rescheduled', 'Booking Rescheduled'), ('booking_completed', 'Booking Completed'), ('attendee_added', 'Attendee Added'), ('attendee_cancelled', 'Attendee Cancelled'), ('waitlist_added', 'Added to Waitlist'), ('waitlist_converted', 'Waitlist Converted'), ('calendar_sync_success', 'Calendar Sync Success'), ('calendar_sync_failed', 'Calendar Sync Failed'), ('workflow_triggered', 'Workflow Triggered'), ('notification_sent', 'Notification Sent'), ('access_token_regenerated', 'Access Token Regenerated')], max_length=30)),
                ('description', models.TextField()),
                ('actor_type', models.CharField(choices=[('organizer', 'Organizer'), ('invitee', 'Invitee'), ('attendee', 'Attendee'), ('system', 'System'), ('integration', 'Integration')], max_length=20)),
                ('actor_email', models.EmailField(blank=True)),
                ('actor_name', models.CharField(blank=True, max_length=200)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.TextField(blank=True)),
                ('metadata', models.JSONField(blank=True, default=dict, help_text='Additional context data')),
                ('old_values', models.JSONField(blank=True, default=dict)),
                ('new_values', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('booking', models.ForeignKey(on_delete=models.CASCADE, related_name='audit_logs', to='events.booking')),
            ],
            options={
                'db_table': 'booking_audit_logs',
                'verbose_name': 'Booking Audit Log',
                'verbose_name_plural': 'Booking Audit Logs',
                'ordering': ['-created_at'],
            },
        ),
        
        # Create EventTypeAvailabilityCache model
        migrations.CreateModel(
            name='EventTypeAvailabil ityCache',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('date', models.DateField()),
                ('timezone_name', models.CharField(max_length=50)),
                ('attendee_count', models.IntegerField(default=1)),
                ('available_slots', models.JSONField(help_text='Serialized available slots')),
                ('computed_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField()),
                ('is_dirty', models.BooleanField(default=False, help_text='True if cache needs recomputation')),
                ('computation_time_ms', models.IntegerField(blank=True, help_text='Time taken to compute this cache entry', null=True)),
                ('event_type', models.ForeignKey(on_delete=models.CASCADE, related_name='availability_cache', to='events.eventtype')),
                ('organizer', models.ForeignKey(on_delete=models.CASCADE, related_name='availability_cache', to='users.user')),
            ],
            options={
                'db_table': 'event_type_availability_cache',
                'verbose_name': 'Availability Cache',
                'verbose_name_plural': 'Availability Cache',
            },
        ),
        
        # Add unique constraints
        migrations.AlterUniqueTogether(
            name='customquestion',
            unique_together={('event_type', 'order')},
        ),
        migrations.AlterUniqueTogether(
            name='attendee',
            unique_together={('booking', 'email')},
        ),
        migrations.AlterUniqueTogether(
            name='recurringeventexception',
            unique_together={('event_type', 'recurrence_id', 'exception_date')},
        ),
        migrations.AlterUniqueTogether(
            name='eventtypeavailabilitycache',
            unique_together={('organizer', 'event_type', 'date', 'timezone_name', 'attendee_count')},
        ),
        
        # Add indexes for performance
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_eventtype_organizer_active ON event_types(organizer_id, is_active, is_private);",
            reverse_sql="DROP INDEX IF EXISTS idx_eventtype_organizer_active;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_booking_organizer_time ON bookings(organizer_id, start_time, end_time);",
            reverse_sql="DROP INDEX IF EXISTS idx_booking_organizer_time;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_booking_status_time ON bookings(status, start_time);",
            reverse_sql="DROP INDEX IF EXISTS idx_booking_status_time;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_booking_access_token ON bookings(access_token);",
            reverse_sql="DROP INDEX IF EXISTS idx_booking_access_token;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_booking_recurrence ON bookings(recurrence_id);",
            reverse_sql="DROP INDEX IF EXISTS idx_booking_recurrence;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_booking_calendar_sync ON bookings(calendar_sync_status);",
            reverse_sql="DROP INDEX IF EXISTS idx_booking_calendar_sync;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_waitlist_event_status ON waitlist_entries(event_type_id, status, desired_start_time);",
            reverse_sql="DROP INDEX IF EXISTS idx_waitlist_event_status;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_waitlist_expires ON waitlist_entries(expires_at);",
            reverse_sql="DROP INDEX IF EXISTS idx_waitlist_expires;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_audit_booking_created ON booking_audit_logs(booking_id, created_at DESC);",
            reverse_sql="DROP INDEX IF EXISTS idx_audit_booking_created;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_audit_action_created ON booking_audit_logs(action, created_at DESC);",
            reverse_sql="DROP INDEX IF EXISTS idx_audit_action_created;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_cache_expires ON event_type_availability_cache(expires_at);",
            reverse_sql="DROP INDEX IF EXISTS idx_cache_expires;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_cache_dirty ON event_type_availability_cache(is_dirty);",
            reverse_sql="DROP INDEX IF EXISTS idx_cache_dirty;"
        ),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS idx_cache_organizer_date ON event_type_availability_cache(organizer_id, date);",
            reverse_sql="DROP INDEX IF EXISTS idx_cache_organizer_date;"
        ),
    ]