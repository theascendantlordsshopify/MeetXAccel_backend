"""
Enterprise-grade utility functions for events and booking system.
"""
import logging
from datetime import datetime, timedelta, time
from django.utils import timezone
from django.db import models, transaction
from django.core.cache import cache
from django.conf import settings
from zoneinfo import ZoneInfo
import time as time_module
from .models import Booking, EventType, Attendee, WaitlistEntry, BookingAuditLog, EventTypeAvailabilityCache

logger = logging.getLogger(__name__)


class AvailabilityCalculator:
    """Enterprise-grade availability calculation engine."""
    
    def __init__(self, organizer, event_type, invitee_timezone='UTC'):
        self.organizer = organizer
        self.event_type = event_type
        self.invitee_timezone = invitee_timezone
        self.organizer_timezone = organizer.profile.timezone_name
        
        # Performance tracking
        self.computation_start = None
        self.cache_hits = 0
        self.cache_misses = 0
    
    def get_available_slots(self, start_date, end_date, attendee_count=1, use_cache=True):
        """
        Calculate available time slots with comprehensive conflict resolution.
        
        Args:
            start_date: Start date for availability search
            end_date: End date for availability search
            attendee_count: Number of attendees for group events
            use_cache: Whether to use cached results
        
        Returns:
            dict: Available slots with metadata
        """
        self.computation_start = time_module.time()
        
        try:
            # Check cache first
            if use_cache:
                cached_result = self._get_cached_availability(start_date, end_date, attendee_count)
                if cached_result:
                    self.cache_hits += 1
                    return self._add_performance_metadata(cached_result)
            
            self.cache_misses += 1
            
            # Calculate fresh availability
            available_slots = self._calculate_fresh_availability(start_date, end_date, attendee_count)
            
            # Cache the result
            if use_cache:
                self._cache_availability(start_date, end_date, attendee_count, available_slots)
            
            return self._add_performance_metadata({
                'slots': available_slots,
                'cache_hit': False,
                'total_slots': len(available_slots)
            })
            
        except Exception as e:
            logger.error(f"Error calculating availability: {str(e)}")
            return {
                'slots': [],
                'cache_hit': False,
                'total_slots': 0,
                'error': str(e),
                'performance_metrics': self._get_performance_metrics()
            }
    
    def _calculate_fresh_availability(self, start_date, end_date, attendee_count):
        """Calculate availability without cache."""
        available_slots = []
        current_date = start_date
        
        while current_date <= end_date:
            # Skip if event type can't be booked on this date
            if not self.event_type.can_book_on_date(current_date):
                current_date += timedelta(days=1)
                continue
            
            # Get base availability for this day
            day_slots = self._get_day_availability(current_date, attendee_count)
            available_slots.extend(day_slots)
            
            current_date += timedelta(days=1)
        
        # Sort slots by start time
        available_slots.sort(key=lambda x: x['start_time'])
        
        return available_slots
    
    def _get_day_availability(self, date, attendee_count):
        """Get availability for a specific day."""
        day_of_week = date.weekday()  # 0=Monday, 6=Sunday
        
        # Get organizer's availability rules for this day
        from apps.availability.models import AvailabilityRule
        availability_rules = AvailabilityRule.objects.filter(
            organizer=self.organizer,
            day_of_week=day_of_week,
            is_active=True
        ).filter(
            models.Q(event_types__isnull=True) | models.Q(event_types=self.event_type)
        )
        
        if not availability_rules.exists():
            return []  # No availability on this day
        
        # Check for date overrides
        from apps.availability.models import DateOverrideRule
        date_override = DateOverrideRule.objects.filter(
            organizer=self.organizer,
            date=date,
            is_active=True
        ).filter(
            models.Q(event_types__isnull=True) | models.Q(event_types=self.event_type)
        ).first()
        
        if date_override:
            if not date_override.is_available:
                return []  # Entire day blocked
            else:
                # Use override times
                return self._generate_slots_for_time_range(
                    date, date_override.start_time, date_override.end_time, attendee_count
                )
        
        # Generate slots from availability rules
        day_slots = []
        for rule in availability_rules:
            rule_slots = self._generate_slots_for_availability_rule(rule, date, attendee_count)
            day_slots.extend(rule_slots)
        
        # Remove overlapping slots and merge adjacent ones
        return self._merge_and_deduplicate_slots(day_slots)
    
    def _generate_slots_for_availability_rule(self, rule, date, attendee_count):
        """Generate slots for a specific availability rule."""
        if rule.spans_midnight():
            # Handle midnight-spanning rules
            slots = []
            
            # Part 1: start_time to midnight
            slots.extend(self._generate_slots_for_time_range(
                date, rule.start_time, time(23, 59, 59), attendee_count
            ))
            
            # Part 2: midnight to end_time (next day)
            next_date = date + timedelta(days=1)
            slots.extend(self._generate_slots_for_time_range(
                next_date, time(0, 0), rule.end_time, attendee_count
            ))
            
            return slots
        else:
            # Normal rule within same day
            return self._generate_slots_for_time_range(
                date, rule.start_time, rule.end_time, attendee_count
            )
    
    def _generate_slots_for_time_range(self, date, start_time, end_time, attendee_count):
        """Generate slots for a specific time range on a date."""
        slots = []
        
        # Create timezone-aware datetime objects
        org_tz = ZoneInfo(self.organizer_timezone)
        
        range_start = datetime.combine(date, start_time).replace(tzinfo=org_tz)
        range_end = datetime.combine(date, end_time).replace(tzinfo=org_tz)
        
        # Convert to UTC for calculations
        range_start_utc = range_start.astimezone(timezone.utc)
        range_end_utc = range_end.astimezone(timezone.utc)
        
        # Get slot parameters
        slot_duration = timedelta(minutes=self.event_type.duration)
        buffer_before = timedelta(minutes=self.event_type.buffer_time_before)
        buffer_after = timedelta(minutes=self.event_type.buffer_time_after)
        
        # Get slot interval
        slot_interval = self._get_slot_interval()
        
        # Generate slots
        current_slot_start = range_start_utc
        
        while current_slot_start + slot_duration <= range_end_utc:
            slot_end = current_slot_start + slot_duration
            
            # Check all conflict types
            if self._is_slot_available(current_slot_start, slot_end, attendee_count, buffer_before, buffer_after):
                # Convert to invitee timezone for display
                invitee_tz = ZoneInfo(self.invitee_timezone)
                
                slot = {
                    'start_time': current_slot_start,
                    'end_time': slot_end,
                    'duration_minutes': self.event_type.duration,
                    'local_start_time': current_slot_start.astimezone(invitee_tz),
                    'local_end_time': slot_end.astimezone(invitee_tz),
                    'attendee_count': attendee_count,
                    'available_spots': self._get_available_spots(current_slot_start, slot_end),
                }
                
                slots.append(slot)
            
            current_slot_start += slot_interval
        
        return slots
    
    def _is_slot_available(self, start_time, end_time, attendee_count, buffer_before, buffer_after):
        """Check if a slot is available considering all constraints."""
        # Apply buffers for conflict checking
        buffered_start = start_time - buffer_before
        buffered_end = end_time + buffer_after
        
        # Check minimum scheduling notice
        min_notice = timedelta(minutes=self.event_type.min_scheduling_notice)
        if start_time < timezone.now() + min_notice:
            return False
        
        # Check maximum scheduling horizon
        max_horizon = timedelta(minutes=self.event_type.max_scheduling_horizon)
        if start_time > timezone.now() + max_horizon:
            return False
        
        # Check blocked times
        if self._is_blocked_by_blocked_times(buffered_start, buffered_end):
            return False
        
        # Check recurring blocked times
        if self._is_blocked_by_recurring_blocks(buffered_start, buffered_end):
            return False
        
        # Check existing bookings (cross-event-type)
        if self._is_blocked_by_existing_bookings(buffered_start, buffered_end, attendee_count):
            return False
        
        # Check external calendar conflicts
        if self._is_blocked_by_external_calendars(buffered_start, buffered_end):
            return False
        
        # Check daily booking limits
        if self._exceeds_daily_booking_limit(start_time):
            return False
        
        return True
    
    def _is_blocked_by_blocked_times(self, start_time, end_time):
        """Check conflicts with blocked times."""
        from apps.availability.models import BlockedTime
        
        return BlockedTime.objects.filter(
            organizer=self.organizer,
            is_active=True,
            start_datetime__lt=end_time,
            end_datetime__gt=start_time
        ).exists()
    
    def _is_blocked_by_recurring_blocks(self, start_time, end_time):
        """Check conflicts with recurring blocked times."""
        from apps.availability.models import RecurringBlockedTime
        
        # Get organizer timezone for date calculations
        org_tz = ZoneInfo(self.organizer_timezone)
        local_start = start_time.astimezone(org_tz)
        local_date = local_start.date()
        day_of_week = local_date.weekday()
        
        recurring_blocks = RecurringBlockedTime.objects.filter(
            organizer=self.organizer,
            day_of_week=day_of_week,
            is_active=True
        )
        
        for block in recurring_blocks:
            if block.applies_to_date(local_date):
                # Convert block times to UTC for comparison
                if block.spans_midnight():
                    # Handle midnight-spanning blocks
                    # Part 1: start_time to midnight
                    block_start_1 = datetime.combine(local_date, block.start_time).replace(tzinfo=org_tz)
                    block_end_1 = datetime.combine(local_date, time(23, 59, 59)).replace(tzinfo=org_tz)
                    
                    if (start_time < block_end_1.astimezone(timezone.utc) and 
                        end_time > block_start_1.astimezone(timezone.utc)):
                        return True
                    
                    # Part 2: midnight to end_time (next day)
                    next_date = local_date + timedelta(days=1)
                    block_start_2 = datetime.combine(next_date, time(0, 0)).replace(tzinfo=org_tz)
                    block_end_2 = datetime.combine(next_date, block.end_time).replace(tzinfo=org_tz)
                    
                    if (start_time < block_end_2.astimezone(timezone.utc) and 
                        end_time > block_start_2.astimezone(timezone.utc)):
                        return True
                else:
                    # Normal block within same day
                    block_start = datetime.combine(local_date, block.start_time).replace(tzinfo=org_tz)
                    block_end = datetime.combine(local_date, block.end_time).replace(tzinfo=org_tz)
                    
                    if (start_time < block_end.astimezone(timezone.utc) and 
                        end_time > block_start.astimezone(timezone.utc)):
                        return True
        
        return False
    
    def _is_blocked_by_existing_bookings(self, start_time, end_time, attendee_count):
        """Check conflicts with existing bookings across ALL event types."""
        # Get ALL confirmed bookings for this organizer in the time range
        existing_bookings = Booking.objects.filter(
            organizer=self.organizer,
            status='confirmed',
            start_time__lt=end_time,
            end_time__gt=start_time
        )
        
        for booking in existing_bookings:
            # Apply booking's own buffer times
            booking_buffer_before = timedelta(minutes=booking.event_type.buffer_time_before)
            booking_buffer_after = timedelta(minutes=booking.event_type.buffer_time_after)
            
            buffered_booking_start = booking.start_time - booking_buffer_before
            buffered_booking_end = booking.end_time + booking_buffer_after
            
            # Check for overlap
            if start_time < buffered_booking_end and end_time > buffered_booking_start:
                # For group events, check capacity
                if (booking.event_type.is_group_event() and 
                    booking.event_type.id == self.event_type.id):
                    
                    current_attendees = booking.attendees.filter(status='confirmed').count()
                    if current_attendees + attendee_count <= self.event_type.max_attendees:
                        continue  # Slot still has capacity
                
                return True  # Conflict found
        
        return False
    
    def _is_blocked_by_external_calendars(self, start_time, end_time):
        """Check conflicts with external calendar events."""
        try:
            from apps.integrations.models import CalendarIntegration
            from apps.integrations.utils import get_external_busy_times
            
            # Get active calendar integrations
            calendar_integrations = CalendarIntegration.objects.filter(
                organizer=self.organizer,
                is_active=True,
                sync_enabled=True
            )
            
            for integration in calendar_integrations:
                try:
                    busy_times = get_external_busy_times(
                        integration, 
                        start_time.date(), 
                        end_time.date()
                    )
                    
                    for busy_period in busy_times:
                        if (start_time < busy_period['end_time'] and 
                            end_time > busy_period['start_time']):
                            return True
                            
                except Exception as e:
                    logger.warning(f"Error checking external calendar {integration.provider}: {str(e)}")
                    # Continue checking other integrations
                    continue
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking external calendars: {str(e)}")
            # Fail safe - assume no conflicts if we can't check
            return False
    
    def _exceeds_daily_booking_limit(self, start_time):
        """Check if booking would exceed daily limits."""
        if not self.event_type.max_bookings_per_day:
            return False
        
        # Count existing bookings for this event type on this date
        booking_date = start_time.astimezone(ZoneInfo(self.organizer_timezone)).date()
        
        existing_count = Booking.objects.filter(
            organizer=self.organizer,
            event_type=self.event_type,
            status='confirmed',
            start_time__date=booking_date
        ).count()
        
        return existing_count >= self.event_type.max_bookings_per_day
    
    def _get_available_spots(self, start_time, end_time):
        """Get number of available spots for group events."""
        if not self.event_type.is_group_event():
            return 1
        
        # Find existing booking at this exact time
        existing_booking = Booking.objects.filter(
            organizer=self.organizer,
            event_type=self.event_type,
            status='confirmed',
            start_time=start_time,
            end_time=end_time
        ).first()
        
        if existing_booking:
            current_attendees = existing_booking.attendees.filter(status='confirmed').count()
            return max(0, self.event_type.max_attendees - current_attendees)
        
        return self.event_type.max_attendees
    
    def _get_slot_interval(self):
        """Get slot interval for this event type."""
        if self.event_type.slot_interval_minutes > 0:
            return timedelta(minutes=self.event_type.slot_interval_minutes)
        
        # Use organizer's default or system default
        from apps.availability.models import BufferTime
        buffer_settings, _ = BufferTime.objects.get_or_create(organizer=self.organizer)
        return timedelta(minutes=getattr(buffer_settings, 'slot_interval_minutes', 15))
    
    def _merge_and_deduplicate_slots(self, slots):
        """Merge overlapping slots and remove duplicates."""
        if not slots:
            return slots
        
        # Sort by start time
        sorted_slots = sorted(slots, key=lambda x: x['start_time'])
        merged_slots = []
        
        current_slot = sorted_slots[0].copy()
        
        for next_slot in sorted_slots[1:]:
            # Check if slots are adjacent or overlapping
            if current_slot['end_time'] >= next_slot['start_time']:
                # Merge slots
                current_slot['end_time'] = max(current_slot['end_time'], next_slot['end_time'])
                # Update duration
                duration_minutes = int((current_slot['end_time'] - current_slot['start_time']).total_seconds() / 60)
                current_slot['duration_minutes'] = duration_minutes
                
                # Update local times
                if 'local_end_time' in current_slot:
                    invitee_tz = ZoneInfo(self.invitee_timezone)
                    current_slot['local_end_time'] = current_slot['end_time'].astimezone(invitee_tz)
            else:
                # No overlap, add current slot and move to next
                merged_slots.append(current_slot)
                current_slot = next_slot.copy()
        
        # Add the last slot
        merged_slots.append(current_slot)
        
        return merged_slots
    
    def _get_cached_availability(self, start_date, end_date, attendee_count):
        """Get cached availability if available and valid."""
        # For now, only cache single-day requests for performance
        if start_date != end_date:
            return None
        
        try:
            cache_entry = EventTypeAvailabilityCache.objects.get(
                organizer=self.organizer,
                event_type=self.event_type,
                date=start_date,
                timezone_name=self.invitee_timezone,
                attendee_count=attendee_count,
                is_dirty=False
            )
            
            if not cache_entry.is_expired():
                return {
                    'slots': cache_entry.available_slots,
                    'cache_hit': True,
                    'total_slots': len(cache_entry.available_slots),
                    'cached_at': cache_entry.computed_at
                }
        
        except EventTypeAvailabilityCache.DoesNotExist:
            pass
        
        return None
    
    def _cache_availability(self, start_date, end_date, attendee_count, slots):
        """Cache availability results."""
        # Only cache single-day results
        if start_date != end_date:
            return
        
        computation_time = int((time_module.time() - self.computation_start) * 1000)
        
        # Create or update cache entry
        cache_entry, created = EventTypeAvailabilityCache.objects.update_or_create(
            organizer=self.organizer,
            event_type=self.event_type,
            date=start_date,
            timezone_name=self.invitee_timezone,
            attendee_count=attendee_count,
            defaults={
                'available_slots': slots,
                'expires_at': timezone.now() + timedelta(hours=1),  # Cache for 1 hour
                'is_dirty': False,
                'computation_time_ms': computation_time
            }
        )
    
    def _add_performance_metadata(self, result):
        """Add performance metadata to result."""
        result['performance_metrics'] = self._get_performance_metrics()
        return result
    
    def _get_performance_metrics(self):
        """Get performance metrics for this calculation."""
        total_time = time_module.time() - self.computation_start if self.computation_start else 0
        
        return {
            'computation_time_ms': round(total_time * 1000, 2),
            'cache_hits': self.cache_hits,
            'cache_misses': self.cache_misses,
            'organizer_timezone': self.organizer_timezone,
            'invitee_timezone': self.invitee_timezone
        }


