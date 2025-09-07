from datetime import datetime, timedelta, time
from django.utils import timezone
from django.db import models
from zoneinfo import ZoneInfo
from .models import AvailabilityRule, BlockedTime, BufferTime, DateOverrideRule, RecurringBlockedTime
from apps.events.models import Booking, EventTypeAvailabilityCache
import logging
import time as time_module
from django.core.cache import cache

logger = logging.getLogger(__name__)


def get_external_busy_times(organizer, start_date, end_date):
    """
    Get busy times from external calendar integrations.
    
    Args:
        organizer: User instance
        start_date: Start date for busy time search
        end_date: End date for busy time search
    
    Returns:
        list: List of busy time periods
    """
    busy_times = []
    
    try:
        from apps.integrations.models import CalendarIntegration
        
        # Get active calendar integrations
        calendar_integrations = CalendarIntegration.objects.filter(
            organizer=organizer,
            is_active=True,
            sync_enabled=True
        )
        
        for integration in calendar_integrations:
            try:
                if integration.provider == 'google':
                    from apps.integrations.google_client import GoogleCalendarClient
                    client = GoogleCalendarClient(integration)
                    events = client.get_busy_times(start_date, end_date)
                elif integration.provider == 'outlook':
                    from apps.integrations.outlook_client import OutlookCalendarClient
                    client = OutlookCalendarClient(integration)
                    events = client.get_busy_times(start_date, end_date)
                else:
                    continue
                
                # Convert to our format
                for event in events:
                    busy_times.append({
                        'start_time': event['start_datetime'],
                        'end_time': event['end_datetime'],
                        'source': f"{integration.provider}_calendar",
                        'title': event.get('summary', 'Busy')
                    })
                    
            except Exception as e:
                logger.warning(f"Error fetching busy times from {integration.provider}: {str(e)}")
                continue
        
        return busy_times
        
    except Exception as e:
        logger.error(f"Error getting external busy times: {str(e)}")
        return []


class PerformanceProfiler:
    """Context manager for profiling performance of code blocks."""
    
    def __init__(self, operation_name, log_threshold=0.1):
        self.operation_name = operation_name
        self.log_threshold = log_threshold
        self.start_time = None
        self.metrics = {}
    
    def __enter__(self):
        self.start_time = time_module.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = time_module.time() - self.start_time
        if duration > self.log_threshold:
            logger.info(f"Performance: {self.operation_name} took {duration:.3f}s")
        self.metrics['duration'] = duration
    
    def checkpoint(self, name):
        """Add a checkpoint for detailed profiling."""
        if self.start_time:
            self.metrics[name] = time_module.time() - self.start_time

