from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone
from .models import User, EmailVerificationToken, PasswordResetToken, Invitation, UserSession
from datetime import timedelta


@shared_task
def send_welcome_email(user_id):
    """Send welcome email to new users."""
    try:
        user = User.objects.get(id=user_id)
        
        subject = 'Welcome to Calendly Clone!'
        html_message = render_to_string('emails/welcome.html', {
            'user': user,
            'site_name': 'Calendly Clone',
            'site_url': settings.FRONTEND_URL if hasattr(settings, 'FRONTEND_URL') else 'http://localhost:3000'
        })
        plain_message = strip_tags(html_message)
        
        send_mail(
            subject,
            plain_message,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            html_message=html_message,
            fail_silently=False,
        )
        return f"Welcome email sent to {user.email}"
    except User.DoesNotExist:
        return f"User with id {user_id} not found"
    except Exception as e:
        return f"Failed to send welcome email: {str(e)}"


@shared_task
def send_verification_email(user_id):
    """Send email verification email."""
    try:
        user = User.objects.get(id=user_id)
        
        # Invalidate existing tokens
        EmailVerificationToken.objects.filter(
            user=user,
            token_type='email_verification',
            used_at__isnull=True
        ).update(used_at=timezone.now())
        
        # Create new token
        token = EmailVerificationToken.objects.create(
            user=user,
            email=user.email,
            token_type='email_verification'
        )
        
        verification_url = f"{settings.FRONTEND_URL if hasattr(settings, 'FRONTEND_URL') else 'http://localhost:3000'}/verify-email?token={token.token}"
        
        subject = 'Verify your email address'
        html_message = render_to_string('emails/email_verification.html', {
            'user': user,
            'verification_url': verification_url,
            'site_name': 'Calendly Clone'
        })
        plain_message = strip_tags(html_message)
        
        send_mail(
            subject,
            plain_message,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            html_message=html_message,
            fail_silently=False,
        )
        return f"Verification email sent to {user.email}"
    except User.DoesNotExist:
        return f"User with id {user_id} not found"
    except Exception as e:
        return f"Failed to send verification email: {str(e)}"


@shared_task
def send_password_reset_email(user_id, token_or_message=None):
    """Send password reset email."""
    try:
        user = User.objects.get(id=user_id)
        
        # Handle both token-based reset and password expiry notification
        if token_or_message and len(token_or_message) > 50:  # Assume it's a message if long
            # This is a password expiry notification
            subject = 'Password Expired - Reset Required'
            message = token_or_message
            reset_url = f"{settings.FRONTEND_URL if hasattr(settings, 'FRONTEND_URL') else 'http://localhost:3000'}/request-password-reset"
        else:
            # This is a normal password reset with token
            subject = 'Reset your password'
            message = f"We received a request to reset the password for your {settings.SITE_NAME if hasattr(settings, 'SITE_NAME') else 'Calendly Clone'} account."
            reset_url = f"{settings.FRONTEND_URL if hasattr(settings, 'FRONTEND_URL') else 'http://localhost:3000'}/reset-password?token={token_or_message}"
        
        html_message = render_to_string('emails/password_reset.html', {
            'user': user,
            'reset_url': reset_url,
            'message': message,
            'site_name': 'Calendly Clone'
        })
        plain_message = strip_tags(html_message)
        
        send_mail(
            subject,
            plain_message,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            html_message=html_message,
            fail_silently=False,
        )
        return f"Password reset email sent to {user.email}"
    except User.DoesNotExist:
        return f"User with id {user_id} not found"
    except Exception as e:
        return f"Failed to send password reset email: {str(e)}"


@shared_task
def send_invitation_email(invitation_id):
    """Send team invitation email."""
    try:
        invitation = Invitation.objects.get(id=invitation_id)
        
        invitation_url = f"{settings.FRONTEND_URL if hasattr(settings, 'FRONTEND_URL') else 'http://localhost:3000'}/invitation?token={invitation.token}"
        
        subject = f'You\'re invited to join {invitation.invited_by.get_full_name()}\'s team'
        html_message = render_to_string('emails/invitation.html', {
            'invitation': invitation,
            'invitation_url': invitation_url,
            'site_name': 'Calendly Clone'
        })
        plain_message = strip_tags(html_message)
        
        send_mail(
            subject,
            plain_message,
            settings.DEFAULT_FROM_EMAIL,
            [invitation.invited_email],
            html_message=html_message,
            fail_silently=False,
        )
        return f"Invitation email sent to {invitation.invited_email}"
    except Invitation.DoesNotExist:
        return f"Invitation with id {invitation_id} not found"
    except Exception as e:
        return f"Failed to send invitation email: {str(e)}"


