from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone
from datetime import timedelta
from .models import NotificationLog, NotificationTemplate, NotificationSchedule, NotificationPreference
from .utils import (
    get_twilio_client, validate_phone_number, sanitize_sms_message,
    calculate_send_time_with_preferences, get_notification_context_from_booking,
    create_webhook_payload
)
import logging

logger = logging.getLogger(__name__)


@shared_task
def send_notification_task(notification_log_id, retry_count=0):
    """Send a notification based on notification log."""
    try:
        log = NotificationLog.objects.get(id=notification_log_id)
        
        # Check if already sent or if max retries exceeded
        if log.status in ['sent', 'delivered'] or not log.can_retry():
            return f"Notification {notification_log_id} already processed or max retries exceeded"
        
        # Update status to sending
        log.status = 'sending'
        log.save(update_fields=['status'])
        
        if log.notification_type == 'email':
            result = send_email_notification(log)
        elif log.notification_type == 'sms':
            result = send_sms_notification(log)
        else:
            raise ValueError(f"Unknown notification type: {log.notification_type}")
        
        # Update log status
        if result.get('success', False):
            log.status = 'sent'
            log.sent_at = timezone.now()
            log.delivery_status = result.get('delivery_status', 'sent')
            log.external_id = result.get('external_id', '')
            log.provider_response = result.get('provider_response', {})
        else:
            # Mark for retry or failed
            error_message = result.get('error', 'Unknown error')
            log.mark_retry(error_message)
            
            # Retry if possible
            if log.can_retry():
                # Schedule retry with exponential backoff
                retry_delay = min(300, 30 * (2 ** retry_count))  # Max 5 minutes
                send_notification_task.apply_async(
                    args=[notification_log_id, retry_count + 1],
                    countdown=retry_delay
                )
                return f"Notification {notification_log_id} scheduled for retry {retry_count + 1}"
        
        log.save(update_fields=['status', 'sent_at', 'delivery_status', 'external_id', 'provider_response'])
        
        # Trigger webhook if configured
        if log.status == 'sent':
            trigger_notification_webhook.delay(log.id)
        
        return f"Notification {notification_log_id} processed: {log.status}"
    
    except NotificationLog.DoesNotExist:
        return f"Notification log {notification_log_id} not found"
    except Exception as e:
        logger.error(f"Error sending notification {notification_log_id}: {str(e)}")
        
        # Update log with error
        try:
            log = NotificationLog.objects.get(id=notification_log_id)
            log.mark_retry(str(e))
            
            # Retry if possible
            if log.can_retry():
                retry_delay = min(300, 30 * (2 ** retry_count))
                send_notification_task.apply_async(
                    args=[notification_log_id, retry_count + 1],
                    countdown=retry_delay
                )
                return f"Notification {notification_log_id} scheduled for retry after error"
        except:
            pass
        
        return f"Error sending notification: {str(e)}"


@shared_task
def send_test_notification(template_id, recipient_email, recipient_phone=None):
    """Send a test notification using a template."""
    try:
        template = NotificationTemplate.objects.get(id=template_id)
        
        # Create test context
        test_context = {
            'invitee_name': 'Test User',
            'invitee_email': recipient_email,
            'organizer_name': template.organizer.first_name,
            'organizer_email': template.organizer.email,
            'event_name': 'Test Meeting',
            'start_time': 'Tomorrow at 2:00 PM',
            'duration': '30 minutes',
            'meeting_link': 'https://zoom.us/j/test123456',
        }
        
        # Render template content
        rendered_content = template.render_content(test_context)
        
        # Create test notification log
        log = NotificationLog.objects.create(
            organizer=template.organizer,
            template=template,
            notification_type=template.notification_type,
            recipient_email=recipient_email if template.notification_type == 'email' else '',
            recipient_phone=recipient_phone if template.notification_type == 'sms' else '',
            subject=f"[TEST] {rendered_content['subject']}",
            message=f"[TEST MESSAGE]\n\n{rendered_content['message']}",
            status='pending'
        )
        
        # Send the notification
        send_notification_task.delay(log.id)
        
        return f"Test notification sent using template {template.name}"
    
    except NotificationTemplate.DoesNotExist:
        return f"Template {template_id} not found"
    except Exception as e:
        return f"Error sending test notification: {str(e)}"


