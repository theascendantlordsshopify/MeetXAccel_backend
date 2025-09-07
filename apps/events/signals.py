from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.db import transaction
from .models import Booking, EventType, Attendee
from .utils import create_booking_audit_log, invalidate_availability_cache
import logging

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Booking)
@receiver(post_delete, sender=Booking)
def invalidate_cache_on_booking_change(sender, instance, **kwargs):
    """Invalidate availability cache when bookings change."""
    logger.info(f"Booking changed for {instance.organizer.email}, invalidating cache")
    
    # Invalidate cache for the affected dates
    transaction.on_commit(lambda: invalidate_availability_cache(
        instance.organizer, 
        instance.start_time.date()
    ))
    
    # Also invalidate cache for the end date if different
    if instance.start_time.date() != instance.end_time.date():
        transaction.on_commit(lambda: invalidate_availability_cache(
            instance.organizer, 
            instance.end_time.date()
        ))


@receiver(post_save, sender=Booking)
def handle_booking_calendar_integration(sender, instance, created, **kwargs):
    """Handle calendar integration when booking is created/updated."""
    if created and instance.status == 'confirmed':
        # Set initial calendar sync status
        if not hasattr(instance, '_calendar_sync_triggered'):
            instance.calendar_sync_status = 'pending'
            instance.save(update_fields=['calendar_sync_status'])
            instance._calendar_sync_triggered = True
        
            # Trigger calendar sync
            transaction.on_commit(lambda: _trigger_calendar_sync(instance))
        
        # Generate meeting link if needed
        if instance.event_type.location_type == 'video_call':
            transaction.on_commit(lambda: _trigger_meeting_link_generation(instance))
    
    elif not created:
        # Handle booking updates (rescheduling, status changes)
        if hasattr(instance, '_status_changed'):
            old_status = instance._old_status
            new_status = instance.status
            
            if old_status != new_status:
                if new_status == 'cancelled':
                    transaction.on_commit(lambda: _trigger_cancellation_workflows(instance))
                elif new_status == 'rescheduled':
                    transaction.on_commit(lambda: _trigger_rescheduling_workflows(instance))
                elif new_status == 'completed':
                    transaction.on_commit(lambda: _trigger_completion_workflows(instance))


@receiver(post_save, sender=EventType)
def invalidate_cache_on_event_type_change(sender, instance, **kwargs):
    """Invalidate cache when event type settings change."""
    if hasattr(instance, '_availability_affecting_fields_changed'):
        logger.info(f"Event type {instance.name} changed, invalidating all cache for organizer")
        
        # Invalidate all cache for this organizer since event type changes affect all dates
        transaction.on_commit(lambda: invalidate_availability_cache(instance.organizer))


@receiver(post_save, sender=Attendee)
def handle_attendee_changes(sender, instance, created, **kwargs):
    """Handle attendee additions/changes."""
    if created:
        # Update booking attendee count
        booking = instance.booking
        booking.attendee_count = booking.attendees.filter(status='confirmed').count()
        booking.save(update_fields=['attendee_count'])
        
        # Invalidate cache
        transaction.on_commit(lambda: invalidate_availability_cache(
            booking.organizer, 
            booking.start_time.date()
        ))


@receiver(post_delete, sender=Booking)
def handle_booking_calendar_cleanup(sender, instance, **kwargs):
    """Handle calendar cleanup when booking is deleted."""
    if instance.external_calendar_event_id:
        transaction.on_commit(lambda: _trigger_calendar_event_deletion(instance))


def _trigger_calendar_sync(booking):
    """Trigger calendar synchronization for booking."""
    from .tasks import sync_booking_to_external_calendars
    sync_booking_to_external_calendars.delay(booking.id)


def _trigger_meeting_link_generation(booking):
    """Trigger meeting link generation task."""
    from apps.integrations.tasks import generate_meeting_link
    generate_meeting_link.delay(booking.id)


def _trigger_cancellation_workflows(booking):
    """Trigger workflows for booking cancellation."""
    from .tasks import trigger_event_type_workflows
    trigger_event_type_workflows.delay(booking.id, 'booking_cancelled')


def _trigger_rescheduling_workflows(booking):
    """Trigger workflows for booking rescheduling."""
    from .tasks import trigger_event_type_workflows
    trigger_event_type_workflows.delay(booking.id, 'booking_rescheduled')


def _trigger_completion_workflows(booking):
    """Trigger workflows for booking completion."""
    from .tasks import trigger_event_type_workflows
    trigger_event_type_workflows.delay(booking.id, 'booking_completed')


def _trigger_calendar_event_deletion(booking):
    """Trigger calendar event deletion task."""
    from apps.integrations.tasks import remove_calendar_event
    remove_calendar_event.delay(booking.id)