@shared_task
def cleanup_expired_tokens():
    """Clean up expired tokens."""
    now = timezone.now()
    
    # Clean up expired email verification tokens
    expired_email_tokens = EmailVerificationToken.objects.filter(
        expires_at__lt=now,
        used_at__isnull=True
    )
    email_count = expired_email_tokens.count()
    expired_email_tokens.delete()
    
    # Clean up expired password reset tokens
    expired_password_tokens = PasswordResetToken.objects.filter(
        expires_at__lt=now,
        used_at__isnull=True
    )
    password_count = expired_password_tokens.count()
    expired_password_tokens.delete()
    
    # Clean up expired invitations
    expired_invitations = Invitation.objects.filter(
        expires_at__lt=now,
        status='pending'
    )
    invitation_count = expired_invitations.count()
    expired_invitations.update(status='expired')
    
    # Clean up old inactive sessions
    old_sessions = UserSession.objects.filter(
        is_active=False,
        last_activity__lt=now - timedelta(days=30)
    )
    session_count = old_sessions.count()
    old_sessions.delete()
    
    return f"Cleaned up {email_count} email tokens, {password_count} password tokens, {invitation_count} invitations, {session_count} old sessions"


@shared_task
def create_default_permissions():
    """Create default permissions for the system."""
    from .models import Permission, Role
    
    default_permissions = [
        # User management
        ('can_view_users', 'View Users', 'Can view user list and details', 'user_management'),
        ('can_create_users', 'Create Users', 'Can create new user accounts', 'user_management'),
        ('can_edit_users', 'Edit Users', 'Can edit user accounts', 'user_management'),
        ('can_delete_users', 'Delete Users', 'Can delete user accounts', 'user_management'),
        
        # Event management
        ('can_view_events', 'View Events', 'Can view event types and bookings', 'event_management'),
        ('can_create_events', 'Create Events', 'Can create event types', 'event_management'),
        ('can_edit_events', 'Edit Events', 'Can edit event types', 'event_management'),
        ('can_delete_events', 'Delete Events', 'Can delete event types', 'event_management'),
        ('can_manage_bookings', 'Manage Bookings', 'Can manage all bookings', 'event_management'),
        
        # System administration
        ('can_view_admin', 'View Admin', 'Can access admin interface', 'administration'),
        ('can_manage_roles', 'Manage Roles', 'Can create and assign roles', 'administration'),
        ('can_view_audit_logs', 'View Audit Logs', 'Can view system audit logs', 'administration'),
        ('can_manage_integrations', 'Manage Integrations', 'Can manage external integrations', 'administration'),
        
        # Billing and subscriptions
        ('can_view_billing', 'View Billing', 'Can view billing information', 'billing'),
        ('can_manage_billing', 'Manage Billing', 'Can manage billing and subscriptions', 'billing'),
        
        # Reports and analytics
        ('can_view_reports', 'View Reports', 'Can view reports and analytics', 'reporting'),
        ('can_export_data', 'Export Data', 'Can export system data', 'reporting'),
    ]
    
    created_count = 0
    for codename, name, description, category in default_permissions:
        permission, created = Permission.objects.get_or_create(
            codename=codename,
            defaults={
                'name': name,
                'description': description,
                'category': category
            }
        )
        if created:
            created_count += 1
    
    return f"Created {created_count} default permissions"


@shared_task
def cleanup_inactive_users():
    """Clean up inactive users who haven't verified email after 30 days."""
    cutoff_date = timezone.now() - timedelta(days=30)
    
    inactive_users = User.objects.filter(
        is_email_verified=False,
        account_status='pending_verification',
        date_joined__lt=cutoff_date
    )
    
    count = inactive_users.count()
    inactive_users.delete()
    
    return f"Cleaned up {count} inactive users"


