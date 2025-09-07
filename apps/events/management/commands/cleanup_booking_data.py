"""
Management command to clean up old booking data and maintain system performance.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from apps.events.models import (
    Booking, WaitlistEntry, BookingAuditLog, EventTypeAvailabilityCache
)


class Command(BaseCommand):
    help = 'Clean up old booking data to maintain system performance'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be cleaned up without actually deleting',
        )
        parser.add_argument(
            '--audit-logs-days',
            type=int,
            default=365,
            help='Keep audit logs for this many days (default: 365)',
        )
        parser.add_argument(
            '--completed-bookings-days',
            type=int,
            default=90,
            help='Keep completed bookings for this many days (default: 90)',
        )
        parser.add_argument(
            '--expired-waitlist-days',
            type=int,
            default=30,
            help='Keep expired waitlist entries for this many days (default: 30)',
        )
    
    def handle(self, *args, **options):
        dry_run = options['dry_run']
        
        if dry_run:
            self.stdout.write(self.style.WARNING('🔍 DRY RUN MODE - No data will be deleted\n'))
        
        total_cleaned = 0
        
        # Clean up old audit logs
        audit_cutoff = timezone.now() - timedelta(days=options['audit_logs_days'])
        old_audit_logs = BookingAuditLog.objects.filter(created_at__lt=audit_cutoff)
        audit_count = old_audit_logs.count()
        
        self.stdout.write(f'📋 Audit Logs older than {options["audit_logs_days"]} days: {audit_count}')
        
        if not dry_run and audit_count > 0:
            old_audit_logs.delete()
            total_cleaned += audit_count
            self.stdout.write(self.style.SUCCESS(f'   ✅ Deleted {audit_count} old audit logs'))
        
        # Clean up old completed bookings
        booking_cutoff = timezone.now() - timedelta(days=options['completed_bookings_days'])
        old_completed_bookings = Booking.objects.filter(
            status='completed',
            end_time__lt=booking_cutoff
        )
        completed_count = old_completed_bookings.count()
        
        self.stdout.write(f'📅 Completed bookings older than {options["completed_bookings_days"]} days: {completed_count}')
        
        if not dry_run and completed_count > 0:
            old_completed_bookings.delete()
            total_cleaned += completed_count
            self.stdout.write(self.style.SUCCESS(f'   ✅ Deleted {completed_count} old completed bookings'))
        
        # Clean up expired waitlist entries
        waitlist_cutoff = timezone.now() - timedelta(days=options['expired_waitlist_days'])
        old_waitlist_entries = WaitlistEntry.objects.filter(
            status__in=['expired', 'cancelled'],
            updated_at__lt=waitlist_cutoff
        )
        waitlist_count = old_waitlist_entries.count()
        
        self.stdout.write(f'📝 Expired waitlist entries older than {options["expired_waitlist_days"]} days: {waitlist_count}')
        
        if not dry_run and waitlist_count > 0:
            old_waitlist_entries.delete()
            total_cleaned += waitlist_count
            self.stdout.write(self.style.SUCCESS(f'   ✅ Deleted {waitlist_count} old waitlist entries'))
        
        # Clean up expired cache entries
        expired_cache = EventTypeAvailabilityCache.objects.filter(
            expires_at__lt=timezone.now()
        )
        cache_count = expired_cache.count()
        
        self.stdout.write(f'🚀 Expired cache entries: {cache_count}')
        
        if not dry_run and cache_count > 0:
            expired_cache.delete()
            total_cleaned += cache_count
            self.stdout.write(self.style.SUCCESS(f'   ✅ Deleted {cache_count} expired cache entries'))
        
        # Clean up very old cancelled bookings (keep for audit purposes but limit retention)
        very_old_cancelled = Booking.objects.filter(
            status='cancelled',
            cancelled_at__lt=timezone.now() - timedelta(days=180)  # 6 months
        )
        very_old_count = very_old_cancelled.count()
        
        self.stdout.write(f'❌ Very old cancelled bookings (>6 months): {very_old_count}')
        
        if not dry_run and very_old_count > 0:
            very_old_cancelled.delete()
            total_cleaned += very_old_count
            self.stdout.write(self.style.SUCCESS(f'   ✅ Deleted {very_old_count} very old cancelled bookings'))
        
        # Summary
        if dry_run:
            self.stdout.write(f'\n📊 Would clean up {total_cleaned} total records')
            self.stdout.write('Run without --dry-run to actually perform cleanup')
        else:
            self.stdout.write(f'\n✅ Cleanup completed: {total_cleaned} records removed')
        
        # Performance recommendations
        self.stdout.write('\n💡 Performance Recommendations:')
        
        # Check for high cache miss rate
        total_cache = EventTypeAvailabilityCache.objects.count()
        dirty_cache = EventTypeAvailabilityCache.objects.filter(is_dirty=True).count()
        
        if total_cache > 0:
            cache_hit_rate = ((total_cache - dirty_cache) / total_cache) * 100
            if cache_hit_rate < 70:
                self.stdout.write('   - Consider increasing cache timeout or optimizing cache invalidation')
        
        # Check for high cancellation rates
        if total_bookings > 0:
            cancellation_rate = (cancelled_bookings / total_bookings) * 100
            if cancellation_rate > 15:
                self.stdout.write('   - High cancellation rate detected - consider reviewing booking policies')
        
        # Check for calendar sync issues
        if sync_stats['sync_failed'] > 0:
            failure_rate = (sync_stats['sync_failed'] / total_bookings) * 100
            if failure_rate > 5:
                self.stdout.write('   - Calendar sync issues detected - check integration health')
        
        self.stdout.write('\n🏁 Health check completed!')