@shared_task
def process_scheduled_notifications():
    """Process scheduled notifications that are due with preference enforcement."""
    now = timezone.now()
    tolerance_minutes = 5  # Process notifications within 5 minutes of scheduled time
    
    # Get scheduled notifications that are due
    due_notifications = NotificationSchedule.objects.filter(
        status='scheduled',
        scheduled_for__lte=now + timedelta(minutes=tolerance_minutes),
        scheduled_for__gte=now - timedelta(minutes=tolerance_minutes)
    )
    
    processed_count = 0
    deferred_count = 0
    
    for notification in due_notifications:
        try:
            # Get organizer preferences
            preferences, _ = NotificationPreference.objects.get_or_create(
                organizer=notification.organizer
            )
            
            # Check if we should send now based on preferences
            if not notification.should_send_now(tolerance_minutes):
                continue
            
            # Check DND and weekend preferences
            adjusted_send_time = calculate_send_time_with_preferences(
                notification.scheduled_for,
                preferences,
                notification.schedule_type
            )
            
            # If send time was adjusted, reschedule
            if adjusted_send_time != notification.scheduled_for:
                notification.scheduled_for = adjusted_send_time
                notification.save(update_fields=['scheduled_for'])
                deferred_count += 1
                continue
            
            # Check daily reminder limits
            if notification.schedule_type == 'reminder' and not preferences.can_send_reminder():
                notification.status = 'cancelled'
                notification.error_message = 'Daily reminder limit exceeded'
                notification.save(update_fields=['status', 'error_message'])
                continue
            
            # Create notification log
            log = NotificationLog.objects.create(
                organizer=notification.organizer,
                booking=notification.booking,
                notification_type=notification.notification_type,
                recipient_email=notification.recipient_email,
                recipient_phone=notification.recipient_phone,
                subject=notification.subject,
                message=notification.message,
                status='pending'
            )
            
            # Send the notification
            send_notification_task.delay(log.id)
            
            # Update schedule status
            notification.status = 'sent'
            notification.sent_at = timezone.now()
            notification.save()
            
            processed_count += 1
            
        except Exception as e:
            logger.error(f"Error processing scheduled notification {notification.id}: {str(e)}")
            # Mark as failed
            notification.status = 'failed'
            notification.error_message = str(e)
            notification.save()
    
    return f"Processed {processed_count} scheduled notifications, deferred {deferred_count}"


@shared_task
def send_booking_reminders():
    """Send booking reminders based on user preferences with enhanced logic."""
    from apps.events.models import Booking
    
    # Get confirmed bookings in the next 24 hours
    now = timezone.now()
    upcoming_bookings = Booking.objects.filter(
        status='confirmed',
        start_time__gt=now,
        start_time__lte=now + timedelta(hours=24)
    )
    
    reminders_sent = 0
    reminders_scheduled = 0
    
    for booking in upcoming_bookings:
        try:
            # Get organizer's notification preferences
            preferences, _ = NotificationPreference.objects.get_or_create(
                organizer=booking.organizer
            )
            
            # Check if organizer wants reminders
            if not (preferences.booking_reminders_email or preferences.booking_reminders_sms):
                continue
            
            # Check daily reminder limits
            if not preferences.can_send_reminder():
                logger.info(f"Daily reminder limit reached for {booking.organizer.email}")
                continue
            
            # Calculate reminder time
            reminder_time = booking.start_time - timedelta(minutes=preferences.reminder_minutes_before)
            
            # Adjust for preferences (DND, weekends)
            adjusted_reminder_time = calculate_send_time_with_preferences(
                reminder_time, preferences, 'reminder'
            )
            
            # Check if it's time to send reminder (within 10 minutes window)
            if adjusted_reminder_time <= now <= adjusted_reminder_time + timedelta(minutes=10):
                
                # Check if reminder already sent
                existing_reminder = NotificationLog.objects.filter(
                    booking=booking,
                    notification_type__in=['email', 'sms'],
                    status__in=['sent', 'delivered'],
                    created_at__gte=now - timedelta(hours=1)  # Sent in last hour
                ).exists()
                
                if not existing_reminder:
                    # Send email reminder if enabled
                    if (preferences.booking_reminders_email and 
                        preferences.preferred_notification_method in ['email', 'both']):
                        send_booking_reminder_email(booking, preferences)
                        reminders_sent += 1
                    
                    # Send SMS reminder if enabled
                    if (preferences.booking_reminders_sms and 
                        preferences.preferred_notification_method in ['sms', 'both'] and
                        booking.invitee_phone):
                        send_booking_reminder_sms(booking, preferences)
                        reminders_sent += 1
            
            elif adjusted_reminder_time > now:
                # Schedule for future sending
                existing_schedule = NotificationSchedule.objects.filter(
                    booking=booking,
                    schedule_type='reminder',
                    status='scheduled'
                ).exists()
                
                if not existing_schedule:
                    # Create scheduled reminders
                    if (preferences.booking_reminders_email and 
                        preferences.preferred_notification_method in ['email', 'both']):
                        create_scheduled_reminder(booking, 'email', adjusted_reminder_time, preferences)
                        reminders_scheduled += 1
                    
                    if (preferences.booking_reminders_sms and 
                        preferences.preferred_notification_method in ['sms', 'both'] and
                        booking.invitee_phone):
                        create_scheduled_reminder(booking, 'sms', adjusted_reminder_time, preferences)
                        reminders_scheduled += 1
        
        except Exception as e:
            logger.error(f"Error processing reminder for booking {booking.id}: {str(e)}")
            continue
    
    return f"Sent {reminders_sent} booking reminders, scheduled {reminders_scheduled} for later"


@shared_task
def send_daily_agenda():
    """Send daily agenda emails to organizers with timezone and preference handling."""
    from apps.events.models import Booking
    from datetime import date
    from zoneinfo import ZoneInfo
    
    # Get organizers who want daily agenda
    preferences = NotificationPreference.objects.filter(daily_agenda_email=True)
    
    agendas_sent = 0
    agendas_deferred = 0
    
    for preference in preferences:
        try:
            organizer = preference.organizer
            
            # Calculate today in organizer's timezone
            organizer_tz = ZoneInfo(organizer.profile.timezone_name)
            now_local = timezone.now().astimezone(organizer_tz)
            today_local = now_local.date()
            
            # Check if it's the right time to send agenda
            agenda_time = now_local.replace(
                hour=preference.daily_agenda_time.hour,
                minute=preference.daily_agenda_time.minute,
                second=0,
                microsecond=0
            )
            
            # Only send if within 30 minutes of scheduled time
            time_diff = abs((now_local - agenda_time).total_seconds() / 60)
            if time_diff > 30:
                continue
            
            # Check weekend exclusion
            if preference.should_exclude_weekend('daily_agenda', today_local):
                agendas_deferred += 1
                continue
            
            # Check DND period
            if preference.is_in_dnd_period(now_local.time()):
                agendas_deferred += 1
                continue
            
            # Get today's bookings
            bookings = Booking.objects.filter(
                organizer=organizer,
                status='confirmed',
                start_time__date=today_local
            ).order_by('start_time')
            
            if bookings.exists():
                # Check if agenda already sent today
                existing_agenda = NotificationLog.objects.filter(
                    organizer=organizer,
                    notification_type='email',
                    subject__icontains='agenda',
                    created_at__date=today_local,
                    status__in=['sent', 'delivered']
                ).exists()
                
                if existing_agenda:
                    continue
                
                # Create agenda email
                subject = f"Your agenda for {today_local.strftime('%B %d, %Y')}"
                message = create_daily_agenda_message(bookings, organizer_tz)
                
                # Create notification log
                log = NotificationLog.objects.create(
                    organizer=organizer,
                    notification_type='email',
                    recipient_email=organizer.email,
                    subject=subject,
                    message=message,
                    status='pending'
                )
                
                # Send the notification
                send_notification_task.delay(log.id)
                agendas_sent += 1
        
        except Exception as e:
            logger.error(f"Error sending daily agenda to {preference.organizer.email}: {str(e)}")
            continue
    
    return f"Sent {agendas_sent} daily agenda emails, deferred {agendas_deferred}"


def send_email_notification(log):
    """Send email notification with enhanced error handling and HTML support."""
    try:
        # Prepare email content
        subject = log.subject
        message = log.message
        
        # Try to render HTML template if available
        html_message = None
        try:
            # Check if we have an HTML template for this notification type
            template_name = f"emails/{log.template.template_type}.html" if log.template else "emails/generic.html"
            
            context = get_notification_context_from_booking(log.booking) if log.booking else {}
            context.update({
                'subject': subject,
                'message': message,
                'organizer': log.organizer,
                'site_name': getattr(settings, 'SITE_NAME', 'Calendly Clone')
            })
            
            html_message = render_to_string(template_name, context)
        except Exception as e:
            logger.debug(f"Could not render HTML template: {str(e)}")
            # Continue with plain text
        
        # Send email
        result = send_mail(
            log.subject,
            log.message,
            settings.DEFAULT_FROM_EMAIL,
            [log.recipient_email],
            html_message=html_message,
            fail_silently=False,
        )
        
        if result == 1:  # Django's send_mail returns number of emails sent
            return {
                'success': True,
                'delivery_status': 'sent',
                'provider_response': {'emails_sent': result}
            }
        else:
            return {
                'success': False,
                'error': 'Email sending returned 0 (no emails sent)',
                'provider_response': {'emails_sent': result}
            }
        
    except Exception as e:
        logger.error(f"Email sending error: {str(e)}")
        return {
            'success': False,
            'error': f"Failed to send email: {str(e)}",
            'provider_response': {}
        }