@shared_task
def unlock_locked_accounts():
    """Unlock accounts that have passed their lock duration."""
    now = timezone.now()
    
    locked_users = User.objects.filter(
        locked_until__lt=now,
        locked_until__isnull=False
    )
    
    count = locked_users.count()
    locked_users.update(
        locked_until=None,
        failed_login_attempts=0
    )
    
    return f"Unlocked {count} accounts"


@shared_task
def send_password_expiry_warning(user_id):
    """Send password expiry warning email."""
    try:
        user = User.objects.get(id=user_id)
        
        if not user.password_expires_at:
            return "User password does not expire"
        
        days_until_expiry = (user.password_expires_at - timezone.now()).days
        
        subject = f'Your password expires in {days_until_expiry} days'
        html_message = render_to_string('emails/password_expiry_warning.html', {
            'user': user,
            'days_until_expiry': days_until_expiry,
            'change_password_url': f"{settings.FRONTEND_URL if hasattr(settings, 'FRONTEND_URL') else 'http://localhost:3000'}/change-password",
            'site_name': 'Calendly Clone'
        })
        plain_message = strip_tags(html_message)
        
        send_mail(
            subject,
            plain_message,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            html_message=html_message,
            fail_silently=False,
        )
        return f"Password expiry warning sent to {user.email}"
    except User.DoesNotExist:
        return f"User with id {user_id} not found"
    except Exception as e:
        return f"Failed to send password expiry warning: {str(e)}"


@shared_task
def check_password_expiries_and_warn():
    """Check for passwords nearing expiry and send warnings."""
    from django.conf import settings
    from datetime import timedelta
    
    if not hasattr(settings, 'PASSWORD_EXPIRY_DAYS') or settings.PASSWORD_EXPIRY_DAYS <= 0:
        return "Password expiry is disabled"
    
    warning_days = getattr(settings, 'PASSWORD_EXPIRY_WARNING_DAYS', 7)
    warning_threshold = timezone.now() + timedelta(days=warning_days)
    
    # Find users whose passwords will expire within the warning period
    users_to_warn = User.objects.filter(
        is_active=True,
        account_status='active',
        password_expires_at__lte=warning_threshold,
        password_expires_at__gt=timezone.now()
    )
    
    warned_count = 0
    for user in users_to_warn:
        # Check if we've already sent a warning recently (within last 24 hours)
        recent_warning = user.audit_logs.filter(
            action='password_expiry_warning_sent',
            created_at__gte=timezone.now() - timedelta(hours=24)
        ).exists()
        
        if not recent_warning:
            send_password_expiry_warning.delay(user.id)
            
            # Create audit log
            from .utils import create_audit_log
            create_audit_log(
                user=user,
                action='password_expiry_warning_sent',
                description=f"Password expiry warning sent - expires in {user.days_until_password_expiry()} days",
                metadata={'days_until_expiry': user.days_until_password_expiry()}
            )
            warned_count += 1
    
    return f"Sent password expiry warnings to {warned_count} users"


@shared_task
def cleanup_expired_grace_periods():
    """Clean up users whose grace period has expired."""
    from django.conf import settings
    from datetime import timedelta
    
    if not hasattr(settings, 'PASSWORD_EXPIRY_DAYS') or settings.PASSWORD_EXPIRY_DAYS <= 0:
        return "Password expiry is disabled"
    
    grace_period_hours = getattr(settings, 'PASSWORD_EXPIRY_GRACE_PERIOD_HOURS', 24)
    
    # Find users whose grace period has expired
    expired_grace_users = User.objects.filter(
        account_status='password_expired_grace_period',
        password_expires_at__lt=timezone.now() - timedelta(hours=grace_period_hours)
    )
    
    count = expired_grace_users.count()
    
    # Move them to fully expired status
    expired_grace_users.update(account_status='password_expired')
    
    # Send password reset emails for these users
    for user in expired_grace_users:
        send_password_reset_email.delay(
            user.id, 
            "Your password has expired and the grace period has ended. Please reset your password to regain access."
        )
        
        # Create audit log
        from .utils import create_audit_log
        create_audit_log(
            user=user,
            action='password_grace_period_expired',
            description="Password grace period expired, account moved to password_expired status",
            metadata={'grace_period_hours': grace_period_hours}
        )
    
    return f"Processed {count} users whose grace period expired"


