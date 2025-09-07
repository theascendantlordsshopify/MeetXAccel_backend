from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo
from .models import AvailabilityRule, BlockedTime, BufferTime, DateOverrideRule, RecurringBlockedTime
from .utils import (
    calculate_available_slots, is_slot_blocked, is_slot_conflicting_with_bookings,
    is_slot_blocked_by_recurring, merge_overlapping_slots, validate_timezone,
    calculate_multi_invitee_intersection, calculate_timezone_offset_hours
)
from apps.events.models import EventType, Booking

User = get_user_model()


class AvailabilityUtilsTestCase(TestCase):
    """Test suite for availability calculation utilities."""
    
    def setUp(self):
        """Set up test data."""
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True,
            is_email_verified=True,
            account_status='active'
        )
        
        # Create organizer profile with timezone
        from apps.users.models import Profile
        Profile.objects.create(
            user=self.organizer,
            timezone_name='America/New_York'
        )
        
        self.event_type = EventType.objects.create(
            organizer=self.organizer,
            name='30 Min Meeting',
            duration=30,
            min_booking_notice=60,  # 1 hour notice
            max_booking_advance=43200,  # 30 days
            max_attendees=1
        )
        
        # Create basic availability rule (9 AM - 5 PM, Monday-Friday)
        for day in range(5):  # Monday to Friday
            AvailabilityRule.objects.create(
                organizer=self.organizer,
                day_of_week=day,
                start_time=time(9, 0),
                end_time=time(17, 0),
                is_active=True
            )
    
    def test_basic_availability_calculation(self):
        """Test basic availability calculation without conflicts."""
        tomorrow = timezone.now().date() + timedelta(days=1)
        
        # Ensure tomorrow is a weekday
        while tomorrow.weekday() >= 5:  # Skip weekends
            tomorrow += timedelta(days=1)
        
        slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='UTC'
        )
        
        self.assertGreater(len(slots), 0, "Should have available slots")
        
        # Check slot structure
        first_slot = slots[0]
        self.assertIn('start_time', first_slot)
        self.assertIn('end_time', first_slot)
        self.assertIn('duration_minutes', first_slot)
        self.assertEqual(first_slot['duration_minutes'], 30)
    
    def test_blocked_time_conflicts(self):
        """Test that blocked times properly block availability."""
        tomorrow = timezone.now().date() + timedelta(days=1)
        
        # Ensure tomorrow is a weekday
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        # Create a blocked time from 10 AM - 12 PM
        blocked_start = datetime.combine(tomorrow, time(10, 0)).replace(tzinfo=ZoneInfo('America/New_York'))
        blocked_end = datetime.combine(tomorrow, time(12, 0)).replace(tzinfo=ZoneInfo('America/New_York'))
        
        BlockedTime.objects.create(
            organizer=self.organizer,
            start_datetime=blocked_start.astimezone(timezone.utc),
            end_datetime=blocked_end.astimezone(timezone.utc),
            reason='Test block'
        )
        
        slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='UTC'
        )
        
        # Check that no slots overlap with the blocked time
        for slot in slots:
            slot_start_ny = slot['start_time'].astimezone(ZoneInfo('America/New_York'))
            slot_end_ny = slot['end_time'].astimezone(ZoneInfo('America/New_York'))
            
            # Ensure no overlap with 10 AM - 12 PM
            self.assertFalse(
                slot_start_ny.time() < time(12, 0) and slot_end_ny.time() > time(10, 0),
                f"Slot {slot_start_ny.time()}-{slot_end_ny.time()} overlaps with blocked time"
            )
    
    def test_midnight_spanning_rules(self):
        """Test availability rules that span across midnight."""
        # Create a night shift rule (10 PM - 6 AM)
        AvailabilityRule.objects.create(
            organizer=self.organizer,
            day_of_week=0,  # Monday night into Tuesday
            start_time=time(22, 0),  # 10 PM
            end_time=time(6, 0),     # 6 AM (next day)
            is_active=True
        )
        
        # Test on a Monday night
        monday = timezone.now().date()
        while monday.weekday() != 0:  # Find next Monday
            monday += timedelta(days=1)
        
        slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=monday,
            end_date=monday + timedelta(days=1),  # Include Tuesday
            invitee_timezone='America/New_York'
        )
        
        # Should have slots on Monday night and Tuesday early morning
        monday_night_slots = [
            slot for slot in slots 
            if slot['local_start_time'].date() == monday and slot['local_start_time'].hour >= 22
        ]
        
        tuesday_morning_slots = [
            slot for slot in slots 
            if slot['local_start_time'].date() == monday + timedelta(days=1) and slot['local_start_time'].hour < 6
        ]
        
        self.assertGreater(len(monday_night_slots), 0, "Should have Monday night slots")
        self.assertGreater(len(tuesday_morning_slots), 0, "Should have Tuesday morning slots")
    
    def test_recurring_blocked_times(self):
        """Test recurring blocked times."""
        # Create a recurring block every Monday 2-3 PM
        RecurringBlockedTime.objects.create(
            organizer=self.organizer,
            name='Weekly Team Meeting',
            day_of_week=0,  # Monday
            start_time=time(14, 0),  # 2 PM
            end_time=time(15, 0),    # 3 PM
            is_active=True
        )
        
        # Find next Monday
        monday = timezone.now().date()
        while monday.weekday() != 0:
            monday += timedelta(days=1)
        
        slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=monday,
            end_date=monday,
            invitee_timezone='America/New_York'
        )
        
        # Check that no slots overlap with 2-3 PM
        for slot in slots:
            slot_start_ny = slot['local_start_time']
            slot_end_ny = slot['local_end_time']
            
            # Ensure no overlap with 2-3 PM
            self.assertFalse(
                slot_start_ny.time() < time(15, 0) and slot_end_ny.time() > time(14, 0),
                f"Slot {slot_start_ny.time()}-{slot_end_ny.time()} overlaps with recurring block"
            )
    
    def test_date_override_rules(self):
        """Test date-specific override rules."""
        tomorrow = timezone.now().date() + timedelta(days=1)
        
        # Ensure tomorrow is a weekday (should have regular availability)
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        # Create a date override that blocks the entire day
        DateOverrideRule.objects.create(
            organizer=self.organizer,
            date=tomorrow,
            is_available=False,
            reason='Personal day off'
        )
        
        slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='UTC'
        )
        
        self.assertEqual(len(slots), 0, "Should have no slots due to date override")
        
        # Test partial day override
        day_after_tomorrow = tomorrow + timedelta(days=1)
        while day_after_tomorrow.weekday() >= 5:
            day_after_tomorrow += timedelta(days=1)
        
        DateOverrideRule.objects.create(
            organizer=self.organizer,
            date=day_after_tomorrow,
            is_available=True,
            start_time=time(10, 0),
            end_time=time(12, 0),
            reason='Limited availability'
        )
        
        slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=day_after_tomorrow,
            end_date=day_after_tomorrow,
            invitee_timezone='America/New_York'
        )
        
        # Should only have slots between 10 AM - 12 PM
        for slot in slots:
            slot_start_ny = slot['local_start_time']
            slot_end_ny = slot['local_end_time']
            
            self.assertGreaterEqual(slot_start_ny.time(), time(10, 0))
            self.assertLessEqual(slot_end_ny.time(), time(12, 0))
    
    def test_group_event_capacity(self):
        """Test capacity management for group events."""
        # Create a group event type
        group_event = EventType.objects.create(
            organizer=self.organizer,
            name='Group Workshop',
            duration=60,
            max_attendees=3
        )
        
        tomorrow = timezone.now().date() + timedelta(days=1)
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        # Create an existing booking with 2 attendees at 10 AM
        existing_booking_start = datetime.combine(tomorrow, time(10, 0)).replace(tzinfo=ZoneInfo('America/New_York'))
        
        Booking.objects.create(
            event_type=group_event,
            organizer=self.organizer,
            invitee_name='Test User',
            invitee_email='test@example.com',
            start_time=existing_booking_start.astimezone(timezone.utc),
            end_time=(existing_booking_start + timedelta(hours=1)).astimezone(timezone.utc),
            attendee_count=2,
            status='confirmed'
        )
        
        # Calculate slots for 1 more attendee (should be available)
        slots_1_attendee = calculate_available_slots(
            organizer=self.organizer,
            event_type=group_event,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='America/New_York',
            attendee_count=1
        )
        
        # Should have a slot at 10 AM (2 existing + 1 new = 3, which equals max_attendees)
        ten_am_slots = [
            slot for slot in slots_1_attendee
            if slot['local_start_time'].time() == time(10, 0)
        ]
        self.assertGreater(len(ten_am_slots), 0, "Should have 10 AM slot available for 1 attendee")
        
        # Calculate slots for 2 more attendees (should NOT be available)
        slots_2_attendees = calculate_available_slots(
            organizer=self.organizer,
            event_type=group_event,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='America/New_York',
            attendee_count=2
        )
        
        # Should NOT have a slot at 10 AM (2 existing + 2 new = 4, which exceeds max_attendees)
        ten_am_slots_2 = [
            slot for slot in slots_2_attendees
            if slot['local_start_time'].time() == time(10, 0)
        ]
        self.assertEqual(len(ten_am_slots_2), 0, "Should NOT have 10 AM slot available for 2 attendees")
    
    def test_multi_invitee_timezone_intersection(self):
        """Test multi-invitee timezone intersection logic."""
        tomorrow = timezone.now().date() + timedelta(days=1)
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        # Test with multiple timezones
        invitee_timezones = ['America/New_York', 'Europe/London', 'Asia/Tokyo']
        
        slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='America/New_York',
            invitee_timezones=invitee_timezones
        )
        
        # Check that returned slots have timezone information
        if slots:
            first_slot = slots[0]
            self.assertIn('invitee_times', first_slot)
            self.assertIn('fairness_score', first_slot)
            
            # Check that all requested timezones are included
            for tz in invitee_timezones:
                self.assertIn(tz, first_slot['invitee_times'])
    
    def test_dst_transition_handling(self):
        """Test handling of DST transitions."""
        # Test around DST transition dates
        # Spring forward: Second Sunday in March (2024: March 10)
        # Fall back: First Sunday in November (2024: November 3)
        
        dst_spring_date = date(2024, 3, 10)  # Spring forward
        dst_fall_date = date(2024, 11, 3)    # Fall back
        
        for test_date in [dst_spring_date, dst_fall_date]:
            # Ensure it's a weekday
            while test_date.weekday() >= 5:
                test_date += timedelta(days=1)
            
            slots = calculate_available_slots(
                organizer=self.organizer,
                event_type=self.event_type,
                start_date=test_date,
                end_date=test_date,
                invitee_timezone='America/New_York'
            )
            
            # Should still have slots, and they should be properly timezone-aware
            if slots:
                for slot in slots:
                    self.assertIsNotNone(slot['start_time'].tzinfo)
                    self.assertIsNotNone(slot['end_time'].tzinfo)
                    if 'local_start_time' in slot:
                        self.assertIsNotNone(slot['local_start_time'].tzinfo)
    
    def test_minimum_gap_between_bookings(self):
        """Test minimum gap enforcement between bookings."""
        # Set minimum gap to 30 minutes
        buffer_settings, _ = BufferTime.objects.get_or_create(organizer=self.organizer)
        buffer_settings.minimum_gap = 30
        buffer_settings.save()
        
        tomorrow = timezone.now().date() + timedelta(days=1)
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        # Create an existing booking at 10 AM
        existing_booking_start = datetime.combine(tomorrow, time(10, 0)).replace(tzinfo=ZoneInfo('America/New_York'))
        
        Booking.objects.create(
            event_type=self.event_type,
            organizer=self.organizer,
            invitee_name='Test User',
            invitee_email='test@example.com',
            start_time=existing_booking_start.astimezone(timezone.utc),
            end_time=(existing_booking_start + timedelta(minutes=30)).astimezone(timezone.utc),
            status='confirmed'
        )
        
        slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='America/New_York'
        )
        
        # Check that the next available slot respects the minimum gap
        # Existing booking: 10:00-10:30, minimum gap: 30 min, so next slot should be at 11:00 or later
        slots_after_booking = [
            slot for slot in slots
            if slot['local_start_time'].time() > time(10, 30)
        ]
        
        if slots_after_booking:
            earliest_next_slot = min(slots_after_booking, key=lambda x: x['local_start_time'])
            self.assertGreaterEqual(
                earliest_next_slot['local_start_time'].time(), 
                time(11, 0),
                "Next slot should respect minimum gap"
            )
    
    def test_event_type_specific_rules(self):
        """Test that availability rules can be specific to event types."""
        # Create another event type
        consultation_event = EventType.objects.create(
            organizer=self.organizer,
            name='Consultation',
            duration=60
        )
        
        # Create a rule that only applies to consultations (weekends)
        weekend_rule = AvailabilityRule.objects.create(
            organizer=self.organizer,
            day_of_week=5,  # Saturday
            start_time=time(10, 0),
            end_time=time(14, 0),
            is_active=True
        )
        weekend_rule.event_types.add(consultation_event)
        
        # Find next Saturday
        saturday = timezone.now().date()
        while saturday.weekday() != 5:
            saturday += timedelta(days=1)
        
        # Regular meeting should have no slots on Saturday
        regular_slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=saturday,
            end_date=saturday,
            invitee_timezone='UTC'
        )
        self.assertEqual(len(regular_slots), 0, "Regular meetings should have no Saturday slots")
        
        # Consultation should have slots on Saturday
        consultation_slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=consultation_event,
            start_date=saturday,
            end_date=saturday,
            invitee_timezone='UTC'
        )
        self.assertGreater(len(consultation_slots), 0, "Consultations should have Saturday slots")
    
    def test_timezone_validation(self):
        """Test timezone validation utility."""
        # Valid timezones
        self.assertTrue(validate_timezone('UTC'))
        self.assertTrue(validate_timezone('America/New_York'))
        self.assertTrue(validate_timezone('Europe/London'))
        self.assertTrue(validate_timezone('Asia/Tokyo'))
        
        # Invalid timezones
        self.assertFalse(validate_timezone('Invalid/Timezone'))
        self.assertFalse(validate_timezone('EST'))  # Deprecated
        self.assertFalse(validate_timezone(''))
        self.assertFalse(validate_timezone(None))
    
    def test_timezone_offset_calculation(self):
        """Test timezone offset calculation."""
        # Test known offset
        offset = calculate_timezone_offset_hours('UTC', 'America/New_York')
        self.assertEqual(offset, -5.0, "NYC should be 5 hours behind UTC (standard time)")
        
        # Test same timezone
        offset = calculate_timezone_offset_hours('UTC', 'UTC')
        self.assertEqual(offset, 0.0, "Same timezone should have 0 offset")
    
    def test_slot_merging(self):
        """Test merging of overlapping or adjacent slots."""
        # Create test slots
        base_time = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        
        slots = [
            {
                'start_time': base_time,
                'end_time': base_time + timedelta(minutes=30),
                'duration_minutes': 30
            },
            {
                'start_time': base_time + timedelta(minutes=30),
                'end_time': base_time + timedelta(minutes=60),
                'duration_minutes': 30
            },
            {
                'start_time': base_time + timedelta(minutes=90),
                'end_time': base_time + timedelta(minutes=120),
                'duration_minutes': 30
            }
        ]
        
        merged = merge_overlapping_slots(slots)
        
        # Should merge first two slots, keep third separate
        self.assertEqual(len(merged), 2, "Should merge adjacent slots")
        self.assertEqual(merged[0]['duration_minutes'], 60, "Merged slot should have combined duration")
        self.assertEqual(merged[1]['duration_minutes'], 30, "Separate slot should remain unchanged")
    
    def test_buffer_time_priority(self):
        """Test that event-specific buffer times take priority over global defaults."""
        # Set global buffer times
        buffer_settings, _ = BufferTime.objects.get_or_create(organizer=self.organizer)
        buffer_settings.default_buffer_before = 15
        buffer_settings.default_buffer_after = 15
        buffer_settings.save()
        
        # Set event-specific buffer times
        self.event_type.buffer_time_before = 30
        self.event_type.buffer_time_after = 30
        self.event_type.save()
        
        tomorrow = timezone.now().date() + timedelta(days=1)
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        # Create an existing booking
        existing_booking_start = datetime.combine(tomorrow, time(12, 0)).replace(tzinfo=ZoneInfo('America/New_York'))
        
        Booking.objects.create(
            event_type=self.event_type,
            organizer=self.organizer,
            invitee_name='Test User',
            invitee_email='test@example.com',
            start_time=existing_booking_start.astimezone(timezone.utc),
            end_time=(existing_booking_start + timedelta(minutes=30)).astimezone(timezone.utc),
            status='confirmed'
        )
        
        slots = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='America/New_York'
        )
        
        # Check that slots respect the 30-minute buffer (not the 15-minute global default)
        # Existing booking: 12:00-12:30, with 30-min buffer before and after
        # So no slots should be available from 11:30-13:00
        
        conflicting_slots = [
            slot for slot in slots
            if (slot['local_start_time'].time() >= time(11, 30) and 
                slot['local_start_time'].time() < time(13, 0))
        ]
        
        self.assertEqual(len(conflicting_slots), 0, "Should respect event-specific buffer times")
    
    def test_invalid_timezone_handling(self):
        """Test handling of invalid timezones in multi-invitee requests."""
        tomorrow = timezone.now().date() + timedelta(days=1)
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        # Mix valid and invalid timezones
        invitee_timezones = ['America/New_York', 'Invalid/Timezone', 'Europe/London']
        
        result = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='America/New_York',
            invitee_timezones=invitee_timezones
        )
        
        # Should return warnings about invalid timezone
        self.assertIsInstance(result, dict)
        self.assertIn('warnings', result)
        self.assertTrue(any('Invalid/Timezone' in warning for warning in result['warnings']))
        
        # Should still process valid timezones
        slots = result['slots']
        if slots:
            first_slot = slots[0]
            if 'invitee_times' in first_slot:
                # Should only include valid timezones
                self.assertIn('America/New_York', first_slot['invitee_times'])
                self.assertIn('Europe/London', first_slot['invitee_times'])
                self.assertNotIn('Invalid/Timezone', first_slot['invitee_times'])
    
    def test_event_type_slot_interval_override(self):
        """Test that event type can override organizer's default slot interval."""
        # Set organizer's default slot interval to 30 minutes
        buffer_settings, _ = BufferTime.objects.get_or_create(organizer=self.organizer)
        buffer_settings.slot_interval_minutes = 30
        buffer_settings.save()
        
        # Set event type specific slot interval to 15 minutes
        self.event_type.slot_interval_minutes = 15
        self.event_type.save()
        
        tomorrow = timezone.now().date() + timedelta(days=1)
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        result = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='America/New_York'
        )
        
        slots = result['slots'] if isinstance(result, dict) else result
        
        # Should have more slots due to 15-minute intervals vs 30-minute
        self.assertGreater(len(slots), 0)
        
        # Check that slots are spaced 15 minutes apart (not 30)
        if len(slots) > 1:
            time_diff = slots[1]['start_time'] - slots[0]['start_time']
            self.assertEqual(time_diff.total_seconds() / 60, 15)
    
    def test_customizable_reasonable_hours(self):
        """Test customizable reasonable hours for multi-invitee scheduling."""
        # Set custom reasonable hours (10 AM - 6 PM)
        self.organizer.profile.reasonable_hours_start = 10
        self.organizer.profile.reasonable_hours_end = 18
        self.organizer.profile.save()
        
        tomorrow = timezone.now().date() + timedelta(days=1)
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        # Test with multiple timezones
        invitee_timezones = ['America/New_York', 'Europe/London']
        
        result = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='America/New_York',
            invitee_timezones=invitee_timezones
        )
        
        slots = result['slots'] if isinstance(result, dict) else result
        
        # Check that slots respect custom reasonable hours
        for slot in slots:
            if 'invitee_times' in slot:
                for tz, time_info in slot['invitee_times'].items():
                    local_hour = time_info['start_hour']
                    # Should be within custom reasonable hours (10-18)
                    self.assertGreaterEqual(local_hour, 10)
                    self.assertLessEqual(local_hour, 18)
    
    def test_performance_metrics_in_response(self):
        """Test that performance metrics are included in calculation results."""
        tomorrow = timezone.now().date() + timedelta(days=1)
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        
        result = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='UTC'
        )
        
        # Should return dict with performance metrics
        self.assertIsInstance(result, dict)
        self.assertIn('slots', result)
        self.assertIn('warnings', result)
        self.assertIn('performance_metrics', result)
        
        # Performance metrics should include duration
        metrics = result['performance_metrics']
        self.assertIn('duration', metrics)
        self.assertIsInstance(metrics['duration'], float)
    
    def test_zero_duration_slots(self):
        """Test handling of zero-duration availability rules."""
        # Create a rule with same start and end time
        zero_rule = AvailabilityRule.objects.create(
            organizer=self.organizer,
            day_of_week=0,  # Monday
            start_time=time(10, 0),
            end_time=time(10, 0),  # Same as start time
            is_active=True
        )
        
        tomorrow = timezone.now().date() + timedelta(days=1)
        while tomorrow.weekday() != 0:  # Find next Monday
            tomorrow += timedelta(days=1)
        
        result = calculate_available_slots(
            organizer=self.organizer,
            event_type=self.event_type,
            start_date=tomorrow,
            end_date=tomorrow,
            invitee_timezone='UTC'
        )
        
        slots = result['slots'] if isinstance(result, dict) else result
        
        # Should not generate any slots from zero-duration rule
        # (but other rules might still generate slots)
        for slot in slots:
            # Ensure no slot has zero duration
            self.assertGreater(slot['duration_minutes'], 0)


