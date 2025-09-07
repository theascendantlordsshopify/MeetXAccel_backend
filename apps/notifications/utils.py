"""
Utility functions for notifications with robust error handling and template processing.
"""
import re
import logging
from django.template import Template, Context
from django.utils import timezone
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def render_template_with_fallbacks(template_string, context_data):
    """
    Safely render template content with fallbacks for missing data.
    
    Args:
        template_string: Template string with placeholders
        context_data: Dictionary of data for placeholder replacement
    
    Returns:
        str: Rendered template with fallbacks for missing data
    """
    if not template_string:
        return ""
    
    # Define fallback values for common placeholders
    fallbacks = {
        'invitee_name': 'Guest',
        'invitee_email': 'N/A',
        'organizer_name': 'Host',
        'organizer_email': 'N/A',
        'event_name': 'Meeting',
        'start_time': 'TBD',
        'end_time': 'TBD',
        'duration': 'TBD',
        'meeting_link': 'TBD',
        'meeting_id': 'TBD',
        'meeting_password': 'N/A',
        'booking_id': 'N/A',
        'cancellation_reason': 'No reason provided',
        'organizer_phone': 'N/A',
        'invitee_phone': 'N/A',
        'event_description': 'No description available',
        'location': 'TBD',
        'timezone': 'UTC',
        'booking_url': 'N/A',
        'reschedule_url': 'N/A',
        'cancel_url': 'N/A',
    }
    
    # Merge context data with fallbacks
    safe_context = fallbacks.copy()
    safe_context.update(context_data or {})
    
    # Use Django template engine for safe rendering
    try:
        template = Template(template_string)
        context = Context(safe_context)
        return template.render(context)
    except Exception as e:
        logger.error(f"Template rendering error: {str(e)}")
        
        # Fallback to simple string replacement
        rendered = template_string
        for key, value in safe_context.items():
            placeholder_patterns = [
                f'{{{{{key}}}}}',  # Django style: {{key}}
                f'{{{key}}}',     # Simple style: {key}
                f'%{key}%',       # Alternative style: %key%
            ]
            
            for pattern in placeholder_patterns:
                rendered = rendered.replace(pattern, str(value))
        
        return rendered


def validate_template_placeholders(template_string, required_placeholders=None):
    """
    Validate that template contains required placeholders and identify all placeholders.
    
    Args:
        template_string: Template string to validate
        required_placeholders: List of required placeholder names
    
    Returns:
        dict: Validation results with found/missing placeholders
    """
    if not template_string:
        return {'valid': True, 'found_placeholders': [], 'missing_placeholders': []}
    
    # Find all placeholders in template
    placeholder_patterns = [
        r'\{\{(\w+)\}\}',  # Django style: {{key}}
        r'\{(\w+)\}',      # Simple style: {key}
        r'%(\w+)%',        # Alternative style: %key%
    ]
    
    found_placeholders = set()
    for pattern in placeholder_patterns:
        matches = re.findall(pattern, template_string)
        found_placeholders.update(matches)
    
    # Check required placeholders
    missing_placeholders = []
    if required_placeholders:
        for required in required_placeholders:
            if required not in found_placeholders:
                missing_placeholders.append(required)
    
    return {
        'valid': len(missing_placeholders) == 0,
        'found_placeholders': list(found_placeholders),
        'missing_placeholders': missing_placeholders
    }


def calculate_send_time_with_preferences(base_time, preferences, notification_type='reminder'):
    """
    Calculate appropriate send time based on user preferences.
    
    Args:
        base_time: Desired send time (timezone-aware datetime)
        preferences: NotificationPreference instance
        notification_type: Type of notification ('reminder', 'daily_agenda', etc.)
    
    Returns:
        datetime: Adjusted send time respecting preferences
    """
    if not preferences:
        return base_time
    
    organizer_tz = ZoneInfo(preferences.organizer.profile.timezone_name)
    local_time = base_time.astimezone(organizer_tz)
    
    # Check DND period
    if preferences.is_in_dnd_period(local_time.time()):
        # Move to end of DND period
        next_send = local_time.replace(
            hour=preferences.dnd_end_time.hour,
            minute=preferences.dnd_end_time.minute,
            second=0,
            microsecond=0
        )
        
        # If DND end is earlier in the day, move to next day
        if next_send.time() <= local_time.time():
            next_send += timedelta(days=1)
        
        local_time = next_send
    
    # Check weekend exclusion
    if preferences.should_exclude_weekend(notification_type, local_time.date()):
        # Move to next Monday
        days_until_monday = (7 - local_time.weekday()) % 7
        if days_until_monday == 0:  # Already Monday
            days_until_monday = 7
        local_time += timedelta(days=days_until_monday)
        
        # Reset to appropriate time for the notification type
        if notification_type == 'daily_agenda':
            local_time = local_time.replace(
                hour=preferences.daily_agenda_time.hour,
                minute=preferences.daily_agenda_time.minute,
                second=0,
                microsecond=0
            )
    
    return local_time.astimezone(timezone.utc)


