from django.contrib import admin
from .models import AvailabilityRule, BlockedTime, BufferTime, DateOverrideRule, RecurringBlockedTime


@admin.register(AvailabilityRule)
class AvailabilityRuleAdmin(admin.ModelAdmin):
    list_display = ('organizer', 'day_of_week', 'start_time', 'end_time', 'spans_midnight', 'event_types_count', 'is_active')
    list_filter = ('day_of_week', 'is_active', 'created_at')
    search_fields = ('organizer__email', 'organizer__first_name', 'organizer__last_name')
    readonly_fields = ('created_at', 'updated_at')
    filter_horizontal = ('event_types',)
    
    fieldsets = (
        ('Organizer', {
            'fields': ('organizer',)
        }),
        ('Schedule', {
            'fields': ('day_of_week', 'start_time', 'end_time', 'is_active')
        }),
        ('Event Type Specificity', {
            'fields': ('event_types',),
            'description': 'Leave empty to apply to all event types'
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def spans_midnight(self, obj):
        return obj.spans_midnight()
    spans_midnight.boolean = True
    spans_midnight.short_description = 'Spans Midnight'
    
    def event_types_count(self, obj):
        count = obj.event_types.count()
        return f"{count} types" if count > 0 else "All types"
    event_types_count.short_description = 'Event Types'


@admin.register(DateOverrideRule)
class DateOverrideRuleAdmin(admin.ModelAdmin):
    list_display = ('organizer', 'date', 'is_available', 'start_time', 'end_time', 'spans_midnight', 'event_types_count', 'is_active')
    list_filter = ('is_available', 'is_active', 'date', 'created_at')
    search_fields = ('organizer__email', 'reason')
    readonly_fields = ('created_at', 'updated_at')
    filter_horizontal = ('event_types',)
    date_hierarchy = 'date'
    
    fieldsets = (
        ('Organizer', {
            'fields': ('organizer',)
        }),
        ('Date Override', {
            'fields': ('date', 'is_available', 'start_time', 'end_time', 'reason', 'is_active')
        }),
        ('Event Type Specificity', {
            'fields': ('event_types',),
            'description': 'Leave empty to apply to all event types'
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def spans_midnight(self, obj):
        return obj.spans_midnight()
    spans_midnight.boolean = True
    spans_midnight.short_description = 'Spans Midnight'
    
    def event_types_count(self, obj):
        count = obj.event_types.count()
        return f"{count} types" if count > 0 else "All types"
    event_types_count.short_description = 'Event Types'


@admin.register(RecurringBlockedTime)
class RecurringBlockedTimeAdmin(admin.ModelAdmin):
    list_display = ('organizer', 'name', 'day_of_week', 'start_time', 'end_time', 'spans_midnight', 'date_range', 'is_active')
    list_filter = ('day_of_week', 'is_active', 'created_at')
    search_fields = ('organizer__email', 'name')
    readonly_fields = ('created_at', 'updated_at')
    
    fieldsets = (
        ('Organizer', {
            'fields': ('organizer',)
        }),
        ('Recurring Block Details', {
            'fields': ('name', 'day_of_week', 'start_time', 'end_time', 'is_active')
        }),
        ('Date Range (Optional)', {
            'fields': ('start_date', 'end_date'),
            'description': 'Leave empty for indefinite recurring block'
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def spans_midnight(self, obj):
        return obj.spans_midnight()
    spans_midnight.boolean = True
    spans_midnight.short_description = 'Spans Midnight'
    
    def date_range(self, obj):
        if obj.start_date and obj.end_date:
            return f"{obj.start_date} to {obj.end_date}"
        elif obj.start_date:
            return f"From {obj.start_date}"
        elif obj.end_date:
            return f"Until {obj.end_date}"
        return "Indefinite"
    date_range.short_description = 'Date Range'


@admin.register(BlockedTime)
class BlockedTimeAdmin(admin.ModelAdmin):
    list_display = ('organizer', 'start_datetime', 'end_datetime', 'reason', 'source', 'is_active')
    list_filter = ('source', 'is_active', 'start_datetime', 'created_at')
    search_fields = ('organizer__email', 'reason')
    readonly_fields = ('created_at', 'updated_at', 'external_id', 'external_updated_at')
    date_hierarchy = 'start_datetime'
    
    fieldsets = (
        ('Organizer', {
            'fields': ('organizer',)
        }),
        ('Blocked Period', {
            'fields': ('start_datetime', 'end_datetime', 'reason', 'is_active')
        }),
        ('Source Information', {
            'fields': ('source', 'external_id', 'external_updated_at'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(BufferTime)
class BufferTimeAdmin(admin.ModelAdmin):
    list_display = ('organizer', 'default_buffer_before', 'default_buffer_after', 'minimum_gap', 'slot_interval_minutes')
    search_fields = ('organizer__email', 'organizer__first_name', 'organizer__last_name')
    readonly_fields = ('created_at', 'updated_at')
    
    fieldsets = (
        ('Organizer', {
            'fields': ('organizer',)
        }),
        ('Buffer Settings', {
            'fields': ('default_buffer_before', 'default_buffer_after', 'minimum_gap', 'slot_interval_minutes')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )