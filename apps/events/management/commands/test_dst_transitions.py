"""
Management command to test DST transition handling in availability calculations.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import datetime, date, time
from zoneinfo import ZoneInfo
from apps.events.models import EventType
from apps.events.utils import get_available_time_slots, calculate_dst_safe_time_slots
from apps.users.models import User


class Command(BaseCommand):
    help = 'Test DST transition handling in availability calculations'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--organizer-email',
            type=str,
            required=True,
            help='Email of organizer to test',
        )
        parser.add_argument(
            '--event-type-slug',
            type=str,
            required=True,
            help='Event type slug to test',
        )
        parser.add_argument(
            '--year',
            type=int,
            default=2024,
            help='Year to test DST transitions for (default: 2024)',
        )
    
    def handle(self, *args, **options):
        try:
            # Get organizer and event type
            organizer = User.objects.get(
                email=options['organizer_email'],
                is_organizer=True
            )
            
            event_type = EventType.objects.get(
                organizer=organizer,
                event_type_slug=options['event_type_slug'],
                is_active=True
            )
            
        except (User.DoesNotExist, EventType.DoesNotExist) as e:
            self.stdout.write(self.style.ERROR(f'❌ {str(e)}'))
            return
        
        year = options['year']
        organizer_timezone = organizer.profile.timezone_name
        
        self.stdout.write(self.style.SUCCESS(f'🕐 Testing DST Transitions for {year}\n'))
        self.stdout.write(f'Organizer: {organizer.email}')
        self.stdout.write(f'Event Type: {event_type.name}')
        self.stdout.write(f'Organizer Timezone: {organizer_timezone}\n')
        
        # DST transition dates for common timezones
        dst_transitions = {
            'America/New_York': [
                date(year, 3, 10),  # Spring forward (second Sunday in March)
                date(year, 11, 3),  # Fall back (first Sunday in November)
            ],
            'Europe/London': [
                date(year, 3, 31),  # Spring forward (last Sunday in March)
                date(year, 10, 27), # Fall back (last Sunday in October)
            ],
            'America/Los_Angeles': [
                date(year, 3, 10),  # Spring forward
                date(year, 11, 3),  # Fall back
            ]
        }
        
        # Test transitions for organizer's timezone
        test_dates = dst_transitions.get(organizer_timezone, [])
        
        if not test_dates:
            self.stdout.write(self.style.WARNING(f'⚠️  No DST transition dates defined for {organizer_timezone}'))
            # Test with common dates anyway
            test_dates = [date(year, 3, 10), date(year, 11, 3)]
        
        for test_date in test_dates:
            self.stdout.write(f'🔍 Testing DST transition around {test_date}:')
            
            # Test the day before, day of, and day after transition
            for offset in [-1, 0, 1]:
                test_day = test_date + timezone.timedelta(days=offset)
                
                # Skip if date is in the past
                if test_day < timezone.now().date():
                    continue
                
                self.stdout.write(f'   📅 {test_day}:')
                
                try:
                    # Calculate availability
                    result = get_available_time_slots(
                        organizer=organizer,
                        event_type=event_type,
                        start_date=test_day,
                        end_date=test_day,
                        invitee_timezone=organizer_timezone
                    )
                    
                    slots = result.get('slots', [])
                    
                    if slots:
                        self.stdout.write(f'      ✅ {len(slots)} slots found')
                        
                        # Check first and last slot for DST info
                        first_slot = slots[0]
                        last_slot = slots[-1]
                        
                        if 'dst_info' in first_slot:
                            dst_info = first_slot['dst_info']
                            self.stdout.write(f'      🕐 First slot DST: {dst_info["organizer_dst"]}')
                            
                            if dst_info['dst_transition']:
                                self.stdout.write(self.style.WARNING('      ⚠️  DST transition detected in slot!'))
                        
                        # Show time range
                        start_time = first_slot['local_start_time'].strftime('%H:%M')
                        end_time = last_slot['local_end_time'].strftime('%H:%M')
                        self.stdout.write(f'      🕐 Time range: {start_time} - {end_time}')
                        
                    else:
                        self.stdout.write('      ❌ No slots available')
                        
                        # Check if this is due to DST transition
                        org_tz = ZoneInfo(organizer_timezone)
                        test_datetime = datetime.combine(test_day, time(12, 0)).replace(tzinfo=org_tz)
                        
                        try:
                            dst_offset = test_datetime.dst()
                            self.stdout.write(f'      🕐 DST offset: {dst_offset}')
                        except Exception:
                            pass
                    
                    # Performance metrics
                    metrics = result.get('performance_metrics', {})
                    if 'computation_time_ms' in metrics:
                        comp_time = metrics['computation_time_ms']
                        if comp_time > 100:  # More than 100ms
                            self.stdout.write(self.style.WARNING(f'      ⏱️  Slow computation: {comp_time:.1f}ms'))
                        else:
                            self.stdout.write(f'      ⏱️  Computation time: {comp_time:.1f}ms')
                
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'      ❌ Error: {str(e)}'))
            
            self.stdout.write('')  # Empty line between transitions
        
        # Test cross-timezone DST handling
        self.stdout.write('🌍 Testing Cross-Timezone DST Handling:')
        
        test_timezones = ['UTC', 'America/New_York', 'Europe/London', 'Asia/Tokyo']
        
        for invitee_tz in test_timezones:
            if invitee_tz == organizer_timezone:
                continue
            
            self.stdout.write(f'   🌐 Invitee timezone: {invitee_tz}')
            
            try:
                # Test with a DST transition date
                test_date = test_dates[0] if test_dates else date(year, 3, 10)
                
                result = get_available_time_slots(
                    organizer=organizer,
                    event_type=event_type,
                    start_date=test_date,
                    end_date=test_date,
                    invitee_timezone=invitee_tz
                )
                
                slots = result.get('slots', [])
                
                if slots:
                    # Check timezone conversion accuracy
                    first_slot = slots[0]
                    
                    org_time = first_slot['start_time'].astimezone(ZoneInfo(organizer_timezone))
                    invitee_time = first_slot['local_start_time']
                    
                    self.stdout.write(f'      🕐 Organizer time: {org_time.strftime("%H:%M %Z")}')
                    self.stdout.write(f'      🕐 Invitee time: {invitee_time.strftime("%H:%M %Z")}')
                    
                    # Calculate expected offset
                    expected_offset = (invitee_time.utcoffset() - org_time.utcoffset()).total_seconds() / 3600
                    self.stdout.write(f'      📊 Timezone offset: {expected_offset:.1f} hours')
                    
                else:
                    self.stdout.write('      ❌ No slots available')
            
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'      ❌ Error: {str(e)}'))
        
        self.stdout.write(f'\n✅ DST transition testing completed!')