def calculate_available_slots(organizer, event_type, start_date, end_date, invitee_timezone='UTC', 
                            attendee_count=1, invitee_timezones=None):
    """
    Enhanced calculate available time slots with comprehensive conflict resolution.
    
    Args:
        organizer: User instance (organizer)
        event_type: EventType instance
        start_date: datetime.date object
        end_date: datetime.date object
        invitee_timezone: IANA timezone string (primary invitee)
        attendee_count: Number of attendees for this booking (default: 1)
        invitee_timezones: List of IANA timezone strings for multi-invitee scheduling
    
    Returns:
        Dict with 'slots', 'warnings', and performance metrics
    """
    with PerformanceProfiler(f"calculate_available_slots for {organizer.email}") as profiler:
        warnings = []
        
        # Validate timezone strings
        if not validate_timezone(invitee_timezone):
            raise ValueError(f"Invalid timezone: {invitee_timezone}")
        
        if invitee_timezones:
            valid_timezones = []
            for tz in invitee_timezones:
                if validate_timezone(tz):
                    valid_timezones.append(tz)
                else:
                    warnings.append(f"Invalid timezone '{tz}' was skipped")
                    logger.warning(f"Invalid timezone '{tz}' provided for multi-invitee scheduling")
            invitee_timezones = valid_timezones
        
        profiler.checkpoint('timezone_validation')
        
        # Check cache first
        cache_key = f"availability:{organizer.id}:{event_type.id}:{start_date}:{end_date}:{invitee_timezone}:{attendee_count}"
        cached_result = cache.get(cache_key)
        
        if cached_result and not _is_cache_dirty(organizer, start_date, end_date):
            profiler.checkpoint('cache_hit')
            cached_result['performance_metrics'] = profiler.metrics
            cached_result['cache_hit'] = True
            return cached_result
    
        # Get organizer's timezone
        organizer_timezone = organizer.profile.timezone_name
        
        # Get availability rules that apply to this event type
        availability_rules = AvailabilityRule.objects.filter(
            organizer=organizer,
            is_active=True
        ).filter(
            models.Q(event_types__isnull=True) | models.Q(event_types=event_type)
        ).distinct()
        
        profiler.checkpoint('rules_query')
        
        # Get date overrides that apply to this event type
        date_overrides = DateOverrideRule.objects.filter(
            organizer=organizer,
            is_active=True,
            date__gte=start_date,
            date__lte=end_date
        ).filter(
            models.Q(event_types__isnull=True) | models.Q(event_types=event_type)
        ).distinct()
        
        # Get blocked times
        blocked_times = BlockedTime.objects.filter(
            organizer=organizer,
            is_active=True,
            start_datetime__date__lte=end_date,
            end_datetime__date__gte=start_date
        )
        
        # Get recurring blocked times
        recurring_blocks = RecurringBlockedTime.objects.filter(
            organizer=organizer,
            is_active=True
        )
        
        # Get existing bookings (ALL event types for this organizer)
        existing_bookings = Booking.objects.filter(
            organizer=organizer,
            status='confirmed',
            start_time__date__lte=end_date,
            end_time__date__gte=start_date
        ).select_related('event_type')
        
        # Get external calendar busy times
        external_busy_times = get_external_busy_times(organizer, start_date, end_date)
        
        # Get buffer settings
        buffer_settings, _ = BufferTime.objects.get_or_create(organizer=organizer)
        
        profiler.checkpoint('data_queries')
        
        available_slots = []
        current_date = start_date
        
        while current_date <= end_date:
            # Skip past dates
            if current_date < timezone.now().date():
                current_date += timedelta(days=1)
                continue
            
            # Check if event type can be booked on this date
            if not event_type.can_book_on_date(current_date):
                current_date += timedelta(days=1)
                continue
            
            # Check for date-specific overrides first
            date_override = date_overrides.filter(date=current_date).first()
            
            if date_override:
                if not date_override.is_available:
                    # Entire day is blocked
                    current_date += timedelta(days=1)
                    continue
                else:
                    # Use override times instead of regular rules
                    slots = generate_slots_for_override(
                        override=date_override,
                        date=current_date,
                        event_type=event_type,
                        organizer_timezone=organizer_timezone,
                        invitee_timezone=invitee_timezone,
                        blocked_times=blocked_times,
                        recurring_blocks=recurring_blocks,
                        existing_bookings=existing_bookings,
                        external_busy_times=external_busy_times,
                        buffer_settings=buffer_settings,
                        attendee_count=attendee_count
                    )
                    available_slots.extend(slots)
            else:
                # Use regular availability rules
                day_of_week = current_date.weekday()  # 0=Monday, 6=Sunday
                day_rules = availability_rules.filter(day_of_week=day_of_week)
                
                for rule in day_rules:
                    if rule.applies_to_event_type(event_type):
                        slots = generate_slots_for_rule(
                            rule=rule,
                            date=current_date,
                            event_type=event_type,
                            organizer_timezone=organizer_timezone,
                            invitee_timezone=invitee_timezone,
                            blocked_times=blocked_times,
                            recurring_blocks=recurring_blocks,
                            existing_bookings=existing_bookings,
                            external_busy_times=external_busy_times,
                            buffer_settings=buffer_settings,
                            attendee_count=attendee_count
                        )
                        available_slots.extend(slots)
            
            current_date += timedelta(days=1)
        
        profiler.checkpoint('slot_generation')
        
        # Merge overlapping or adjacent slots
        available_slots = merge_overlapping_slots(available_slots)
        
        # Sort slots by start time
        available_slots.sort(key=lambda x: x['start_time'])
        
        # Apply DST safety checks
        available_slots = calculate_dst_safe_time_slots(
            organizer_timezone, invitee_timezone, available_slots
        )
        
        profiler.checkpoint('slot_processing')
        
        # Handle multi-invitee timezone intersection if needed
        if invitee_timezones and len(invitee_timezones) > 1:
            available_slots = calculate_multi_invitee_intersection(
                available_slots, invitee_timezones, invitee_timezone, organizer
            )
        
        profiler.checkpoint('multi_invitee_processing')
        
        result = {
            'slots': available_slots,
            'warnings': warnings,
            'cache_hit': False,
            'total_slots': len(available_slots),
            'performance_metrics': profiler.metrics
        }
        
        # Cache the result (for single-day requests)
        if start_date == end_date and len(available_slots) > 0:
            cache.set(cache_key, result, timeout=900)  # Cache for 15 minutes
        
        return result