def send_sms_notification(log):
    """Send SMS notification using Twilio with delivery tracking."""
    try:
        # Validate phone number
        phone_validation = validate_phone_number(log.recipient_phone)
        if not phone_validation['valid']:
            return {
                'success': False,
                'error': f"Invalid phone number: {phone_validation['error']}",
                'provider_response': {}
            }
        
        # Get Twilio client
        client = get_twilio_client()
        
        # Sanitize message content
        sanitized_message = sanitize_sms_message(log.message)
        
        # Prepare status callback URL for delivery tracking
        status_callback_url = None
        if hasattr(settings, 'BASE_URL'):
            status_callback_url = f"{settings.BASE_URL}/api/v1/notifications/sms-status-callback/"
        
        # Send SMS
        message = client.messages.create(
            body=sanitized_message,
            from_=settings.TWILIO_PHONE_NUMBER,
            to=phone_validation['formatted'],
            status_callback=status_callback_url
        )
        
        return {
            'success': True,
            'external_id': message.sid,
            'delivery_status': 'queued',  # Twilio initial status
            'provider_response': {
                'sid': message.sid,
                'status': message.status,
                'to': message.to,
                'from': message.from_,
                'price': message.price,
                'price_unit': message.price_unit
            }
        }
        
    except Exception as e:
        logger.error(f"SMS sending error: {str(e)}")
        return {
            'success': False,
            'error': f"Failed to send SMS: {str(e)}",
            'provider_response': {}
        }


def send_booking_reminder_email(booking, preferences):
    """Send booking reminder email."""
    try:
        # Get or create reminder template
        template, _ = NotificationTemplate.objects.get_or_create(
            organizer=booking.organizer,
            template_type='booking_reminder',
            notification_type='email',
            is_default=True,
            defaults={
                'name': 'Default Booking Reminder Email',
                'subject': 'Reminder: {{event_name}} in {{reminder_minutes}} minutes',
                'message': '''Hi {{invitee_name}},

This is a reminder that you have a meeting coming up:

Event: {{event_name}}
Time: {{start_time_invitee}} ({{invitee_timezone}})
Duration: {{duration}}

{% if meeting_link %}Meeting Link: {{meeting_link}}{% endif %}

See you soon!

Best regards,
{{organizer_name}}''',
                'required_placeholders': ['invitee_name', 'event_name', 'start_time_invitee']
            }
        )
        
        # Create context with reminder-specific data
        context = get_notification_context_from_booking(booking)
        context['reminder_minutes'] = preferences.reminder_minutes_before
        
        # Create notification log
        from .utils import create_notification_from_template
        log = create_notification_from_template(
            template=template,
            booking=booking,
            custom_context=context,
            recipient_email=booking.invitee_email
        )
        
        # Send immediately
        send_notification_task.delay(log.id)
        
    except Exception as e:
        logger.error(f"Error creating booking reminder email: {str(e)}")
    
    log = NotificationLog.objects.create(
        organizer=booking.organizer,
        booking=booking,
        notification_type='email',
        recipient_email=booking.invitee_email,
        subject=subject,
        message=message,
        status='pending'
    )
    
    send_notification_task.delay(log.id)


def send_booking_reminder_sms(booking, preferences):
    """Send booking reminder SMS."""
    try:
        if not booking.invitee_phone:
            logger.warning(f"No phone number for booking {booking.id}")
            return
        
        # Get or create SMS reminder template
        template, _ = NotificationTemplate.objects.get_or_create(
            organizer=booking.organizer,
            template_type='booking_reminder',
            notification_type='sms',
            is_default=True,
            defaults={
                'name': 'Default Booking Reminder SMS',
                'subject': '',  # SMS doesn't use subject
                'message': 'Reminder: {{event_name}} with {{organizer_name}} in {{reminder_minutes}} minutes. {{meeting_link}}',
                'required_placeholders': ['event_name', 'organizer_name', 'reminder_minutes']
            }
        )
        
        # Create context
        context = get_notification_context_from_booking(booking)
        context['reminder_minutes'] = preferences.reminder_minutes_before
        
        # Create notification log
        from .utils import create_notification_from_template
        log = create_notification_from_template(
            template=template,
            booking=booking,
            custom_context=context,
            recipient_phone=booking.invitee_phone
        )
        
        # Send immediately
        send_notification_task.delay(log.id)
        
    except Exception as e:
        logger.error(f"Error creating booking reminder SMS: {str(e)}")


