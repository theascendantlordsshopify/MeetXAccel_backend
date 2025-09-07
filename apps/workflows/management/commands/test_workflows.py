"""
Management command to test workflows with various scenarios.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.workflows.models import Workflow
from apps.workflows.tasks import execute_workflow, bulk_execute_workflows
from apps.events.models import Booking


class Command(BaseCommand):
    help = 'Test workflows with various scenarios'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--workflow-id',
            type=str,
            help='Test specific workflow by ID',
        )
        parser.add_argument(
            '--organizer-email',
            type=str,
            help='Test workflows for specific organizer',
        )
        parser.add_argument(
            '--test-type',
            type=str,
            choices=['mock', 'real', 'live'],
            default='mock',
            help='Type of test to run',
        )
        parser.add_argument(
            '--booking-id',
            type=str,
            help='Use specific booking for real/live tests',
        )
        parser.add_argument(
            '--max-workflows',
            type=int,
            default=5,
            help='Maximum number of workflows to test',
        )
    
    def handle(self, *args, **options):
        # Build queryset
        if options['workflow_id']:
            try:
                workflows = [Workflow.objects.get(id=options['workflow_id'], is_active=True)]
            except Workflow.DoesNotExist:
                self.stdout.write(
                    self.style.ERROR(f'Workflow {options["workflow_id"]} not found')
                )
                return
        else:
            queryset = Workflow.objects.filter(is_active=True)
            
            if options['organizer_email']:
                queryset = queryset.filter(organizer__email=options['organizer_email'])
            
            workflows = list(queryset[:options['max_workflows']])
        
        if not workflows:
            self.stdout.write(
                self.style.WARNING('No workflows found matching criteria')
            )
            return
        
        test_type = options['test_type']
        booking_id = options['booking_id']
        
        self.stdout.write(f'Testing {len(workflows)} workflows with {test_type} data...\n')
        
        if test_type == 'mock':
            # Test with mock data
            workflow_ids = [str(w.id) for w in workflows]
            booking_ids = [None] * len(workflow_ids)
            
            task = bulk_execute_workflows.delay(workflow_ids, booking_ids, test_mode=True)
            
            self.stdout.write(
                self.style.SUCCESS(f'✅ Mock test initiated for {len(workflows)} workflows')
            )
            self.stdout.write(f'Task ID: {task.id}')
        
        elif test_type in ['real', 'live']:
            if not booking_id:
                self.stdout.write(
                    self.style.ERROR('--booking-id is required for real/live tests')
                )
                return
            
            try:
                booking = Booking.objects.get(id=booking_id)
            except Booking.DoesNotExist:
                self.stdout.write(
                    self.style.ERROR(f'Booking {booking_id} not found')
                )
                return
            
            # Test each workflow individually
            for workflow in workflows:
                if workflow.organizer != booking.organizer:
                    self.stdout.write(
                        self.style.WARNING(f'⚠️  Skipping {workflow.name} - different organizer')
                    )
                    continue
                
                test_mode = test_type == 'real'  # real=test_mode, live=production_mode
                
                if test_type == 'live':
                    self.stdout.write(
                        self.style.WARNING(f'⚠️  LIVE TEST: {workflow.name} will execute real actions!')
                    )
                
                task = execute_workflow.delay(
                    workflow.id, 
                    booking.id, 
                    test_mode=test_mode,
                    delay_applied=True
                )
                
                self.stdout.write(
                    self.style.SUCCESS(f'✅ {workflow.name} - Task ID: {task.id}')
                )
        
        self.stdout.write(f'\n🚀 All tests initiated. Check Celery logs for results.')