def get_available_time_slots(organizer, event_type, start_date, end_date, 
                           invitee_timezone='UTC', attendee_count=1, use_cache=True):
    """
    Main function to get available time slots.
    
    Args:
        organizer: User instance (organizer)
        event_type: EventType instance
        start_date: Start date for availability search
        end_date: End date for availability search
        invitee_timezone: IANA timezone string for the invitee
        attendee_count: Number of attendees for group events
        use_cache: Whether to use cached results
    
    Returns:
        dict: Available slots with metadata
    """
    calculator = AvailabilityCalculator(organizer, event_type, invitee_timezone)
    return calculator.get_available_slots(start_date, end_date, attendee_count, use_cache)


def create_booking_with_validation(event_type, organizer, booking_data, custom_answers=None):
    """
    Create a booking with comprehensive validation and conflict checking.
    
    Args:
        event_type: EventType instance
        organizer: User instance
        booking_data: Dictionary with booking details
        custom_answers: Dictionary with custom question answers
    
    Returns:
        tuple: (booking, created, errors)
    """
    errors = []
    
    try:
        with transaction.atomic():
            # Extract booking details
            start_time = booking_data['start_time']
            attendee_count = booking_data.get('attendee_count', 1)
            
            # Calculate end time
            end_time = start_time + timedelta(minutes=event_type.duration)
            
            # Final availability check (race condition prevention)
            calculator = AvailabilityCalculator(organizer, event_type)
            if not calculator._is_slot_available(
                start_time, end_time, attendee_count,
                timedelta(minutes=event_type.buffer_time_before),
                timedelta(minutes=event_type.buffer_time_after)
            ):
                errors.append("Selected time slot is no longer available")
                return None, False, errors
            
            # Validate custom answers
            if custom_answers:
                validation_errors = validate_custom_answers(event_type, custom_answers)
                if validation_errors:
                    errors.extend(validation_errors)
                    return None, False, errors
            
            # Check for existing group booking at this time
            existing_booking = None
            if event_type.is_group_event():
                existing_booking = Booking.objects.filter(
                    organizer=organizer,
                    event_type=event_type,
                    start_time=start_time,
                    end_time=end_time,
                    status='confirmed'
                ).first()
            
            if existing_booking:
                # Add to existing group booking
                attendee = Attendee.objects.create(
                    booking=existing_booking,
                    name=booking_data['invitee_name'],
                    email=booking_data['invitee_email'],
                    phone=booking_data.get('invitee_phone', ''),
                    custom_answers=custom_answers or {}
                )
                
                # Update booking attendee count
                existing_booking.attendee_count = existing_booking.attendees.filter(
                    status='confirmed'
                ).count()
                existing_booking.save(update_fields=['attendee_count'])
                
                # Create audit log
                create_booking_audit_log(
                    booking=existing_booking,
                    action='attendee_added',
                    description=f"Added attendee {attendee.name} to group booking",
                    actor_type='invitee',
                    actor_email=attendee.email,
                    actor_name=attendee.name,
                    metadata={'attendee_id': str(attendee.id)}
                )
                
                return existing_booking, False, []
            else:
                # Create new booking
                booking = Booking.objects.create(
                    event_type=event_type,
                    organizer=organizer,
                    invitee_name=booking_data['invitee_name'],
                    invitee_email=booking_data['invitee_email'],
                    invitee_phone=booking_data.get('invitee_phone', ''),
                    invitee_timezone=booking_data.get('invitee_timezone', 'UTC'),
                    start_time=start_time,
                    end_time=end_time,
                    attendee_count=attendee_count,
                    custom_answers=custom_answers or {}
                )
                
                # Create primary attendee for group events
                if event_type.is_group_event():
                    Attendee.objects.create(
                        booking=booking,
                        name=booking_data['invitee_name'],
                        email=booking_data['invitee_email'],
                        phone=booking_data.get('invitee_phone', ''),
                        custom_answers=custom_answers or {}
                    )
                
                # Create audit log
                create_booking_audit_log(
                    booking=booking,
                    action='booking_created',
                    description=f"Booking created by {booking.invitee_name}",
                    actor_type='invitee',
                    actor_email=booking.invitee_email,
                    actor_name=booking.invitee_name,
                    metadata={
                        'attendee_count': attendee_count,
                        'custom_answers': custom_answers or {}
                    }
                )
                
                return booking, True, []
                
    except Exception as e:
        logger.error(f"Error creating booking: {str(e)}")
        errors.append(f"Failed to create booking: {str(e)}")
        return None, False, errors


def validate_custom_answers(event_type, custom_answers):
    """
    Validate custom question answers with conditional logic.
    
    Args:
        event_type: EventType instance
        custom_answers: Dictionary of answers
    
    Returns:
        list: Validation errors
    """
    errors = []
    
    # Get custom questions for this event type
    questions = event_type.questions.filter(is_active=True).order_by('order')
    
    for question in questions:
        question_key = str(question.id)
        answer = custom_answers.get(question_key)
        
        # Check if question should be shown based on conditions
        if not question.should_show_for_answers(custom_answers):
            continue
        
        # Check if required question is answered
        if question.is_required and not answer:
            errors.append(f"Question '{question.question_text}' is required")
            continue
        
        # Validate answer format based on question type
        if answer:
            validation_error = validate_answer_format(question, answer)
            if validation_error:
                errors.append(validation_error)
    
    return errors


def validate_answer_format(question, answer):
    """Validate answer format based on question type."""
    import re
    from datetime import datetime
    
    try:
        if question.question_type == 'email':
            if not re.match(r'^[^@]+@[^@]+\.[^@]+$', answer):
                return f"Invalid email format for '{question.question_text}'"
        
        elif question.question_type == 'phone':
            # Basic phone validation
            digits_only = re.sub(r'\D', '', answer)
            if len(digits_only) < 10:
                return f"Invalid phone number for '{question.question_text}'"
        
        elif question.question_type == 'number':
            try:
                float(answer)
            except ValueError:
                return f"Invalid number format for '{question.question_text}'"
        
        elif question.question_type == 'date':
            try:
                datetime.strptime(answer, '%Y-%m-%d')
            except ValueError:
                return f"Invalid date format for '{question.question_text}' (use YYYY-MM-DD)"
        
        elif question.question_type == 'url':
            if not answer.startswith(('http://', 'https://')):
                return f"Invalid URL format for '{question.question_text}'"
        
        elif question.question_type in ['select', 'radio']:
            if answer not in question.options:
                return f"Invalid option selected for '{question.question_text}'"
        
        elif question.question_type == 'multiselect':
            if isinstance(answer, list):
                for option in answer:
                    if option not in question.options:
                        return f"Invalid option '{option}' for '{question.question_text}'"
            else:
                return f"Multiple select answers must be a list for '{question.question_text}'"
        
        # Apply validation rules if present
        if question.validation_rules:
            validation_error = apply_validation_rules(question, answer)
            if validation_error:
                return validation_error
        
        return None
        
    except Exception as e:
        logger.error(f"Error validating answer for question {question.id}: {str(e)}")
        return f"Validation error for '{question.question_text}'"


def apply_validation_rules(question, answer):
    """Apply custom validation rules to an answer."""
    rules = question.validation_rules
    
    if 'min_length' in rules:
        if len(str(answer)) < rules['min_length']:
            return f"Answer for '{question.question_text}' is too short (minimum {rules['min_length']} characters)"
    
    if 'max_length' in rules:
        if len(str(answer)) > rules['max_length']:
            return f"Answer for '{question.question_text}' is too long (maximum {rules['max_length']} characters)"
    
    if 'pattern' in rules:
        import re
        if not re.match(rules['pattern'], str(answer)):
            return f"Answer for '{question.question_text}' doesn't match required format"
    
    if 'min_value' in rules and question.question_type == 'number':
        try:
            if float(answer) < rules['min_value']:
                return f"Value for '{question.question_text}' is too small (minimum {rules['min_value']})"
        except ValueError:
            pass
    
    if 'max_value' in rules and question.question_type == 'number':
        try:
            if float(answer) > rules['max_value']:
                return f"Value for '{question.question_text}' is too large (maximum {rules['max_value']})"
        except ValueError:
            pass
    
    return None


def create_booking_audit_log(booking, action, description, actor_type='system', 
                           actor_email='', actor_name='', ip_address=None, 
                           user_agent='', metadata=None, old_values=None, new_values=None):
    """
    Create a comprehensive audit log entry for booking actions.
    
    Args:
        booking: Booking instance
        action: Action type from BookingAuditLog.ACTION_CHOICES
        description: Human-readable description
        actor_type: Type of actor performing the action
        actor_email: Email of the actor
        actor_name: Name of the actor
        ip_address: IP address of the request
        user_agent: User agent string
        metadata: Additional metadata
        old_values: Previous values (for updates)
        new_values: New values (for updates)
    
    Returns:
        BookingAuditLog: Created audit log entry
    """
    return BookingAuditLog.objects.create(
        booking=booking,
        action=action,
        description=description,
        actor_type=actor_type,
        actor_email=actor_email or '',
        actor_name=actor_name or '',
        ip_address=ip_address,
        user_agent=user_agent or '',
        metadata=metadata or {},
        old_values=old_values or {},
        new_values=new_values or {}
    )