def create_daily_agenda_message(bookings, organizer_tz):
    """Create daily agenda email message."""
    from .utils import format_time_for_timezone
    
    if not bookings:
        return "You have no meetings scheduled for today. Enjoy your day!"
    
    message = f"Here's your agenda for today ({bookings.count()} meeting{'s' if bookings.count() != 1 else ''}):\n\n"
    
    for booking in bookings:
        start_time_local = format_time_for_timezone(
            booking.start_time, 
            str(organizer_tz), 
            '%I:%M %p'
        )
        
        message += f"• {start_time_local} - {booking.event_type.name}\n"
        message += f"  with {booking.invitee_name} ({booking.invitee_email})\n"
        
        if booking.meeting_link:
            message += f"  Meeting Link: {booking.meeting_link}\n"
        
        if booking.invitee_phone:
            message += f"  Phone: {booking.invitee_phone}\n"
        
        message += "\n"
    
    message += "Have a great day!"
    return message


def create_scheduled_reminder(booking, notification_type, send_time, preferences):
    """Create a scheduled reminder notification."""
    try:
        # Create context
        context = get_notification_context_from_booking(booking)
        context['reminder_minutes'] = preferences.reminder_minutes_before
        
        # Determine recipient
        recipient_email = booking.invitee_email if notification_type == 'email' else ''
        recipient_phone = booking.invitee_phone if notification_type == 'sms' else ''
        
        # Create subject and message
        if notification_type == 'email':
            subject = f"Reminder: {booking.event_type.name} in {preferences.reminder_minutes_before} minutes"
            message = f"""Hi {booking.invitee_name},

This is a reminder that you have a meeting coming up:

Event: {booking.event_type.name}
Time: {context['start_time_invitee']} ({booking.invitee_timezone})
Duration: {booking.event_type.duration} minutes

{f"Meeting Link: {booking.meeting_link}" if booking.meeting_link else ""}

See you soon!

Best regards,
{booking.organizer.first_name}"""
        else:  # SMS
            subject = ''
            message = f"Reminder: {booking.event_type.name} with {booking.organizer.first_name} in {preferences.reminder_minutes_before} minutes. {booking.meeting_link or ''}"
        
        # Create scheduled notification
        NotificationSchedule.objects.create(
            organizer=booking.organizer,
            booking=booking,
            schedule_type='reminder',
            notification_type=notification_type,
            scheduled_for=send_time,
            recipient_email=recipient_email,
            recipient_phone=recipient_phone,
            subject=subject,
            message=message,
            status='scheduled'
        )
        
    except Exception as e:
        logger.error(f"Error creating scheduled reminder: {str(e)}")


@shared_task
def trigger_notification_webhook(notification_log_id):
    """Trigger webhook for notification events."""
    try:
        log = NotificationLog.objects.get(id=notification_log_id)
        
        # Get webhook integrations for this organizer
        from apps.integrations.models import WebhookIntegration
        webhooks = WebhookIntegration.objects.filter(
            organizer=log.organizer,
            is_active=True,
            events__contains=['notification_sent']
        )
        
        for webhook in webhooks:
            try:
                payload = create_webhook_payload(log)
                
                # Send webhook
                from apps.integrations.tasks import send_webhook
                send_webhook.delay(webhook.id, 'notification_sent', payload)
                
            except Exception as e:
                logger.error(f"Error triggering webhook for notification {notification_log_id}: {str(e)}")
        
        return f"Triggered {webhooks.count()} webhooks for notification {notification_log_id}"
        
    except NotificationLog.DoesNotExist:
        return f"Notification log {notification_log_id} not found"
    except Exception as e:
        return f"Error triggering notification webhook: {str(e)}"


@shared_task
def cleanup_old_notification_logs():
    """Clean up old notification logs to prevent database bloat."""
    cutoff_date = timezone.now() - timedelta(days=90)  # Keep 90 days
    
    old_logs = NotificationLog.objects.filter(created_at__lt=cutoff_date)
    count = old_logs.count()
    old_logs.delete()
    
    return f"Cleaned up {count} old notification logs"