def _is_cache_dirty(organizer, start_date, end_date):
    """Check if cache is dirty for the given date range."""
    # Check if any cache entries are marked as dirty
    dirty_entries = EventTypeAvailabilityCache.objects.filter(
        organizer=organizer,
        date__gte=start_date,
        date__lte=end_date,
        is_dirty=True
    ).exists()
    
    return dirty_entries


def generate_slots_for_rule(rule, date, event_type, organizer_timezone, invitee_timezone, 
                          blocked_times, recurring_blocks, existing_bookings, 
                          external_busy_times, buffer_settings, attendee_count=1):
    """
    Generate available slots for a specific availability rule on a specific date.
    """
    slots = []
    
    # Create timezone objects
    org_tz = ZoneInfo(organizer_timezone)
    invitee_tz = ZoneInfo(invitee_timezone)
    
    # Handle midnight-crossing rules
    if rule.spans_midnight():
        # Rule spans midnight - split into two parts
        # Part 1: start_time to midnight
        slots.extend(_generate_slots_for_time_range(
            date, rule.start_time, time(23, 59, 59),
            event_type, org_tz, invitee_tz, blocked_times, recurring_blocks,
            existing_bookings, external_busy_times, buffer_settings, attendee_count
        ))
        
        # Part 2: midnight to end_time (next day)
        next_date = date + timedelta(days=1)
        slots.extend(_generate_slots_for_time_range(
            next_date, time(0, 0), rule.end_time,
            event_type, org_tz, invitee_tz, blocked_times, recurring_blocks,
            existing_bookings, external_busy_times, buffer_settings, attendee_count
        ))
    else:
        # Normal rule within same day
        slots.extend(_generate_slots_for_time_range(
            date, rule.start_time, rule.end_time,
            event_type, org_tz, invitee_tz, blocked_times, recurring_blocks,
            existing_bookings, external_busy_times, buffer_settings, attendee_count
        ))
    
    return slots


def generate_slots_for_override(override, date, event_type, organizer_timezone, invitee_timezone,
                              blocked_times, recurring_blocks, existing_bookings, 
                              external_busy_times, buffer_settings, attendee_count=1):
    """
    Generate available slots for a date override rule.
    """
    if not override.is_available or not override.start_time or not override.end_time:
        return []
    
    # Create timezone objects
    org_tz = ZoneInfo(organizer_timezone)
    invitee_tz = ZoneInfo(invitee_timezone)
    
    # Handle midnight-crossing overrides
    if override.spans_midnight():
        # Override spans midnight - split into two parts
        slots = []
        
        # Part 1: start_time to midnight
        slots.extend(_generate_slots_for_time_range(
            date, override.start_time, time(23, 59, 59),
            event_type, org_tz, invitee_tz, blocked_times, recurring_blocks,
            existing_bookings, external_busy_times, buffer_settings, attendee_count
        ))
        
        # Part 2: midnight to end_time (next day)
        next_date = date + timedelta(days=1)
        slots.extend(_generate_slots_for_time_range(
            next_date, time(0, 0), override.end_time,
            event_type, org_tz, invitee_tz, blocked_times, recurring_blocks,
            existing_bookings, external_busy_times, buffer_settings, attendee_count
        ))
        
        return slots
    else:
        # Normal override within same day
        return _generate_slots_for_time_range(
            date, override.start_time, override.end_time,
            event_type, org_tz, invitee_tz, blocked_times, recurring_blocks,
            existing_bookings, external_busy_times, buffer_settings, attendee_count
        )


