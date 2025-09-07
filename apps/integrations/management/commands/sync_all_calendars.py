"""
Management command to manually trigger calendar sync for all integrations.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.integrations.models import CalendarIntegration
from apps.integrations.tasks import sync_calendar_events


class Command(BaseCommand):
    help = 'Manually trigger calendar sync for all active integrations'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--provider',
            type=str,
            help='Sync only specific provider (google, outlook)',
        )
        parser.add_argument(
            '--organizer-email',
            type=str,
            help='Sync only specific organizer by email',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force sync even if recently synced',
        )
    
    def handle(self, *args, **options):
        # Build queryset
        queryset = CalendarIntegration.objects.filter(
            is_active=True,
            sync_enabled=True
        )
        
        if options['provider']:
            queryset = queryset.filter(provider=options['provider'])
        
        if options['organizer_email']:
            queryset = queryset.filter(organizer__email=options['organizer_email'])
        
        # Filter out recently synced unless forced
        if not options['force']:
            recent_sync_threshold = timezone.now() - timezone.timedelta(minutes=30)
            queryset = queryset.filter(
                models.Q(last_sync_at__isnull=True) | 
                models.Q(last_sync_at__lt=recent_sync_threshold)
            )
        
        integrations = list(queryset)
        
        if not integrations:
            self.stdout.write(
                self.style.WARNING('No integrations found matching criteria')
            )
            return
        
        self.stdout.write(f'Triggering sync for {len(integrations)} integrations...')
        
        # Trigger sync tasks
        for integration in integrations:
            sync_calendar_events.delay(integration.id)
            self.stdout.write(
                f'  - {integration.organizer.email} ({integration.provider})'
            )
        
        self.stdout.write(
            self.style.SUCCESS(f'Successfully triggered sync for {len(integrations)} integrations')
        )