@shared_task
def monitor_notification_failures():
    """Monitor for repeated notification failures and alert admins."""
    from datetime import timedelta
    
    # Check for high failure rates in the last hour
    one_hour_ago = timezone.now() - timedelta(hours=1)
    
    recent_logs = NotificationLog.objects.filter(created_at__gte=one_hour_ago)
    total_recent = recent_logs.count()
    failed_recent = recent_logs.filter(status='failed').count()
    
    if total_recent > 0:
        failure_rate = (failed_recent / total_recent) * 100
        
        # Alert if failure rate > 20%
        if failure_rate > 20 and total_recent >= 10:
            alert_admins_of_notification_failures.delay(failure_rate, failed_recent, total_recent)
    
    # Check for organizers with consistently failing notifications
    problem_organizers = NotificationLog.objects.filter(
        created_at__gte=one_hour_ago,
        status='failed'
    ).values('organizer').annotate(
        failure_count=models.Count('id')
    ).filter(failure_count__gte=5)
    
    for organizer_data in problem_organizers:
        alert_organizer_of_notification_issues.delay(organizer_data['organizer'])
    
    return f"Monitoring completed: {failure_rate:.1f}% failure rate, {len(problem_organizers)} problem organizers"


@shared_task
def alert_admins_of_notification_failures(failure_rate, failed_count, total_count):
    """Alert administrators of high notification failure rates."""
    try:
        subject = f"High Notification Failure Rate Alert: {failure_rate:.1f}%"
        message = f"""
Alert: High notification failure rate detected

Failure Rate: {failure_rate:.1f}%
Failed Notifications: {failed_count}
Total Notifications: {total_count}
Time Period: Last 1 hour

Please investigate the notification system for potential issues.

Common causes:
- Email server issues
- SMS provider (Twilio) issues
- Network connectivity problems
- Invalid recipient data

Check the admin panel for detailed error logs.
        """
        
        admin_emails = getattr(settings, 'ADMIN_NOTIFICATION_EMAILS', [])
        if admin_emails:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                admin_emails,
                fail_silently=True,
            )
        
        return f"Admin alert sent to {len(admin_emails)} recipients"
        
    except Exception as e:
        logger.error(f"Error sending admin alert: {str(e)}")
        return f"Error sending admin alert: {str(e)}"


@shared_task
def alert_organizer_of_notification_issues(organizer_id):
    """Alert organizer of repeated notification failures."""
    try:
        from apps.users.models import User
        organizer = User.objects.get(id=organizer_id)
        
        subject = "Notification Delivery Issues"
        message = f"""
Hi {organizer.first_name},

We've detected some issues with delivering notifications from your account. 
This could affect booking confirmations, reminders, and other important communications.

Possible causes:
- Invalid email addresses in your contact list
- SMS delivery issues (check phone number formats)
- Temporary service provider issues

Please check your notification settings and contact information.
If the problem persists, please contact our support team.

Best regards,
The Calendly Clone Team
        """
        
        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            [organizer.email],
            fail_silently=True,
        )
        
        return f"Issue alert sent to {organizer.email}"
        
    except User.DoesNotExist:
        return f"Organizer {organizer_id} not found"
    except Exception as e:
        return f"Error sending organizer alert: {str(e)}"


@shared_task
def send_booking_notification(booking_id, event_type):
    """Send booking-related notifications (confirmation, cancellation, etc.)."""
    try:
        from apps.events.models import Booking
        booking = Booking.objects.get(id=booking_id)
        
        # Get organizer preferences
        preferences, _ = NotificationPreference.objects.get_or_create(
            organizer=booking.organizer
        )
        
        # Determine which notifications to send based on event type and preferences
        if event_type == 'created':
            if preferences.booking_confirmations_email:
                send_booking_confirmation_email(booking, preferences)
            if preferences.booking_confirmations_sms and booking.invitee_phone:
                send_booking_confirmation_sms(booking, preferences)
            
            # Schedule reminder if enabled
            if preferences.booking_reminders_email or preferences.booking_reminders_sms:
                schedule_booking_reminder(booking, preferences)
        
        elif event_type == 'cancelled':
            if preferences.booking_cancellations_email:
                send_booking_cancellation_email(booking, preferences)
            if preferences.booking_cancellations_sms and booking.invitee_phone:
                send_booking_cancellation_sms(booking, preferences)
        
        elif event_type == 'rescheduled':
            # Send rescheduling notification
            send_booking_rescheduled_email(booking, preferences)
            if booking.invitee_phone:
                send_booking_rescheduled_sms(booking, preferences)
        
        return f"Booking notifications sent for {event_type}: {booking_id}"
        
    except Booking.DoesNotExist:
        return f"Booking {booking_id} not found"
    except Exception as e:
        logger.error(f"Error sending booking notification: {str(e)}")
        return f"Error sending booking notification: {str(e)}"