class AvailabilityAPITestCase(TestCase):
    """Test suite for availability API endpoints."""
    
    def setUp(self):
        """Set up test data."""
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True,
            is_email_verified=True,
            account_status='active'
        )
        
        from apps.users.models import Profile
        Profile.objects.create(
            user=self.organizer,
            organizer_slug='test-organizer',
            timezone_name='America/New_York'
        )
        
        self.event_type = EventType.objects.create(
            organizer=self.organizer,
            name='Test Meeting',
            event_type_slug='test-meeting',
            duration=30
        )
    
    def test_calculated_slots_api_validation(self):
        """Test API parameter validation."""
        from django.urls import reverse
        
        url = reverse('availability:calculated-slots', kwargs={'organizer_slug': 'test-organizer'})
        
        # Test missing required parameters
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)
        
        # Test invalid date format
        response = self.client.get(url, {
            'event_type_slug': 'test-meeting',
            'start_date': 'invalid-date',
            'end_date': '2024-01-02'
        })
        self.assertEqual(response.status_code, 400)
        
        # Test invalid timezone
        tomorrow = timezone.now().date() + timedelta(days=1)
        response = self.client.get(url, {
            'event_type_slug': 'test-meeting',
            'start_date': tomorrow.isoformat(),
            'end_date': tomorrow.isoformat(),
            'invitee_timezone': 'Invalid/Timezone'
        })
        self.assertEqual(response.status_code, 400)
        
        # Test valid request
        response = self.client.get(url, {
            'event_type_slug': 'test-meeting',
            'start_date': tomorrow.isoformat(),
            'end_date': tomorrow.isoformat(),
            'invitee_timezone': 'UTC'
        })
        self.assertEqual(response.status_code, 200)
        
        # Check response structure
        data = response.json()
        self.assertIn('available_slots', data)
        self.assertIn('cache_hit', data)
        self.assertIn('total_slots', data)


