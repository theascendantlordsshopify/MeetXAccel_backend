"""
Management command to check health of all integrations.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.integrations.models import CalendarIntegration, VideoConferenceIntegration
from apps.integrations.utils import create_integration_health_report
from apps.users.models import User


class Command(BaseCommand):
    help = 'Check health status of all integrations'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--organizer-email',
            type=str,
            help='Check specific organizer by email',
        )
        parser.add_argument(
            '--unhealthy-only',
            action='store_true',
            help='Show only unhealthy integrations',
        )
        parser.add_argument(
            '--fix-tokens',
            action='store_true',
            help='Attempt to refresh expired tokens',
        )
    
    def handle(self, *args, **options):
        # Build organizer queryset
        if options['organizer_email']:
            try:
                organizers = [User.objects.get(email=options['organizer_email'], is_organizer=True)]
            except User.DoesNotExist:
                self.stdout.write(
                    self.style.ERROR(f'Organizer not found: {options["organizer_email"]}')
                )
                return
        else:
            organizers = User.objects.filter(is_organizer=True, is_active=True)
        
        total_organizers = len(organizers)
        healthy_count = 0
        unhealthy_count = 0
        
        self.stdout.write(f'Checking health for {total_organizers} organizers...\n')
        
        for organizer in organizers:
            health_report = create_integration_health_report(organizer)
            
            # Determine if organizer has any unhealthy integrations
            has_unhealthy = health_report['overall_health'] != 'healthy'
            
            if options['unhealthy_only'] and not has_unhealthy:
                continue
            
            if has_unhealthy:
                unhealthy_count += 1
                status_style = self.style.ERROR
            else:
                healthy_count += 1
                status_style = self.style.SUCCESS
            
            self.stdout.write(
                status_style(f'📧 {organizer.email} - {health_report["overall_health"].upper()}')
            )
            
            # Show calendar integrations
            for cal_integration in health_report['calendar_integrations']:
                health_icon = '✅' if cal_integration['health'] == 'healthy' else '❌'
                self.stdout.write(
                    f'  {health_icon} Calendar ({cal_integration["provider"]}): '
                    f'Active={cal_integration["is_active"]}, '
                    f'Token Expired={cal_integration["token_expired"]}, '
                    f'Sync Errors={cal_integration["sync_errors"]}'
                )
                
                if options['fix_tokens'] and cal_integration['token_expired']:
                    self._attempt_token_refresh(organizer, cal_integration['provider'], 'calendar')
            
            # Show video integrations
            for video_integration in health_report['video_integrations']:
                health_icon = '✅' if video_integration['health'] == 'healthy' else '❌'
                self.stdout.write(
                    f'  {health_icon} Video ({video_integration["provider"]}): '
                    f'Active={video_integration["is_active"]}, '
                    f'Token Expired={video_integration["token_expired"]}, '
                    f'API Calls Today={video_integration["api_calls_today"]}'
                )
                
                if options['fix_tokens'] and video_integration['token_expired']:
                    self._attempt_token_refresh(organizer, video_integration['provider'], 'video')
            
            self.stdout.write('')  # Empty line for readability
        
        # Summary
        self.stdout.write(
            self.style.SUCCESS(f'\n📊 Summary: {healthy_count} healthy, {unhealthy_count} unhealthy')
        )
    
    def _attempt_token_refresh(self, organizer, provider, integration_type):
        """Attempt to refresh an expired token."""
        try:
            from apps.integrations.utils import refresh_access_token
            
            if integration_type == 'calendar':
                integration = CalendarIntegration.objects.get(
                    organizer=organizer,
                    provider=provider
                )
            else:
                integration = VideoConferenceIntegration.objects.get(
                    organizer=organizer,
                    provider=provider
                )
            
            if refresh_access_token(integration):
                self.stdout.write(
                    self.style.SUCCESS(f'    🔄 Successfully refreshed {provider} {integration_type} token')
                )
            else:
                self.stdout.write(
                    self.style.ERROR(f'    ❌ Failed to refresh {provider} {integration_type} token')
                )
                
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'    ❌ Error refreshing {provider} {integration_type} token: {str(e)}')
            )