def send_booking_confirmation_email(booking, preferences):
    """Send booking confirmation email."""
    try:
        template, _ = NotificationTemplate.objects.get_or_create(
            organizer=booking.organizer,
            template_type='booking_confirmation',
            notification_type='email',
            is_default=True,
            defaults={
                'name': 'Default Booking Confirmation Email',
                'subject': 'Booking Confirmed: {{event_name}}',
                'message': '''Hi {{invitee_name}},

Your booking has been confirmed!

Event: {{event_name}}
Date & Time: {{start_time_invitee}} ({{invitee_timezone}})
Duration: {{duration}}
Host: {{organizer_name}}

{% if meeting_link %}Meeting Link: {{meeting_link}}{% endif %}
{% if meeting_id %}Meeting ID: {{meeting_id}}{% endif %}
{% if meeting_password %}Password: {{meeting_password}}{% endif %}

We'll send you a reminder before the meeting.

Best regards,
{{organizer_name}}''',
                'required_placeholders': ['invitee_name', 'event_name', 'start_time_invitee', 'organizer_name']
            }
        )
        
        from .utils import create_notification_from_template
        log = create_notification_from_template(
            template=template,
            booking=booking,
            recipient_email=booking.invitee_email
        )
        
        send_notification_task.delay(log.id)
        
    except Exception as e:
        logger.error(f"Error creating booking confirmation email: {str(e)}")


def send_booking_confirmation_sms(booking, preferences):
    """Send booking confirmation SMS."""
    try:
        template, _ = NotificationTemplate.objects.get_or_create(
            organizer=booking.organizer,
            template_type='booking_confirmation',
            notification_type='sms',
            is_default=True,
            defaults={
                'name': 'Default Booking Confirmation SMS',
                'subject': '',
                'message': 'Booking confirmed: {{event_name}} with {{organizer_name}} on {{start_time_invitee}}. {{meeting_link}}',
                'required_placeholders': ['event_name', 'organizer_name', 'start_time_invitee']
            }
        )
        
        from .utils import create_notification_from_template
        log = create_notification_from_template(
            template=template,
            booking=booking,
            recipient_phone=booking.invitee_phone
        )
        
        send_notification_task.delay(log.id)
        
    except Exception as e:
        logger.error(f"Error creating booking confirmation SMS: {str(e)}")


def send_booking_cancellation_email(booking, preferences):
    """Send booking cancellation email."""
    try:
        template, _ = NotificationTemplate.objects.get_or_create(
            organizer=booking.organizer,
            template_type='booking_cancellation',
            notification_type='email',
            is_default=True,
            defaults={
                'name': 'Default Booking Cancellation Email',
                'subject': 'Booking Cancelled: {{event_name}}',
                'message': '''Hi {{invitee_name}},

Your booking has been cancelled.

Event: {{event_name}}
Date & Time: {{start_time_invitee}} ({{invitee_timezone}})
Host: {{organizer_name}}

{% if cancellation_reason %}Reason: {{cancellation_reason}}{% endif %}

You can book a new time at any time.

Best regards,
{{organizer_name}}''',
                'required_placeholders': ['invitee_name', 'event_name', 'organizer_name']
            }
        )
        
        from .utils import create_notification_from_template
        log = create_notification_from_template(
            template=template,
            booking=booking,
            recipient_email=booking.invitee_email
        )
        
        send_notification_task.delay(log.id)
        
    except Exception as e:
        logger.error(f"Error creating booking cancellation email: {str(e)}")


def send_booking_cancellation_sms(booking, preferences):
    """Send booking cancellation SMS."""
    try:
        template, _ = NotificationTemplate.objects.get_or_create(
            organizer=booking.organizer,
            template_type='booking_cancellation',
            notification_type='sms',
            is_default=True,
            defaults={
                'name': 'Default Booking Cancellation SMS',
                'subject': '',
                'message': 'Booking cancelled: {{event_name}} with {{organizer_name}} on {{start_time_invitee}}. {% if cancellation_reason %}Reason: {{cancellation_reason}}{% endif %}',
                'required_placeholders': ['event_name', 'organizer_name', 'start_time_invitee']
            }
        )
        
        from .utils import create_notification_from_template
        log = create_notification_from_template(
            template=template,
            booking=booking,
            recipient_phone=booking.invitee_phone
        )
        
        send_notification_task.delay(log.id)
        
    except Exception as e:
        logger.error(f"Error creating booking cancellation SMS: {str(e)}")


