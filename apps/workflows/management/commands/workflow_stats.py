"""
Management command to display comprehensive workflow statistics.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Count, Avg, Q
from datetime import timedelta
from apps.workflows.models import Workflow, WorkflowExecution, WorkflowAction


class Command(BaseCommand):
    help = 'Display comprehensive workflow statistics'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--organizer-email',
            type=str,
            help='Show stats for specific organizer only',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Number of days to analyze (default: 30)',
        )
        parser.add_argument(
            '--detailed',
            action='store_true',
            help='Show detailed per-workflow statistics',
        )
    
    def handle(self, *args, **options):
        days = options['days']
        cutoff_date = timezone.now() - timedelta(days=days)
        
        # Build queryset
        workflows_qs = Workflow.objects.all()
        executions_qs = WorkflowExecution.objects.filter(created_at__gte=cutoff_date)
        
        if options['organizer_email']:
            workflows_qs = workflows_qs.filter(organizer__email=options['organizer_email'])
            executions_qs = executions_qs.filter(workflow__organizer__email=options['organizer_email'])
        
        workflows = workflows_qs.select_related('organizer')
        executions = executions_qs.select_related('workflow', 'booking')
        
        # Overall statistics
        total_workflows = workflows.count()
        active_workflows = workflows.filter(is_active=True).count()
        total_executions = executions.count()
        
        execution_stats = executions.aggregate(
            successful=Count('id', filter=Q(status='completed')),
            failed=Count('id', filter=Q(status='failed')),
            running=Count('id', filter=Q(status='running')),
            avg_actions_executed=Avg('actions_executed'),
            avg_actions_failed=Avg('actions_failed')
        )
        
        success_rate = (execution_stats['successful'] / total_executions * 100) if total_executions > 0 else 0
        
        # Display overall stats
        self.stdout.write(self.style.SUCCESS('📊 Workflow Statistics\n'))
        self.stdout.write(f'📅 Analysis Period: Last {days} days\n')
        
        self.stdout.write('🔧 Workflows:')
        self.stdout.write(f'   Total: {total_workflows}')
        self.stdout.write(f'   Active: {active_workflows}')
        self.stdout.write(f'   Inactive: {total_workflows - active_workflows}\n')
        
        self.stdout.write('⚡ Executions:')
        self.stdout.write(f'   Total: {total_executions}')
        self.stdout.write(f'   Successful: {execution_stats["successful"]} ({success_rate:.1f}%)')
        self.stdout.write(f'   Failed: {execution_stats["failed"]}')
        self.stdout.write(f'   Currently Running: {execution_stats["running"]}\n')
        
        if total_executions > 0:
            self.stdout.write('📈 Performance:')
            self.stdout.write(f'   Avg Actions per Execution: {execution_stats["avg_actions_executed"]:.1f}')
            self.stdout.write(f'   Avg Failed Actions: {execution_stats["avg_actions_failed"]:.1f}\n')
        
        # Trigger type breakdown
        trigger_stats = executions.values('workflow__trigger').annotate(
            count=Count('id'),
            success_count=Count('id', filter=Q(status='completed'))
        ).order_by('-count')
        
        if trigger_stats:
            self.stdout.write('🎯 Most Common Triggers:')
            for stat in trigger_stats[:5]:
                trigger = stat['workflow__trigger']
                count = stat['count']
                success_count = stat['success_count']
                trigger_success_rate = (success_count / count * 100) if count > 0 else 0
                self.stdout.write(f'   {trigger}: {count} executions ({trigger_success_rate:.1f}% success)')
            self.stdout.write('')
        
        # Top performing workflows
        if options['detailed']:
            self.stdout.write('🏆 Top Performing Workflows:')
            
            workflow_performance = []
            for workflow in workflows:
                workflow_executions = executions.filter(workflow=workflow)
                total = workflow_executions.count()
                successful = workflow_executions.filter(status='completed').count()
                
                if total > 0:
                    workflow_performance.append({
                        'workflow': workflow,
                        'total': total,
                        'successful': successful,
                        'success_rate': (successful / total * 100)
                    })
            
            # Sort by success rate, then by total executions
            workflow_performance.sort(key=lambda x: (x['success_rate'], x['total']), reverse=True)
            
            for i, perf in enumerate(workflow_performance[:10]):
                workflow = perf['workflow']
                icon = '🥇' if i == 0 else '🥈' if i == 1 else '🥉' if i == 2 else '  '
                
                self.stdout.write(
                    f'{icon} {workflow.name} ({workflow.organizer.email}): '
                    f'{perf["success_rate"]:.1f}% success ({perf["successful"]}/{perf["total"]})'
                )
            
            # Problematic workflows
            problematic = [p for p in workflow_performance if p['success_rate'] < 80 and p['total'] >= 3]
            
            if problematic:
                self.stdout.write('\n⚠️  Problematic Workflows (< 80% success rate):')
                for perf in problematic:
                    workflow = perf['workflow']
                    self.stdout.write(
                        f'   ❌ {workflow.name} ({workflow.organizer.email}): '
                        f'{perf["success_rate"]:.1f}% success ({perf["successful"]}/{perf["total"]})'
                    )
        
        # Action type breakdown
        action_stats = WorkflowAction.objects.filter(
            workflow__in=workflows,
            is_active=True
        ).values('action_type').annotate(
            count=Count('id'),
            avg_success_rate=Avg('successful_executions') * 100 / Avg('total_executions')
        ).order_by('-count')
        
        if action_stats:
            self.stdout.write('\n🎬 Action Types:')
            for stat in action_stats:
                action_type = stat['action_type']
                count = stat['count']
                avg_success = stat['avg_success_rate'] or 0
                self.stdout.write(f'   {action_type}: {count} actions (avg {avg_success:.1f}% success)')
        
        self.stdout.write(f'\n✅ Analysis complete!')