def _generate_slots_for_time_range(date, start_time, end_time, event_type, org_tz, invitee_tz,
                                 blocked_times, recurring_blocks, existing_bookings, 
                                 external_busy_times, buffer_settings, attendee_count):
    """
    Internal helper to generate slots for a specific time range on a specific date.
    """
    import time as time_module
    start_computation = time_module.time()
    
    slots = []
    
    # Create start and end datetime for the time range on this date
    range_start = datetime.combine(date, start_time).replace(tzinfo=org_tz)
    range_end = datetime.combine(date, end_time).replace(tzinfo=org_tz)
    
    # Convert to UTC for calculations
    range_start_utc = range_start.astimezone(timezone.utc)
    range_end_utc = range_end.astimezone(timezone.utc)
    
    # Calculate slot duration including buffers
    slot_duration = timedelta(minutes=event_type.duration)
    buffer_before = timedelta(minutes=getattr(event_type, 'buffer_time_before', buffer_settings.default_buffer_before))
    buffer_after = timedelta(minutes=getattr(event_type, 'buffer_time_after', buffer_settings.default_buffer_after))
    minimum_gap = timedelta(minutes=buffer_settings.minimum_gap)
    
    # Get slot interval - prioritize event type, then buffer settings, then default
    event_slot_interval = getattr(event_type, 'slot_interval_minutes', 0)
    if event_slot_interval > 0:
        slot_interval = timedelta(minutes=event_slot_interval)
    else:
        slot_interval = timedelta(minutes=getattr(buffer_settings, 'slot_interval_minutes', 15))
    
    # Generate slots
    current_slot_start = range_start_utc
    
    while current_slot_start + slot_duration <= range_end_utc:
        slot_end = current_slot_start + slot_duration
        
        # Check if this slot conflicts with blocked times
        if is_slot_blocked(current_slot_start, slot_end, blocked_times):
            current_slot_start += slot_interval
            continue
        
        # Check if this slot conflicts with recurring blocked times
        if is_slot_blocked_by_recurring(current_slot_start, slot_end, recurring_blocks, org_tz):
            current_slot_start += slot_interval
            continue
        
        # Check if this slot conflicts with external calendar events
        if is_slot_blocked_by_external_calendar(current_slot_start, slot_end, external_busy_times):
            current_slot_start += slot_interval
            continue
        
        # Check if this slot conflicts with existing bookings (including buffers)
        buffered_start = current_slot_start - buffer_before
        buffered_end = slot_end + buffer_after
        
        if is_slot_conflicting_with_bookings(buffered_start, buffered_end, existing_bookings, event_type, attendee_count):
            current_slot_start += slot_interval
            continue
        
        # Check minimum booking notice
        min_notice = timedelta(minutes=event_type.min_scheduling_notice)
        if current_slot_start < timezone.now() + min_notice:
            current_slot_start += slot_interval
            continue
        
        # Check maximum booking advance
        max_advance = timedelta(minutes=event_type.max_scheduling_horizon)
        if current_slot_start > timezone.now() + max_advance:
            break
        
        # Check daily booking limits
        if _exceeds_daily_booking_limit(event_type, current_slot_start):
            current_slot_start += slot_interval
            continue
        
        # This slot is available
        slot = {
            'start_time': current_slot_start,
            'end_time': slot_end,
            'duration_minutes': event_type.duration,
            'available_spots': _get_available_spots_for_slot(
                event_type, current_slot_start, slot_end, existing_bookings, attendee_count
            ),
        }
        
        # Add localized times for display
        if invitee_timezone != 'UTC':
            slot['local_start_time'] = current_slot_start.astimezone(invitee_tz)
            slot['local_end_time'] = slot_end.astimezone(invitee_tz)
        
        slots.append(slot)
        
        # Move to next slot (15-minute intervals + minimum gap)
        next_increment = max(slot_interval, minimum_gap)
        current_slot_start += next_increment
    
    # Log computation time for performance monitoring
    computation_time = time_module.time() - start_computation
    if computation_time > 0.1:  # Log if computation takes more than 100ms
        logger.info(f"Slot generation took {computation_time:.3f}s for {date} ({len(slots)} slots)")
    
    return slots


def is_slot_blocked_by_external_calendar(start_time, end_time, external_busy_times):
    """Check if a time slot conflicts with external calendar events."""
    for busy_period in external_busy_times:
        busy_start = busy_period['start_time']
        busy_end = busy_period['end_time']
        
        # Check for overlap
        if start_time < busy_end and end_time > busy_start:
            return True
    
    return False