def send_booking_rescheduled_email(booking, preferences):
    """Send booking rescheduled email."""
    try:
        template, _ = NotificationTemplate.objects.get_or_create(
            organizer=booking.organizer,
            template_type='booking_rescheduled',
            notification_type='email',
            is_default=True,
            defaults={
                'name': 'Default Booking Rescheduled Email',
                'subject': 'Meeting Rescheduled: {{event_name}}',
                'message': '''Hi {{invitee_name}},

Your meeting has been rescheduled.

Event: {{event_name}}
New Date & Time: {{start_time_invitee}} ({{invitee_timezone}})
Duration: {{duration}}
Host: {{organizer_name}}

{% if meeting_link %}Meeting Link: {{meeting_link}}{% endif %}

We'll send you a reminder before the meeting.

Best regards,
{{organizer_name}}''',
                'required_placeholders': ['invitee_name', 'event_name', 'start_time_invitee', 'organizer_name']
            }
        )
        
        from .utils import create_notification_from_template
        log = create_notification_from_template(
            template=template,
            booking=booking,
            recipient_email=booking.invitee_email
        )
        
        send_notification_task.delay(log.id)
        
    except Exception as e:
        logger.error(f"Error creating booking rescheduled email: {str(e)}")


def send_booking_rescheduled_sms(booking, preferences):
    """Send booking rescheduled SMS."""
    try:
        template, _ = NotificationTemplate.objects.get_or_create(
            organizer=booking.organizer,
            template_type='booking_rescheduled',
            notification_type='sms',
            is_default=True,
            defaults={
                'name': 'Default Booking Rescheduled SMS',
                'subject': '',
                'message': 'Meeting rescheduled: {{event_name}} with {{organizer_name}} now on {{start_time_invitee}}. {{meeting_link}}',
                'required_placeholders': ['event_name', 'organizer_name', 'start_time_invitee']
            }
        )
        
        from .utils import create_notification_from_template
        log = create_notification_from_template(
            template=template,
            booking=booking,
            recipient_phone=booking.invitee_phone
        )
        
        send_notification_task.delay(log.id)
        
    except Exception as e:
        logger.error(f"Error creating booking rescheduled SMS: {str(e)}")


def schedule_booking_reminder(booking, preferences):
    """Schedule booking reminder based on preferences."""
    try:
        from .utils import calculate_reminder_send_time
        
        # Calculate when to send reminder
        reminder_time = calculate_reminder_send_time(
            booking, 
            preferences.reminder_minutes_before, 
            preferences
        )
        
        # Only schedule if reminder time is in the future
        if reminder_time > timezone.now():
            # Schedule email reminder
            if (preferences.booking_reminders_email and 
                preferences.preferred_notification_method in ['email', 'both']):
                create_scheduled_reminder(booking, 'email', reminder_time, preferences)
            
            # Schedule SMS reminder
            if (preferences.booking_reminders_sms and 
                preferences.preferred_notification_method in ['sms', 'both'] and
                booking.invitee_phone):
                create_scheduled_reminder(booking, 'sms', reminder_time, preferences)
        
    except Exception as e:
        logger.error(f"Error scheduling booking reminder: {str(e)}")


@shared_task
def retry_failed_notifications():
    """Retry failed notifications that can still be retried."""
    # Get failed notifications that can be retried
    retryable_logs = NotificationLog.objects.filter(
        status='failed',
        retry_count__lt=models.F('max_retries'),
        created_at__gte=timezone.now() - timedelta(hours=24)  # Only retry recent failures
    )
    
    retry_count = 0
    
    for log in retryable_logs:
        try:
            # Reset status to pending for retry
            log.status = 'pending'
            log.save(update_fields=['status'])
            
            # Schedule retry with delay
            retry_delay = min(300, 60 * log.retry_count)  # Max 5 minutes
            send_notification_task.apply_async(
                args=[log.id, log.retry_count],
                countdown=retry_delay
            )
            
            retry_count += 1
            
        except Exception as e:
            logger.error(f"Error scheduling retry for notification {log.id}: {str(e)}")
    
    return f"Scheduled {retry_count} notifications for retry"