def handle_booking_cancellation(booking, cancelled_by='invitee', reason='', 
                              ip_address=None, user_agent=''):
    """
    Handle booking cancellation with all side effects.
    
    Args:
        booking: Booking instance to cancel
        cancelled_by: Who cancelled the booking
        reason: Cancellation reason
        ip_address: IP address of the request
        user_agent: User agent string
    
    Returns:
        tuple: (success, errors)
    """
    errors = []
    
    try:
        with transaction.atomic():
            # Validate cancellation is allowed
            if not booking.can_be_cancelled():
                errors.append("Booking cannot be cancelled at this time")
                return False, errors
            
            # Store old values for audit
            old_values = {
                'status': booking.status,
                'cancelled_at': booking.cancelled_at,
                'cancelled_by': booking.cancelled_by,
                'cancellation_reason': booking.cancellation_reason
            }
            
            # Cancel the booking
            booking.cancel(cancelled_by, reason)
            
            # Create audit log
            create_booking_audit_log(
                booking=booking,
                action='booking_cancelled',
                description=f"Booking cancelled by {cancelled_by}: {reason}",
                actor_type=cancelled_by,
                actor_email=booking.invitee_email if cancelled_by == 'invitee' else booking.organizer.email,
                actor_name=booking.invitee_name if cancelled_by == 'invitee' else booking.organizer.get_full_name(),
                ip_address=ip_address,
                user_agent=user_agent,
                metadata={'reason': reason},
                old_values=old_values,
                new_values={
                    'status': booking.status,
                    'cancelled_at': booking.cancelled_at,
                    'cancelled_by': booking.cancelled_by,
                    'cancellation_reason': booking.cancellation_reason
                }
            )
            
            # Invalidate cache
            invalidate_availability_cache(booking.organizer, booking.start_time.date())
            
            # Check waitlist for this time slot
            process_waitlist_for_cancelled_booking.delay(booking.id)
            
            return True, []
            
    except Exception as e:
        logger.error(f"Error cancelling booking {booking.id}: {str(e)}")
        errors.append(f"Failed to cancel booking: {str(e)}")
        return False, errors