def is_slot_blocked(start_time, end_time, blocked_times):
    """Check if a time slot conflicts with blocked times."""
    for blocked in blocked_times:
        blocked_start = blocked.start_datetime
        blocked_end = blocked.end_datetime
        
        # Check for overlap using proper interval logic
        if (start_time < blocked_end and end_time > blocked_start):
            return True
    
    return False


def is_slot_blocked_by_recurring(start_time, end_time, recurring_blocks, organizer_tz):
    """Check if a time slot conflicts with recurring blocked times."""
    for recurring_block in recurring_blocks:
        # Check if this recurring block applies to the date
        slot_date = start_time.astimezone(organizer_tz).date()
        
        if not recurring_block.applies_to_date(slot_date):
            continue
        
        # Convert recurring block times to UTC for comparison
        if recurring_block.spans_midnight():
            # Handle midnight-spanning blocks
            # Part 1: start_time to midnight
            block_start_1 = datetime.combine(slot_date, recurring_block.start_time).replace(tzinfo=organizer_tz)
            block_end_1 = datetime.combine(slot_date, time(23, 59, 59)).replace(tzinfo=organizer_tz)
            
            block_start_1_utc = block_start_1.astimezone(timezone.utc)
            block_end_1_utc = block_end_1.astimezone(timezone.utc)
            
            if (start_time < block_end_1_utc and end_time > block_start_1_utc):
                return True
            
            # Part 2: midnight to end_time (next day)
            next_date = slot_date + timedelta(days=1)
            block_start_2 = datetime.combine(next_date, time(0, 0)).replace(tzinfo=organizer_tz)
            block_end_2 = datetime.combine(next_date, recurring_block.end_time).replace(tzinfo=organizer_tz)
            
            block_start_2_utc = block_start_2.astimezone(timezone.utc)
            block_end_2_utc = block_end_2.astimezone(timezone.utc)
            
            if (start_time < block_end_2_utc and end_time > block_start_2_utc):
                return True
        else:
            # Normal recurring block within same day
            block_start = datetime.combine(slot_date, recurring_block.start_time).replace(tzinfo=organizer_tz)
            block_end = datetime.combine(slot_date, recurring_block.end_time).replace(tzinfo=organizer_tz)
            
            block_start_utc = block_start.astimezone(timezone.utc)
            block_end_utc = block_end.astimezone(timezone.utc)
            
            if (start_time < block_end_utc and end_time > block_start_utc):
                return True
    
    return False


def is_slot_conflicting_with_bookings(start_time, end_time, existing_bookings, event_type, attendee_count=1):
    """Check if a time slot conflicts with existing bookings across ALL event types."""
    # Get overlapping bookings
    overlapping_bookings = []
    for booking in existing_bookings:
        # Apply booking's own buffer times
        booking_buffer_before = timedelta(minutes=booking.event_type.buffer_time_before)
        booking_buffer_after = timedelta(minutes=booking.event_type.buffer_time_after)
        
        buffered_booking_start = booking.start_time - booking_buffer_before
        buffered_booking_end = booking.end_time + booking_buffer_after
        
        # Check for overlap using proper interval logic
        if (start_time < buffered_booking_end and end_time > buffered_booking_start):
            overlapping_bookings.append(booking)
    
    # If no overlapping bookings, slot is available
    if not overlapping_bookings:
        return False
    
    # Check each overlapping booking
    for booking in overlapping_bookings:
        # For same event type group events, check capacity
        if (booking.event_type.id == event_type.id and 
            event_type.is_group_event() and
            booking.start_time == start_time and
            booking.end_time == end_time):
            
            # Check if there's room in this specific booking
            current_attendees = booking.attendees.filter(status='confirmed').count()
            if current_attendees + attendee_count <= event_type.max_attendees:
                continue  # This booking has capacity
        
        # For different event types or times, it's always a conflict
        return True
        
    return False


def _exceeds_daily_booking_limit(event_type, start_time):
    """Check if booking would exceed daily limits."""
    if not event_type.max_bookings_per_day:
        return False
    
    # Count existing bookings for this event type on this date
    org_tz = ZoneInfo(event_type.organizer.profile.timezone_name)
    booking_date = start_time.astimezone(org_tz).date()
    
    existing_count = Booking.objects.filter(
        organizer=event_type.organizer,
        event_type=event_type,
        status='confirmed',
        start_time__date=booking_date
    ).count()
    
    return existing_count >= event_type.max_bookings_per_day