def get_notification_context_from_booking(booking):
    """
    Extract notification context data from a booking.
    
    Args:
        booking: Booking instance
    
    Returns:
        dict: Context data for template rendering
    """
    if not booking:
        return {}
    
    # Format times in organizer's timezone
    organizer_tz = ZoneInfo(booking.organizer.profile.timezone_name)
    invitee_tz = ZoneInfo(booking.invitee_timezone)
    
    start_time_org = booking.start_time.astimezone(organizer_tz)
    end_time_org = booking.end_time.astimezone(organizer_tz)
    start_time_invitee = booking.start_time.astimezone(invitee_tz)
    end_time_invitee = booking.end_time.astimezone(invitee_tz)
    
    return {
        'booking_id': str(booking.id),
        'invitee_name': booking.invitee_name or 'Guest',
        'invitee_email': booking.invitee_email or 'N/A',
        'invitee_phone': booking.invitee_phone or 'N/A',
        'organizer_name': booking.organizer.first_name or 'Host',
        'organizer_email': booking.organizer.email or 'N/A',
        'organizer_phone': getattr(booking.organizer.profile, 'phone', '') or 'N/A',
        'event_name': booking.event_type.name or 'Meeting',
        'event_description': booking.event_type.description or 'No description available',
        'duration': f"{booking.event_type.duration} minutes",
        'start_time': start_time_org.strftime('%B %d, %Y at %I:%M %p'),
        'end_time': end_time_org.strftime('%I:%M %p'),
        'start_time_invitee': start_time_invitee.strftime('%B %d, %Y at %I:%M %p'),
        'end_time_invitee': end_time_invitee.strftime('%I:%M %p'),
        'organizer_timezone': booking.organizer.profile.timezone_name,
        'invitee_timezone': booking.invitee_timezone,
        'meeting_link': booking.meeting_link or 'TBD',
        'meeting_id': booking.meeting_id or 'TBD',
        'meeting_password': booking.meeting_password or 'N/A',
        'location': booking.event_type.location_details or 'Video call',
        'cancellation_reason': getattr(booking, 'cancellation_reason', '') or 'No reason provided',
        'booking_url': f"/bookings/{booking.id}",
        'reschedule_url': f"/reschedule/{booking.id}",
        'cancel_url': f"/cancel/{booking.id}",
        'custom_answers': booking.custom_answers or {},
    }


def validate_phone_number(phone_number):
    """
    Validate phone number format for SMS sending.
    
    Args:
        phone_number: Phone number string
    
    Returns:
        dict: Validation result with formatted number
    """
    import re
    
    if not phone_number:
        return {'valid': False, 'error': 'Phone number is required'}
    
    # Remove all non-digit characters
    digits_only = re.sub(r'\D', '', phone_number)
    
    # Check length
    if len(digits_only) < 10:
        return {'valid': False, 'error': 'Phone number too short'}
    
    if len(digits_only) > 15:
        return {'valid': False, 'error': 'Phone number too long'}
    
    # Format for Twilio (E.164 format)
    if len(digits_only) == 10:
        # US number, add country code
        formatted = f"+1{digits_only}"
    elif len(digits_only) == 11 and digits_only.startswith('1'):
        # US number with country code
        formatted = f"+{digits_only}"
    else:
        # International number
        formatted = f"+{digits_only}"
    
    return {
        'valid': True,
        'formatted': formatted,
        'original': phone_number
    }