class AvailabilityModelTestCase(TestCase):
    """Test suite for availability models."""
    
    def setUp(self):
        """Set up test data."""
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
    
    def test_availability_rule_midnight_spanning(self):
        """Test midnight spanning detection for availability rules."""
        # Normal rule
        normal_rule = AvailabilityRule.objects.create(
            organizer=self.organizer,
            day_of_week=0,
            start_time=time(9, 0),
            end_time=time(17, 0)
        )
        self.assertFalse(normal_rule.spans_midnight())
        
        # Midnight spanning rule
        night_rule = AvailabilityRule.objects.create(
            organizer=self.organizer,
            day_of_week=1,
            start_time=time(22, 0),
            end_time=time(6, 0)
        )
        self.assertTrue(night_rule.spans_midnight())
    
    def test_recurring_blocked_time_date_application(self):
        """Test recurring blocked time date range application."""
        # Create recurring block with date range
        start_date = date(2024, 1, 1)
        end_date = date(2024, 12, 31)
        
        recurring_block = RecurringBlockedTime.objects.create(
            organizer=self.organizer,
            name='Weekly Meeting',
            day_of_week=0,  # Monday
            start_time=time(14, 0),
            end_time=time(15, 0),
            start_date=start_date,
            end_date=end_date
        )
        
        # Test dates within range
        test_monday_in_range = date(2024, 6, 3)  # A Monday in 2024
        self.assertTrue(recurring_block.applies_to_date(test_monday_in_range))
        
        # Test date outside range
        test_monday_outside = date(2025, 1, 6)  # A Monday in 2025
        self.assertFalse(recurring_block.applies_to_date(test_monday_outside))
        
        # Test wrong day of week
        test_tuesday = date(2024, 6, 4)  # A Tuesday in 2024
        self.assertFalse(recurring_block.applies_to_date(test_tuesday))
    
    def test_date_override_validation(self):
        """Test date override rule validation."""
        from .serializers import DateOverrideRuleSerializer
        
        # Valid override with availability
        valid_data = {
            'date': '2024-06-01',
            'is_available': True,
            'start_time': '09:00:00',
            'end_time': '17:00:00',
            'reason': 'Special hours'
        }
        
        serializer = DateOverrideRuleSerializer(data=valid_data)
        self.assertTrue(serializer.is_valid())
        
        # Invalid: available but no times specified
        invalid_data = {
            'date': '2024-06-01',
            'is_available': True,
            'reason': 'Missing times'
        }
        
        serializer = DateOverrideRuleSerializer(data=invalid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn('start_time and end_time are required', str(serializer.errors))