def handle_booking_rescheduling(booking, new_start_time, ip_address=None, user_agent=''):
    """
    Handle booking rescheduling with validation and side effects.
    
    Args:
        booking: Booking instance to reschedule
        new_start_time: New start time (timezone-aware datetime)
        ip_address: IP address of the request
        user_agent: User agent string
    
    Returns:
        tuple: (success, errors)
    """
    errors = []
    
    try:
        with transaction.atomic():
            # Validate rescheduling is allowed
            if not booking.can_be_rescheduled():
                errors.append("Booking cannot be rescheduled at this time")
                return False, errors
            
            # Calculate new end time
            new_end_time = new_start_time + timedelta(minutes=booking.event_type.duration)
            
            # Check if new slot is available
            calculator = AvailabilityCalculator(booking.organizer, booking.event_type)
            if not calculator._is_slot_available(
                new_start_time, new_end_time, booking.attendee_count,
                timedelta(minutes=booking.event_type.buffer_time_before),
                timedelta(minutes=booking.event_type.buffer_time_after)
            ):
                errors.append("New time slot is not available")
                return False, errors
            
            # Store old values for audit
            old_values = {
                'start_time': booking.start_time,
                'end_time': booking.end_time,
                'status': booking.status
            }
            
            # Update booking
            booking.start_time = new_start_time
            booking.end_time = new_end_time
            booking.status = 'rescheduled'
            booking.rescheduled_at = timezone.now()
            booking.save(update_fields=[
                'start_time', 'end_time', 'status', 'rescheduled_at'
            ])
            
            # Create audit log
            create_booking_audit_log(
                booking=booking,
                action='booking_rescheduled',
                description=f"Booking rescheduled from {old_values['start_time']} to {new_start_time}",
                actor_type='invitee',
                actor_email=booking.invitee_email,
                actor_name=booking.invitee_name,
                ip_address=ip_address,
                user_agent=user_agent,
                old_values=old_values,
                new_values={
                    'start_time': booking.start_time,
                    'end_time': booking.end_time,
                    'status': booking.status
                }
            )
            
            # Invalidate cache for both old and new dates
            invalidate_availability_cache(booking.organizer, old_values['start_time'].date())
            invalidate_availability_cache(booking.organizer, new_start_time.date())
            
            return True, []
            
    except Exception as e:
        logger.error(f"Error rescheduling booking {booking.id}: {str(e)}")
        errors.append(f"Failed to reschedule booking: {str(e)}")
        return False, errors


def invalidate_availability_cache(organizer, date=None):
    """
    Invalidate availability cache for an organizer.
    
    Args:
        organizer: User instance
        date: Specific date to invalidate (None for all)
    """
    try:
        queryset = EventTypeAvailabilityCache.objects.filter(organizer=organizer)
        
        if date:
            queryset = queryset.filter(date=date)
        
        # Mark as dirty instead of deleting for better performance
        queryset.update(is_dirty=True)
        
        # Also clear Django cache
        if date:
            cache_pattern = f"availability:{organizer.id}:*:{date}:*"
        else:
            cache_pattern = f"availability:{organizer.id}:*"
        
        # Clear cache entries (Redis pattern matching would be ideal here)
        # For now, we'll rely on the database cache invalidation
        
        logger.info(f"Invalidated availability cache for {organizer.email}" + 
                   (f" on {date}" if date else ""))
        
    except Exception as e:
        logger.error(f"Error invalidating cache: {str(e)}")


def process_waitlist_for_cancelled_booking(booking_id):
    """
    Process waitlist when a booking is cancelled.
    This is typically called as a Celery task.
    """
    try:
        booking = Booking.objects.get(id=booking_id, status='cancelled')
        
        # Find waitlist entries for this time slot
        waitlist_entries = WaitlistEntry.objects.filter(
            event_type=booking.event_type,
            organizer=booking.organizer,
            desired_start_time=booking.start_time,
            desired_end_time=booking.end_time,
            status='active'
        ).order_by('created_at')
        
        # Notify the first person on the waitlist
        if waitlist_entries.exists():
            first_entry = waitlist_entries.first()
            first_entry.notify_availability()
            
            logger.info(f"Notified waitlist entry {first_entry.id} of available slot")
        
    except Booking.DoesNotExist:
        logger.error(f"Booking {booking_id} not found for waitlist processing")
    except Exception as e:
        logger.error(f"Error processing waitlist for booking {booking_id}: {str(e)}")


def generate_recurring_bookings(event_type, base_booking_data, recurrence_rule, max_occurrences=None):
    """
    Generate multiple bookings for recurring events.
    
    Args:
        event_type: EventType instance
        base_booking_data: Base booking data
        recurrence_rule: RRULE string or simple recurrence type
        max_occurrences: Maximum number of occurrences
    
    Returns:
        list: Created booking instances
    """
    bookings = []
    recurrence_id = uuid.uuid4()
    
    try:
        # Parse recurrence rule and generate dates
        occurrence_dates = parse_recurrence_rule(
            recurrence_rule, 
            base_booking_data['start_time'],
            max_occurrences or event_type.max_occurrences
        )
        
        for i, occurrence_date in enumerate(occurrence_dates):
            try:
                # Create booking data for this occurrence
                occurrence_data = base_booking_data.copy()
                occurrence_data['start_time'] = occurrence_date
                
                # Create booking
                booking, created, errors = create_booking_with_validation(
                    event_type, 
                    event_type.organizer, 
                    occurrence_data
                )
                
                if created and booking:
                    # Link to recurring series
                    booking.recurrence_id = recurrence_id
                    booking.recurrence_sequence = i + 1
                    booking.save(update_fields=['recurrence_id', 'recurrence_sequence'])
                    
                    bookings.append(booking)
                elif errors:
                    logger.warning(f"Failed to create recurring booking {i+1}: {errors}")
                    
            except Exception as e:
                logger.error(f"Error creating recurring booking {i+1}: {str(e)}")
                continue
        
        return bookings
        
    except Exception as e:
        logger.error(f"Error generating recurring bookings: {str(e)}")
        return []


def parse_recurrence_rule(recurrence_rule, start_time, max_occurrences):
    """
    Parse recurrence rule and generate occurrence dates.
    
    Args:
        recurrence_rule: RRULE string or simple type
        start_time: Base start time
        max_occurrences: Maximum occurrences
    
    Returns:
        list: List of datetime objects for occurrences
    """
    occurrences = [start_time]  # Include the original
    
    try:
        if recurrence_rule == 'weekly':
            # Simple weekly recurrence
            current_time = start_time
            for i in range(1, max_occurrences):
                current_time += timedelta(weeks=1)
                occurrences.append(current_time)
        
        elif recurrence_rule == 'daily':
            # Simple daily recurrence
            current_time = start_time
            for i in range(1, max_occurrences):
                current_time += timedelta(days=1)
                occurrences.append(current_time)
        
        elif recurrence_rule == 'monthly':
            # Simple monthly recurrence (same day of month)
            from dateutil.relativedelta import relativedelta
            current_time = start_time
            for i in range(1, max_occurrences):
                current_time += relativedelta(months=1)
                occurrences.append(current_time)
        
        elif recurrence_rule.startswith('RRULE:'):
            # Full RRULE parsing (would need python-dateutil)
            # For now, implement basic parsing
            logger.warning(f"Complex RRULE parsing not yet implemented: {recurrence_rule}")
            return [start_time]
        
        return occurrences[:max_occurrences]
        
    except Exception as e:
        logger.error(f"Error parsing recurrence rule: {str(e)}")
        return [start_time]


def get_booking_by_access_token(access_token):
    """
    Get booking by access token with validation.
    
    Args:
        access_token: UUID access token
    
    Returns:
        Booking instance or None
    """
    try:
        booking = Booking.objects.get(access_token=access_token)
        
        if not booking.is_access_token_valid():
            logger.warning(f"Expired access token used for booking {booking.id}")
            return None
        
        return booking
        
    except Booking.DoesNotExist:
        logger.warning(f"Invalid access token: {access_token}")
        return None


def calculate_dst_safe_time_slots(organizer_timezone, invitee_timezone, base_slots):
    """
    Ensure time slots are correctly calculated across DST transitions.
    
    Args:
        organizer_timezone: Organizer's IANA timezone
        invitee_timezone: Invitee's IANA timezone
        base_slots: List of slot dictionaries
    
    Returns:
        list: DST-safe slots with corrected times
    """
    try:
        org_tz = ZoneInfo(organizer_timezone)
        invitee_tz = ZoneInfo(invitee_timezone)
        
        dst_safe_slots = []
        
        for slot in base_slots:
            start_time = slot['start_time']
            end_time = slot['end_time']
            
            # Check if this slot crosses a DST boundary
            start_local_org = start_time.astimezone(org_tz)
            end_local_org = end_time.astimezone(org_tz)
            
            # Check for DST transition
            start_dst = start_local_org.dst()
            end_dst = end_local_org.dst()
            
            if start_dst != end_dst:
                # DST transition during this slot - log warning
                logger.warning(f"DST transition during slot {start_time} - {end_time}")
                
                # Adjust slot to avoid transition if possible
                # For now, skip slots that cross DST boundaries
                continue
            
            # Convert to invitee timezone
            slot_copy = slot.copy()
            slot_copy['local_start_time'] = start_time.astimezone(invitee_tz)
            slot_copy['local_end_time'] = end_time.astimezone(invitee_tz)
            
            # Add DST information
            slot_copy['dst_info'] = {
                'organizer_dst': bool(start_dst),
                'invitee_dst': bool(start_time.astimezone(invitee_tz).dst()),
                'dst_transition': start_dst != end_dst
            }
            
            dst_safe_slots.append(slot_copy)
        
        return dst_safe_slots
        
    except Exception as e:
        logger.error(f"Error calculating DST-safe slots: {str(e)}")
        return base_slots  # Return original slots if DST calculation fails


def validate_timezone_for_booking(timezone_name):
    """
    Validate timezone for booking operations.
    
    Args:
        timezone_name: IANA timezone string
    
    Returns:
        tuple: (is_valid, error_message)
    """
    try:
        ZoneInfo(timezone_name)
        return True, None
    except Exception as e:
        return False, f"Invalid timezone: {timezone_name}"


def get_client_ip_from_request(request):
    """Extract client IP from request headers."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def get_user_agent_from_request(request):
    """Extract user agent from request headers."""
    return request.META.get('HTTP_USER_AGENT', '')


# Import the Celery task here to avoid circular imports
def process_waitlist_for_cancelled_booking(booking_id):
    """Placeholder for Celery task - actual implementation in tasks.py"""
    from .tasks import process_waitlist_for_cancelled_booking
    return process_waitlist_for_cancelled_booking.delay(booking_id)