def _get_available_spots_for_slot(event_type, start_time, end_time, existing_bookings, requested_attendee_count):
    """Get number of available spots for a specific slot."""
    if not event_type.is_group_event():
        return 1 if requested_attendee_count == 1 else 0
    
    # Find existing booking at this exact time
    existing_booking = None
    for booking in existing_bookings:
        if (booking.event_type.id == event_type.id and
            booking.start_time == start_time and
            booking.end_time == end_time and
            booking.status == 'confirmed'):
            existing_booking = booking
            break
    
    if existing_booking:
        current_attendees = existing_booking.attendees.filter(status='confirmed').count()
        return max(0, event_type.max_attendees - current_attendees)
    
    return event_type.max_attendees


def merge_overlapping_slots(slots):
    """
    Merge overlapping or adjacent slots into continuous blocks.
    
    Note: This function handles day-crossing slots correctly by operating on UTC datetime objects.
    Slots that span midnight are already split into separate time ranges by the generation logic.
    """
    if not slots:
        return slots
    
    # Sort slots by start time
    sorted_slots = sorted(slots, key=lambda x: x['start_time'])
    merged_slots = []
    
    current_slot = sorted_slots[0].copy()
    
    for next_slot in sorted_slots[1:]:
        # Check if slots are adjacent or overlapping
        if (current_slot['end_time'] >= next_slot['start_time'] or 
            current_slot['end_time'] + timedelta(minutes=5) >= next_slot['start_time']):
            # Merge slots
            current_slot['end_time'] = max(current_slot['end_time'], next_slot['end_time'])
            # Update duration
            duration_minutes = int((current_slot['end_time'] - current_slot['start_time']).total_seconds() / 60)
            current_slot['duration_minutes'] = duration_minutes
            
            # Update localized times if they exist
            if 'local_start_time' in current_slot and 'local_end_time' in next_slot:
                current_slot['local_end_time'] = max(
                    current_slot.get('local_end_time', current_slot['end_time']),
                    next_slot.get('local_end_time', next_slot['end_time'])
                )
            
            # Merge available spots (take minimum for safety)
            if 'available_spots' in current_slot and 'available_spots' in next_slot:
                current_slot['available_spots'] = min(
                    current_slot['available_spots'],
                    next_slot['available_spots']
                )
        else:
            # No overlap, add current slot and move to next
            merged_slots.append(current_slot)
            current_slot = next_slot.copy()
    
    # Add the last slot
    merged_slots.append(current_slot)
    
    return merged_slots


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
                
                # For now, skip slots that cross DST boundaries to avoid confusion
                continue
            
            # Convert to invitee timezone
            slot_copy = slot.copy()
            slot_copy['local_start_time'] = start_time.astimezone(invitee_tz)
            slot_copy['local_end_time'] = end_time.astimezone(invitee_tz)
            
            # Add DST information for debugging
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


def calculate_multi_invitee_intersection(organizer_slots, invitee_timezones, primary_timezone, organizer=None):
    """
    Calculate the intersection of available slots across multiple invitee timezones.
    
    Args:
        organizer_slots: List of available slots from organizer's perspective
        invitee_timezones: List of IANA timezone strings for all invitees
        primary_timezone: Primary timezone for displaying results
        organizer: User instance for getting custom reasonable hours
    
    Returns:
        List of slots that work for all invitees (within reasonable hours)
    """
    if not invitee_timezones or len(invitee_timezones) <= 1:
        return organizer_slots
    
    # Get reasonable hours from organizer's profile if available
    if organizer and hasattr(organizer, 'profile'):
        reasonable_start_hour = organizer.profile.reasonable_hours_start
        reasonable_end_hour = organizer.profile.reasonable_hours_end
    else:
        reasonable_start_hour = 7
        reasonable_end_hour = 22
    
    # For each slot, check if it falls within reasonable hours for all invitees
    reasonable_slots = []
    
    for slot in organizer_slots:
        slot_start_utc = slot['start_time']
        slot_end_utc = slot['end_time']
        
        # Check if this slot is reasonable for all invitees
        is_reasonable_for_all = True
        invitee_times = {}
        
        for tz_name in invitee_timezones:
            try:
                invitee_tz = ZoneInfo(tz_name)
                local_start = slot_start_utc.astimezone(invitee_tz)
                local_end = slot_end_utc.astimezone(invitee_tz)
                
                # Get reasonable hours for this timezone
                tz_reasonable_start, tz_reasonable_end = get_reasonable_hours_for_timezone(
                    tz_name, reasonable_start_hour, reasonable_end_hour
                )
                
                # Check if slot falls within reasonable hours
                if (local_start.hour < tz_reasonable_start or 
                    local_end.hour > tz_reasonable_end or
                    local_start.date() != local_end.date()):  # Avoid cross-date slots
                    is_reasonable_for_all = False
                    break
                
                # Store timezone information
                invitee_times[tz_name] = {
                    'start_time': local_start,
                    'end_time': local_end,
                    'start_hour': local_start.hour,
                    'end_hour': local_end.hour
                }
                    
            except Exception as e:
                logger.warning(f"Invalid timezone {tz_name}: {e}")
                is_reasonable_for_all = False
                break
        
        if is_reasonable_for_all:
            # Add timezone information for all invitees
            slot_with_timezones = slot.copy()
            slot_with_timezones['invitee_times'] = invitee_times
            
            # Calculate fairness score
            slot_with_timezones['fairness_score'] = calculate_slot_fairness_score(invitee_times)
            
            reasonable_slots.append(slot_with_timezones)
    
    # Sort by fairness score (higher is better)
    reasonable_slots.sort(key=lambda x: x.get('fairness_score', 0), reverse=True)
    
    return reasonable_slots