def get_twilio_client():
    """Get configured Twilio client."""
    from django.conf import settings
    
    if not all([
        getattr(settings, 'TWILIO_ACCOUNT_SID', None),
        getattr(settings, 'TWILIO_AUTH_TOKEN', None),
        getattr(settings, 'TWILIO_PHONE_NUMBER', None)
    ]):
        raise ValueError("Twilio credentials not configured")
    
    from twilio.rest import Client
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def create_notification_from_template(template, booking=None, custom_context=None, recipient_email=None, recipient_phone=None):
    """
    Create a NotificationLog from a template with proper context.
    
    Args:
        template: NotificationTemplate instance
        booking: Booking instance (optional)
        custom_context: Additional context data (optional)
        recipient_email: Override recipient email (optional)
        recipient_phone: Override recipient phone (optional)
    
    Returns:
        NotificationLog: Created notification log instance
    """
    from .models import NotificationLog
    
    # Build context data
    context_data = {}
    
    if booking:
        context_data.update(get_notification_context_from_booking(booking))
    
    if custom_context:
        context_data.update(custom_context)
    
    # Validate required placeholders
    missing_placeholders = template.validate_placeholders(context_data)
    if missing_placeholders:
        logger.warning(f"Template {template.name} missing placeholders: {missing_placeholders}")
    
    # Render content
    rendered_content = template.render_content(context_data)
    
    # Determine recipients
    final_recipient_email = recipient_email or context_data.get('invitee_email', '')
    final_recipient_phone = recipient_phone or context_data.get('invitee_phone', '')
    
    # Create notification log
    notification_log = NotificationLog.objects.create(
        organizer=template.organizer,
        booking=booking,
        template=template,
        notification_type=template.notification_type,
        recipient_email=final_recipient_email if template.notification_type == 'email' else '',
        recipient_phone=final_recipient_phone if template.notification_type == 'sms' else '',
        subject=rendered_content['subject'],
        message=rendered_content['message'],
        status='pending'
    )
    
    return notification_log


def format_time_for_timezone(dt, timezone_name, format_string='%B %d, %Y at %I:%M %p'):
    """
    Format datetime for a specific timezone.
    
    Args:
        dt: datetime object (timezone-aware)
        timezone_name: IANA timezone string
        format_string: strftime format string
    
    Returns:
        str: Formatted time string
    """
    try:
        tz = ZoneInfo(timezone_name)
        local_dt = dt.astimezone(tz)
        return local_dt.strftime(format_string)
    except Exception as e:
        logger.error(f"Error formatting time for timezone {timezone_name}: {str(e)}")
        return dt.strftime(format_string)  # Fallback to original timezone


def calculate_reminder_send_time(booking, minutes_before, preferences=None):
    """
    Calculate when to send a reminder based on booking time and preferences.
    
    Args:
        booking: Booking instance
        minutes_before: Minutes before booking to send reminder
        preferences: NotificationPreference instance (optional)
    
    Returns:
        datetime: When to send the reminder (UTC)
    """
    base_send_time = booking.start_time - timedelta(minutes=minutes_before)
    
    if preferences:
        return calculate_send_time_with_preferences(base_send_time, preferences, 'reminder')
    
    return base_send_time


def get_email_delivery_status_from_provider(external_id, provider='default'):
    """
    Get delivery status from email provider (placeholder for future implementation).
    
    Args:
        external_id: External message ID from email provider
        provider: Email provider name
    
    Returns:
        dict: Delivery status information
    """
    # This is a placeholder for future implementation with email providers
    # that support delivery tracking (e.g., SendGrid, Mailgun, AWS SES)
    return {
        'status': 'unknown',
        'delivered_at': None,
        'opened_at': None,
        'clicked_at': None
    }


def sanitize_sms_message(message, max_length=1600):
    """
    Sanitize SMS message content.
    
    Args:
        message: SMS message content
        max_length: Maximum message length
    
    Returns:
        str: Sanitized message
    """
    if not message:
        return ""
    
    # Remove excessive whitespace
    sanitized = re.sub(r'\s+', ' ', message.strip())
    
    # Truncate if too long
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length-3] + "..."
    
    return sanitized


def create_webhook_payload(notification_log):
    """
    Create webhook payload for notification events.
    
    Args:
        notification_log: NotificationLog instance
    
    Returns:
        dict: Webhook payload
    """
    payload = {
        'event_type': 'notification_sent',
        'notification_id': str(notification_log.id),
        'notification_type': notification_log.notification_type,
        'status': notification_log.status,
        'recipient_email': notification_log.recipient_email,
        'recipient_phone': notification_log.recipient_phone,
        'subject': notification_log.subject,
        'sent_at': notification_log.sent_at.isoformat() if notification_log.sent_at else None,
        'organizer_email': notification_log.organizer.email,
        'timestamp': timezone.now().isoformat()
    }
    
    if notification_log.booking:
        payload['booking_id'] = str(notification_log.booking.id)
        payload['event_type_name'] = notification_log.booking.event_type.name
    
    return payload