@shared_task
def send_sms_verification(user_id, phone_number):
    """Send SMS verification code for MFA setup."""
    try:
        from twilio.rest import Client
        from django.conf import settings
        from django.core.cache import cache
        import random
        
        user = User.objects.get(id=user_id)
        
        # Generate 6-digit code
        code = f"{random.randint(100000, 999999)}"
        
        # Store code in cache for 5 minutes
        cache_key = f"sms_otp_{user_id}"
        cache.set(cache_key, code, timeout=300)  # 5 minutes
        
        # Send SMS using Twilio
        if hasattr(settings, 'TWILIO_ACCOUNT_SID') and hasattr(settings, 'TWILIO_AUTH_TOKEN'):
            client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
            
            message = client.messages.create(
                body=f"Your Calendly Clone verification code is: {code}",
                from_=settings.TWILIO_PHONE_NUMBER,
                to=phone_number
            )
            
            # Create audit log
            from .utils import create_audit_log
            create_audit_log(
                user=user,
                action='sms_otp_sent',
                description=f"SMS OTP sent to {phone_number}",
                metadata={'phone_number': phone_number, 'message_sid': message.sid}
            )
            
            return f"SMS sent to {phone_number}: {message.sid}"
        else:
            # For development, just log the code
            print(f"SMS verification code for {phone_number}: {code}")
            return f"SMS verification code (dev mode): {code}"
            
    except User.DoesNotExist:
        return f"User {user_id} not found"
    except Exception as e:
        return f"Failed to send SMS: {str(e)}"


@shared_task
def send_sms_mfa_code(user_id, device_id):
    """Send SMS MFA code during login."""
    try:
        from twilio.rest import Client
        from django.conf import settings
        from django.core.cache import cache
        from .models import MFADevice
        import random
        
        user = User.objects.get(id=user_id)
        device = MFADevice.objects.get(id=device_id, user=user, device_type='sms', is_active=True)
        
        # Check rate limiting
        if not device.can_attempt_verification():
            return f"Rate limit exceeded for device {device_id}"
        
        # Generate 6-digit code
        code = f"{random.randint(100000, 999999)}"
        
        # Store code in cache for 5 minutes
        cache_key = f"sms_mfa_{user_id}_{device_id}"
        cache.set(cache_key, code, timeout=300)  # 5 minutes
        
        # Send SMS using Twilio
        if hasattr(settings, 'TWILIO_ACCOUNT_SID') and hasattr(settings, 'TWILIO_AUTH_TOKEN'):
            client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
            
            message = client.messages.create(
                body=f"Your Calendly Clone login code is: {code}",
                from_=settings.TWILIO_PHONE_NUMBER,
                to=device.phone_number
            )
            
            # Create audit log
            from .utils import create_audit_log
            create_audit_log(
                user=user,
                action='sms_mfa_sent',
                description=f"SMS MFA code sent to {device.phone_number}",
                metadata={'phone_number': device.phone_number, 'message_sid': message.sid, 'device_id': str(device_id)}
            )
            
            return f"SMS MFA code sent to {device.phone_number}: {message.sid}"
        else:
            # For development, just log the code
            print(f"SMS MFA code for {device.phone_number}: {code}")
            return f"SMS MFA code (dev mode): {code}"
            
    except User.DoesNotExist:
        return f"User {user_id} not found"
    except MFADevice.DoesNotExist:
        return f"MFA device {device_id} not found"
    except Exception as e:
        return f"Failed to send SMS MFA code: {str(e)}"


@shared_task
def cleanup_expired_mfa_sessions():
    """Clean up expired MFA sessions and unused secrets."""
    from datetime import timedelta
    
    # Clean up users who started MFA setup but never completed it
    cutoff_date = timezone.now() - timedelta(hours=1)
    
    incomplete_mfa_users = User.objects.filter(
        mfa_secret__isnull=False,
        is_mfa_enabled=False,
        updated_at__lt=cutoff_date
    )
    
    count = 0
    for user in incomplete_mfa_users:
        user.mfa_secret = ''
        user.save(update_fields=['mfa_secret'])
        count += 1
    
    return f"Cleaned up {count} incomplete MFA setups"