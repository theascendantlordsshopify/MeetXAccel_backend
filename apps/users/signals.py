from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.utils import timezone
from .models import User, Profile, AuditLog, UserSession, PasswordHistory
from .utils import get_client_ip, get_user_agent, create_audit_log, parse_user_agent, get_geolocation_from_ip


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """Create a profile when a new user is created."""
    if created and instance.is_organizer:
        Profile.objects.create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    """Save the profile when the user is saved."""
    if instance.is_organizer and hasattr(instance, 'profile'):
        instance.profile.save()


@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    """Log successful user login."""
    ip_address = get_client_ip(request)
    user_agent = get_user_agent(request)
    
    # Update user's last login IP
    user.last_login_ip = ip_address
    user.save(update_fields=['last_login_ip'])
    
    # Create or update session record
    session_key = request.session.session_key
    if session_key:
        # Get geolocation data
        geo_data = get_geolocation_from_ip(ip_address)
        device_info = parse_user_agent(user_agent)
        
        UserSession.objects.update_or_create(
            user=user,
            session_key=session_key,
            defaults={
                'ip_address': ip_address,
                'country': geo_data['country'],
                'city': geo_data['city'],
                'user_agent': user_agent,
                'device_info': device_info,
                'expires_at': timezone.now() + timezone.timedelta(days=30),
                'is_active': True
            }
        )


@receiver(user_logged_out)
def log_user_logout(sender, request, user, **kwargs):
    """Log user logout."""
    if user:
        # Deactivate session
        session_key = request.session.session_key
        if session_key:
            # Delete Django session
            try:
                from django.contrib.sessions.models import Session
                Session.objects.filter(session_key=session_key).delete()
            except:
                pass
            
            # Deactivate our session record
            UserSession.objects.filter(
                user=user,
                session_key=session_key
            ).update(is_active=False)


@receiver(user_login_failed)
def log_failed_login(sender, credentials, request, **kwargs):
    """Log failed login attempts."""
    email = credentials.get('username')  # Django uses 'username' even for email
    if email:
        try:
            user = User.objects.get(email=email)
            create_audit_log(
                user=user,
                action='login_failed',
                description=f"Failed login attempt from {get_client_ip(request)}",
                request=request
            )
        except User.DoesNotExist:
            # Create anonymous audit log for non-existent users
            AuditLog.objects.create(
                action='login_failed',
                description=f"Failed login attempt for non-existent email: {email}",
                ip_address=get_client_ip(request),
                user_agent=get_user_agent(request)
            )


@receiver(pre_save, sender=User)
def track_password_changes(sender, instance, **kwargs):
    """Track password changes and save to history."""
    if instance.pk:  # Only for existing users
        try:
            old_user = User.objects.get(pk=instance.pk)
            # Check if password has changed
            if old_user.password != instance.password:
                # Save old password to history
                PasswordHistory.objects.create(
                    user=old_user,
                    password_hash=old_user.password
                )
                
                # Update password changed timestamp
                instance.password_changed_at = timezone.now()
                
                # Reset failed login attempts
                instance.failed_login_attempts = 0
                instance.locked_until = None
        except User.DoesNotExist:
            pass


@receiver(post_save, sender=User)
def cleanup_old_password_history(sender, instance, **kwargs):
    """Keep only the last 10 passwords in history."""
    if hasattr(instance, '_password_changed'):
        # Keep only the last 10 passwords
        old_passwords = PasswordHistory.objects.filter(user=instance).order_by('-created_at')[10:]
        if old_passwords:
            PasswordHistory.objects.filter(
                user=instance,
                id__in=[p.id for p in old_passwords]
            ).delete()


@receiver(post_delete, sender=User)
def cleanup_user_data(sender, instance, **kwargs):
    """Clean up user-related data when user is deleted."""
    # This signal will automatically clean up related objects due to CASCADE,
    # but we can add custom cleanup logic here if needed
    
    create_audit_log(
        user=None,  # User is being deleted
        action='user_deleted',
        description=f"User account deleted: {instance.email}",
        metadata={'user_id': str(instance.id), 'email': instance.email}
    )