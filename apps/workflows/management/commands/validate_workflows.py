"""
Management command to validate all workflow configurations.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.workflows.models import Workflow
from apps.workflows.utils import validate_workflow_configuration


class Command(BaseCommand):
    help = 'Validate all workflow configurations and report issues'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--organizer-email',
            type=str,
            help='Validate workflows for specific organizer only',
        )
        parser.add_argument(
            '--fix-issues',
            action='store_true',
            help='Attempt to fix common issues automatically',
        )
        parser.add_argument(
            '--inactive-too',
            action='store_true',
            help='Include inactive workflows in validation',
        )
    
    def handle(self, *args, **options):
        # Build queryset
        queryset = Workflow.objects.all()
        
        if not options['inactive_too']:
            queryset = queryset.filter(is_active=True)
        
        if options['organizer_email']:
            queryset = queryset.filter(organizer__email=options['organizer_email'])
        
        workflows = list(queryset.select_related('organizer').prefetch_related('actions'))
        
        if not workflows:
            self.stdout.write(
                self.style.WARNING('No workflows found matching criteria')
            )
            return
        
        self.stdout.write(f'Validating {len(workflows)} workflows...\n')
        
        total_issues = 0
        fixed_issues = 0
        
        for workflow in workflows:
            try:
                validation = validate_workflow_configuration(workflow)
                
                if not validation['valid'] or validation['warnings']:
                    total_issues += 1
                    
                    # Display workflow info
                    self.stdout.write(
                        self.style.ERROR(f'❌ {workflow.name} ({workflow.organizer.email})')
                    )
                    
                    # Display errors
                    for error in validation['errors']:
                        self.stdout.write(f'   ERROR: {error}')
                    
                    # Display warnings
                    for warning in validation['warnings']:
                        self.stdout.write(f'   WARNING: {warning}')
                    
                    # Attempt fixes if requested
                    if options['fix_issues']:
                        fixes_applied = self._attempt_fixes(workflow, validation)
                        if fixes_applied:
                            fixed_issues += fixes_applied
                            self.stdout.write(
                                self.style.SUCCESS(f'   ✅ Applied {fixes_applied} fixes')
                            )
                    
                    self.stdout.write('')  # Empty line
                else:
                    self.stdout.write(
                        self.style.SUCCESS(f'✅ {workflow.name} ({workflow.organizer.email})')
                    )
            
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'❌ Error validating {workflow.name}: {str(e)}')
                )
                total_issues += 1
        
        # Summary
        healthy_workflows = len(workflows) - total_issues
        self.stdout.write(f'\n📊 Validation Summary:')
        self.stdout.write(f'   Total Workflows: {len(workflows)}')
        self.stdout.write(f'   Healthy: {healthy_workflows}')
        self.stdout.write(f'   With Issues: {total_issues}')
        
        if options['fix_issues']:
            self.stdout.write(f'   Issues Fixed: {fixed_issues}')
    
    def _attempt_fixes(self, workflow, validation):
        """Attempt to fix common workflow issues."""
        fixes_applied = 0
        
        # Fix duplicate action orders
        if any('duplicate order' in warning for warning in validation['warnings']):
            actions = workflow.actions.filter(is_active=True).order_by('created_at')
            for i, action in enumerate(actions):
                action.order = i + 1
                action.save(update_fields=['order'])
            fixes_applied += 1
        
        # Deactivate actions with missing required fields
        for error in validation['errors']:
            if 'requires' in error and 'custom_email' in error:
                # Find and deactivate actions with missing custom_email
                problematic_actions = workflow.actions.filter(
                    recipient='custom',
                    custom_email='',
                    is_active=True
                )
                count = problematic_actions.update(is_active=False)
                if count > 0:
                    fixes_applied += count
        
        return fixes_applied