def calculate_slot_fairness_score(invitee_times):
    """
    Calculate a fairness score for a slot across multiple timezones.
    Higher score means more fair/optimal for all participants.
    """
    if not invitee_times:
        return 0
    
    total_score = 0
    
    for tz_name, time_info in invitee_times.items():
        local_hour = time_info['start_hour']
        
        # Score based on how reasonable the hour is (peak = 10 AM - 4 PM)
        if 10 <= local_hour <= 16:
            tz_score = 100  # Perfect time
        elif 8 <= local_hour <= 18:
            tz_score = 80   # Good time
        elif 7 <= local_hour <= 20:
            tz_score = 60   # Acceptable time
        elif 6 <= local_hour <= 22:
            tz_score = 40   # Early/late but manageable
        else:
            tz_score = 0    # Too early/late
        
        total_score += tz_score
    
    # Return average score
    return total_score / len(invitee_times)


def validate_timezone(timezone_string):
    """Validate that a timezone string is a valid IANA timezone."""
    try:
        ZoneInfo(timezone_string)
        return True
    except Exception:
        return False


def get_reasonable_hours_for_timezone(timezone_string, reasonable_start=7, reasonable_end=22):
    """
    Get reasonable working hours for a timezone.
    
    Args:
        timezone_string: IANA timezone string
        reasonable_start: Start hour (24-hour format)
        reasonable_end: End hour (24-hour format)
    
    Returns:
        Tuple of (start_hour, end_hour) in 24-hour format
    """
    # Use provided reasonable hours (can be customized per organizer)
    # Future enhancement: Consider cultural differences per timezone
    return (reasonable_start, reasonable_end)


def calculate_timezone_offset_hours(from_timezone, to_timezone, reference_date=None):
    """
    Calculate the hour offset between two timezones on a specific date.
    
    Args:
        from_timezone: Source IANA timezone string
        to_timezone: Target IANA timezone string
        reference_date: Date to calculate offset for (accounts for DST)
    
    Returns:
        Float representing hour offset (positive if to_timezone is ahead)
    """
    if reference_date is None:
        reference_date = timezone.now().date()
    
    try:
        from_tz = ZoneInfo(from_timezone)
        to_tz = ZoneInfo(to_timezone)
        
        # Create a reference datetime at noon to avoid DST edge cases
        reference_dt = datetime.combine(reference_date, time(12, 0)).replace(tzinfo=from_tz)
        
        # Convert to target timezone
        converted_dt = reference_dt.astimezone(to_tz)
        
        # Calculate offset in hours
        offset_seconds = (converted_dt.utcoffset() - reference_dt.utcoffset()).total_seconds()
        return offset_seconds / 3600
        
    except Exception as e:
        logger.error(f"Error calculating timezone offset: {e}")
        return 0.0


