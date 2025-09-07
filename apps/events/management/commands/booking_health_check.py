"""
Management command to check booking system health and performance.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Count, Avg, Q
from datetime import timedelta
from apps.events.models import Booking, EventType, WaitlistEntry, EventTypeAvailabilityCache


class Command(BaseCommand):
    help = 'Check booking system health and performance metrics'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days to analyze (default: 7)',
        )
        parser.add_argument(
            '--organizer-email',
            type=str,
            help='Check specific organizer by email',
        )
        parser.add_argument(
            '--detailed',
            action='store_true',
            help='Show detailed per-organizer statistics',
        )
    
    def handle(self, *args, **options):
        days = options['days']
        cutoff_date = timezone.now() - timedelta(days=days)
        
        # Build querysets
        bookings_qs = Booking.objects.filter(created_at__gte=cutoff_date)
        event_types_qs = EventType.objects.all()
        
        if options['organizer_email']:
            bookings_qs = bookings_qs.filter(organizer__email=options['organizer_email'])
            event_types_qs = event_types_qs.filter(organizer__email=options['organizer_email'])
        
        # Overall statistics
        total_bookings = bookings_qs.count()
        confirmed_bookings = bookings_qs.filter(status='confirmed').count()
        cancelled_bookings = bookings_qs.filter(status='cancelled').count()
        completed_bookings = bookings_qs.filter(status='completed').count()
        
        # Calendar sync health
        sync_stats = bookings_qs.aggregate(
            sync_success=Count('id', filter=Q(calendar_sync_status='succeeded')),
            sync_failed=Count('id', filter=Q(calendar_sync_status='failed')),
            sync_pending=Count('id', filter=Q(calendar_sync_status='pending'))
        )
        
        # Cache performance
        cache_stats = EventTypeAvailabilityCache.objects.aggregate(
            total_entries=Count('id'),
            dirty_entries=Count('id', filter=Q(is_dirty=True)),
            expired_entries=Count('id', filter=Q(expires_at__lt=timezone.now())),
            avg_computation_time=Avg('computation_time_ms')
        )
        
        # Waitlist statistics
        waitlist_stats = WaitlistEntry.objects.aggregate(
            active_entries=Count('id', filter=Q(status='active')),
            converted_entries=Count('id', filter=Q(status='converted')),
            expired_entries=Count('id', filter=Q(status='expired'))
        )
        
        # Display results
        self.stdout.write(self.style.SUCCESS('📊 Booking System Health Report\n'))
        self.stdout.write(f'📅 Analysis Period: Last {days} days\n')
        
        # Booking statistics
        self.stdout.write('📋 Booking Statistics:')
        self.stdout.write(f'   Total Bookings: {total_bookings}')
        self.stdout.write(f'   Confirmed: {confirmed_bookings} ({self._percentage(confirmed_bookings, total_bookings)}%)')
        self.stdout.write(f'   Cancelled: {cancelled_bookings} ({self._percentage(cancelled_bookings, total_bookings)}%)')
        self.stdout.write(f'   Completed: {completed_bookings} ({self._percentage(completed_bookings, total_bookings)}%)')
        self.stdout.write('')
        
        # Calendar sync health
        total_sync_attempts = sum(sync_stats.values())
        if total_sync_attempts > 0:
            self.stdout.write('📅 Calendar Sync Health:')
            self.stdout.write(f'   Successful: {sync_stats["sync_success"]} ({self._percentage(sync_stats["sync_success"], total_sync_attempts)}%)')
            self.stdout.write(f'   Failed: {sync_stats["sync_failed"]} ({self._percentage(sync_stats["sync_failed"], total_sync_attempts)}%)')
            self.stdout.write(f'   Pending: {sync_stats["sync_pending"]} ({self._percentage(sync_stats["sync_pending"], total_sync_attempts)}%)')
            
            if sync_stats["sync_failed"] > total_sync_attempts * 0.1:  # More than 10% failure rate
                self.stdout.write(self.style.WARNING('   ⚠️  High calendar sync failure rate detected!'))
            self.stdout.write('')
        
        # Cache performance
        cache_hit_rate = 0
        if cache_stats['total_entries'] > 0:
            cache_hit_rate = ((cache_stats['total_entries'] - cache_stats['dirty_entries']) / 
                            cache_stats['total_entries'] * 100)
        
        self.stdout.write('🚀 Cache Performance:')
        self.stdout.write(f'   Total Cache Entries: {cache_stats["total_entries"]}')
        self.stdout.write(f'   Dirty Entries: {cache_stats["dirty_entries"]}')
        self.stdout.write(f'   Expired Entries: {cache_stats["expired_entries"]}')
        self.stdout.write(f'   Cache Hit Rate: {cache_hit_rate:.1f}%')
        
        if cache_stats['avg_computation_time']:
            self.stdout.write(f'   Avg Computation Time: {cache_stats["avg_computation_time"]:.1f}ms')
        
        if cache_hit_rate < 70:
            self.stdout.write(self.style.WARNING('   ⚠️  Low cache hit rate detected!'))
        self.stdout.write('')
        
        # Waitlist statistics
        total_waitlist = sum(waitlist_stats.values())
        if total_waitlist > 0:
            self.stdout.write('📝 Waitlist Statistics:')
            self.stdout.write(f'   Active Entries: {waitlist_stats["active_entries"]}')
            self.stdout.write(f'   Converted to Bookings: {waitlist_stats["converted_entries"]}')
            self.stdout.write(f'   Expired: {waitlist_stats["expired_entries"]}')
            
            conversion_rate = self._percentage(waitlist_stats["converted_entries"], total_waitlist)
            self.stdout.write(f'   Conversion Rate: {conversion_rate}%')
            self.stdout.write('')
        
        # Event type analysis
        if options['detailed']:
            self.stdout.write('📊 Event Type Performance:')
            
            event_type_stats = event_types_qs.annotate(
                booking_count=Count('bookings', filter=Q(bookings__created_at__gte=cutoff_date)),
                confirmed_count=Count('bookings', filter=Q(
                    bookings__created_at__gte=cutoff_date,
                    bookings__status='confirmed'
                )),
                cancelled_count=Count('bookings', filter=Q(
                    bookings__created_at__gte=cutoff_date,
                    bookings__status='cancelled'
                ))
            ).filter(booking_count__gt=0).order_by('-booking_count')
            
            for event_type in event_type_stats[:10]:
                cancellation_rate = self._percentage(event_type.cancelled_count, event_type.booking_count)
                self.stdout.write(
                    f'   {event_type.name}: {event_type.booking_count} bookings, '
                    f'{cancellation_rate}% cancelled'
                )
        
        # Health assessment
        self.stdout.write('🏥 Overall Health Assessment:')
        
        health_issues = []
        
        if total_bookings > 0:
            cancellation_rate = (cancelled_bookings / total_bookings) * 100
            if cancellation_rate > 20:
                health_issues.append(f'High cancellation rate: {cancellation_rate:.1f}%')
        
        if total_sync_attempts > 0:
            sync_failure_rate = (sync_stats["sync_failed"] / total_sync_attempts) * 100
            if sync_failure_rate > 10:
                health_issues.append(f'High calendar sync failure rate: {sync_failure_rate:.1f}%')
        
        if cache_hit_rate < 70:
            health_issues.append(f'Low cache hit rate: {cache_hit_rate:.1f}%')
        
        if cache_stats['dirty_entries'] > 100:
            health_issues.append(f'High number of dirty cache entries: {cache_stats["dirty_entries"]}')
        
        if health_issues:
            self.stdout.write(self.style.WARNING('   ⚠️  Issues detected:'))
            for issue in health_issues:
                self.stdout.write(f'      - {issue}')
        else:
            self.stdout.write(self.style.SUCCESS('   ✅ System appears healthy'))
        
        self.stdout.write(f'\n✅ Health check completed!')
    
    def _percentage(self, part, total):
        """Calculate percentage safely."""
        if total == 0:
            return 0
        return round((part / total) * 100, 1)