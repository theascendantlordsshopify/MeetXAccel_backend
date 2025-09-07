from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone
from .models import AvailabilityRule, BlockedTime, BufferTime, DateOverrideRule, RecurringBlockedTime
from apps.events.models import EventType
import logging

logger = logging.getLogger(__name__)


@receiver(post_save, sender=AvailabilityRule)
@receiver(post_delete, sender=AvailabilityRule)
def invalidate_cache_on_availability_rule_change(sender, instance, **kwargs):
    """Invalidate cache when availability rules change."""
    from .tasks import clear_availability_cache
    
    logger.info(f"Availability rule changed for {instance.organizer.email}, clearing cache")
    clear_availability_cache.delay(
        instance.organizer.id, 
        cache_type='availability_rule_change',
        day_of_week=instance.day_of_week
    )


@receiver(post_save, sender=DateOverrideRule)
@receiver(post_delete, sender=DateOverrideRule)
def invalidate_cache_on_date_override_change(sender, instance, **kwargs):
    """Invalidate cache when date override rules change."""
    from .tasks import clear_availability_cache
    
    logger.info(f"Date override changed for {instance.organizer.email} on {instance.date}, clearing cache")
    clear_availability_cache.delay(
        instance.organizer.id,
        cache_type='date_override_change',
        affected_date=instance.date.isoformat()
    )


@receiver(post_save, sender=RecurringBlockedTime)
@receiver(post_delete, sender=RecurringBlockedTime)
def invalidate_cache_on_recurring_block_change(sender, instance, **kwargs):
    """Invalidate cache when recurring blocked times change."""
    from .tasks import clear_availability_cache
    
    logger.info(f"Recurring block changed for {instance.organizer.email}, clearing cache")
    clear_availability_cache.delay(
        instance.organizer.id,
        cache_type='recurring_block_change',
        day_of_week=instance.day_of_week,
        start_date=instance.start_date.isoformat() if instance.start_date else None,
        end_date=instance.end_date.isoformat() if instance.end_date else None
    )


@receiver(post_save, sender=BlockedTime)
@receiver(post_delete, sender=BlockedTime)
def invalidate_cache_on_blocked_time_change(sender, instance, **kwargs):
    """Invalidate cache when blocked times change."""
    from .tasks import clear_availability_cache
    
    logger.info(f"Blocked time changed for {instance.organizer.email}, clearing cache")
    clear_availability_cache.delay(
        instance.organizer.id,
        cache_type='blocked_time_change',
        start_date=instance.start_datetime.date().isoformat(),
        end_date=instance.end_datetime.date().isoformat()
    )


@receiver(post_save, sender=BufferTime)
def invalidate_cache_on_buffer_time_change(sender, instance, **kwargs):
    """Invalidate cache when buffer time settings change."""
    from .tasks import clear_availability_cache
    
    logger.info(f"Buffer time settings changed for {instance.organizer.email}, clearing cache")
    clear_availability_cache.delay(
        instance.organizer.id,
        cache_type='buffer_time_change'
    )


@receiver(pre_save, sender=EventType)
def track_event_type_changes(sender, instance, **kwargs):
    """Track changes to event type fields that affect availability."""
    if instance.pk:  # Only for existing event types
        try:
            old_event_type = EventType.objects.get(pk=instance.pk)
            
            # Check if availability-affecting fields have changed
            availability_fields = [
                'duration', 'buffer_time_before', 'buffer_time_after',
                'min_booking_notice', 'max_booking_advance', 'max_attendees', 'is_active'
            ]
            
            fields_changed = []
            previous_values = {}
            for field in availability_fields:
                old_value = getattr(old_event_type, field, None)
                new_value = getattr(instance, field, None)
                if old_value != new_value:
                    fields_changed.append(field)
                    previous_values[field] = old_value
            
            if fields_changed:
                # Store the change information for post_save signal
                instance._availability_fields_changed = fields_changed
                instance._previous_values = previous_values
                
        except EventType.DoesNotExist:
            pass


@receiver(post_save, sender=EventType)
def invalidate_cache_on_event_type_change(sender, instance, **kwargs):
    """Invalidate cache when event type availability settings change."""
    if hasattr(instance, '_availability_fields_changed'):
        from .tasks import clear_availability_cache
        
        changed_fields = instance._availability_fields_changed
        previous_values = getattr(instance, '_previous_values', {})
        
        logger.info(f"Event type {instance.name} changed availability-affecting fields: {changed_fields}")
        logger.debug(f"Previous values: {previous_values}")
        
        # EventType changes affect all future availability for that type, so immediate refresh
        clear_availability_cache.delay(
            instance.organizer.id,
            cache_type='event_type_change',
            event_type_id=str(instance.id),
            changed_fields=changed_fields,
            previous_values=previous_values
        )
        
        # Clean up the temporary attribute
        delattr(instance, '_availability_fields_changed')
        if hasattr(instance, '_previous_values'):
            delattr(instance, '_previous_values')