def find_optimal_slots_for_group(organizer_slots, invitee_timezones, max_slots=10):
    """
    Find the most optimal slots for a group meeting across multiple timezones.
    
    Args:
        organizer_slots: List of available slots from organizer
        invitee_timezones: List of IANA timezone strings
        max_slots: Maximum number of optimal slots to return
    
    Returns:
        List of slots ranked by how "fair" they are across all timezones
    """
    if not invitee_timezones or len(invitee_timezones) <= 1:
        return organizer_slots[:max_slots]
    
    # Use the multi-invitee intersection logic which already includes fairness scoring
    optimal_slots = calculate_multi_invitee_intersection(
        organizer_slots, invitee_timezones, invitee_timezones[0]
    )
    
    return optimal_slots[:max_slots]


def mark_cache_dirty(organizer_id, cache_type, **kwargs):
    """
    Mark cache as dirty for batch invalidation processing.
    
    Args:
        organizer_id: UUID of the organizer
        cache_type: Type of change that triggered the dirty flag
        **kwargs: Additional parameters for specific dirty flag types
    """
    dirty_key = f"dirty_cache:{organizer_id}"
    
    # Get existing dirty data or create new
    dirty_data = cache.get(dirty_key, {
        'organizer_id': str(organizer_id),
        'changes': [],
        'last_updated': timezone.now().isoformat()
    })
    
    # Add new change
    change_entry = {
        'cache_type': cache_type,
        'timestamp': timezone.now().isoformat(),
        **kwargs
    }
    dirty_data['changes'].append(change_entry)
    dirty_data['last_updated'] = timezone.now().isoformat()
    
    # Store dirty flag for 10 minutes (enough time for batch processing)
    cache.set(dirty_key, dirty_data, timeout=600)
    
    logger.debug(f"Marked cache dirty for organizer {organizer_id}: {cache_type}")


def get_dirty_organizers():
    """
    Get list of organizers with dirty cache flags.
    
    Returns:
        List of organizer IDs that need cache refresh
    """
    # This would require Redis SCAN in production
    # For now, return empty list as this is a future enhancement
    return []


def clear_dirty_flags(organizer_id):
    """Clear dirty flags for an organizer after processing."""
    dirty_key = f"dirty_cache:{organizer_id}"
    cache.delete(dirty_key)
    logger.debug(f"Cleared dirty flags for organizer {organizer_id}")


def get_cache_key_for_availability(organizer_id, event_type_id, start_date, end_date, 
                                 invitee_timezone='UTC', attendee_count=1):
    """
    Generate a consistent cache key for availability data.
    
    Args:
        organizer_id: UUID of the organizer
        event_type_id: UUID of the event type
        start_date: Start date for availability
        end_date: End date for availability
        invitee_timezone: Timezone for the invitee
        attendee_count: Number of attendees
    
    Returns:
        String cache key
    """
    return f"availability:{organizer_id}:{event_type_id}:{start_date}:{end_date}:{invitee_timezone}:{attendee_count}"


def get_weekly_cache_keys_for_date_range(organizer_id, event_type_id, start_date, end_date):
    """
    Generate weekly cache keys that cover a date range.
    
    Args:
        organizer_id: UUID of the organizer
        event_type_id: UUID of the event type
        start_date: Start date
        end_date: End date
    
    Returns:
        List of cache key patterns
    """
    cache_keys = []
    current_date = start_date
    
    while current_date <= end_date:
        # Calculate week boundaries
        week_start = current_date - timedelta(days=current_date.weekday())
        week_end = week_start + timedelta(days=6)
        
        # Generate base cache key for this week
        base_key = f"availability:{organizer_id}:{event_type_id}:{week_start}:{week_end}"
        cache_keys.append(base_key)
        
        # Move to next week
        current_date = week_end + timedelta(days=1)
    
    return cache_keys


def generate_cache_key_variations(base_key):
    """
    Generate variations of a cache key to handle different timezone/attendee combinations.
    
    Args:
        base_key: Base cache key pattern
    
    Returns:
        List of cache key variations to clear
    """
    # Common timezone variations
    common_timezones = ['UTC', 'America/New_York', 'Europe/London', 'Asia/Tokyo', 'America/Los_Angeles']
    attendee_counts = [1, 2, 3, 4, 5]  # Common attendee counts
    
    variations = []
    for tz in common_timezones:
        for count in attendee_counts:
            variations.append(f"{base_key}:{tz}:{count}")
    
    return variations