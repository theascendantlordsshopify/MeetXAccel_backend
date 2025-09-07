"""
Comprehensive tests for workflows module with enterprise-grade coverage.
"""
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from unittest.mock import patch, Mock, MagicMock
from datetime import datetime, timedelta
from .models import Workflow, WorkflowAction, WorkflowExecution, WorkflowTemplate
from .utils import (
    build_context_data_from_booking, evaluate_conditions, validate_update_booking_fields,
    get_workflow_execution_summary, create_test_context_data
)
from .tasks import execute_workflow, trigger_workflows, execute_workflow_action
from apps.events.models import EventType, Booking
from apps.users.models import User, Profile

User = get_user_model()


class WorkflowUtilsTestCase(TestCase):
    """Test workflow utility functions."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True,
            is_email_verified=True,
            account_status='active'
        )
        
        Profile.objects.create(
            user=self.organizer,
            timezone_name='America/New_York',
            company='Test Company'
        )
        
        self.event_type = EventType.objects.create(
            organizer=self.organizer,
            name='Discovery Call',
            duration=30
        )
        
        self.booking = Booking.objects.create(
            event_type=self.event_type,
            organizer=self.organizer,
            invitee_name='John Doe',
            invitee_email='john.doe@example.com',
            invitee_phone='+1234567890',
            start_time=timezone.now() + timedelta(hours=2),
            end_time=timezone.now() + timedelta(hours=2, minutes=30),
            status='confirmed',
            custom_answers={'company': 'Acme Corp', 'budget': '10000'}
        )
    
    def test_build_context_data_from_booking(self):
        """Test context data building from booking."""
        context = build_context_data_from_booking(self.booking)
        
        # Test basic fields
        self.assertEqual(context['invitee_name'], 'John Doe')
        self.assertEqual(context['invitee_email'], 'john.doe@example.com')
        self.assertEqual(context['event_type_name'], 'Discovery Call')
        self.assertEqual(context['organizer_email'], 'organizer@test.com')
        self.assertEqual(context['duration'], 30)
        
        # Test derived fields
        self.assertEqual(context['invitee_domain'], 'example.com')
        self.assertTrue(context['has_phone'])
        self.assertFalse(context['is_weekend'])  # Depends on when test runs
        
        # Test custom answers flattening
        self.assertEqual(context['custom_company'], 'Acme Corp')
        self.assertEqual(context['custom_budget'], '10000')
    
    def test_evaluate_conditions_simple(self):
        """Test simple condition evaluation."""
        context = build_context_data_from_booking(self.booking)
        
        # Test equals condition
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "event_type_name", "operator": "equals", "value": "Discovery Call"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
        
        # Test not equals
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "event_type_name", "operator": "not_equals", "value": "Other Meeting"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
        
        # Test contains
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "invitee_email", "operator": "contains", "value": "@example.com"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
    
    def test_evaluate_conditions_complex(self):
        """Test complex condition evaluation with multiple groups."""
        context = build_context_data_from_booking(self.booking)
        
        # Test AND group
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "event_type_name", "operator": "equals", "value": "Discovery Call"},
                {"field": "invitee_domain", "operator": "equals", "value": "example.com"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
        
        # Test OR group
        conditions = [{
            "operator": "OR",
            "rules": [
                {"field": "event_type_name", "operator": "equals", "value": "Wrong Name"},
                {"field": "invitee_domain", "operator": "equals", "value": "example.com"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
        
        # Test multiple groups (implicit AND between groups)
        conditions = [
            {
                "operator": "AND",
                "rules": [
                    {"field": "event_type_name", "operator": "equals", "value": "Discovery Call"}
                ]
            },
            {
                "operator": "OR",
                "rules": [
                    {"field": "custom_company", "operator": "equals", "value": "Acme Corp"},
                    {"field": "custom_budget", "operator": "greater_than", "value": "5000"}
                ]
            }
        ]
        
        self.assertTrue(evaluate_conditions(conditions, context))
    
    def test_evaluate_conditions_numeric(self):
        """Test numeric condition operators."""
        context = build_context_data_from_booking(self.booking)
        
        # Test greater_than
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "duration", "operator": "greater_than", "value": "20"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
        
        # Test less_than
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "duration", "operator": "less_than", "value": "60"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
        
        # Test with custom numeric field
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "custom_budget", "operator": "greater_than", "value": "5000"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
    
    def test_evaluate_conditions_string_operations(self):
        """Test string operation conditions."""
        context = build_context_data_from_booking(self.booking)
        
        # Test starts_with
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "invitee_email", "operator": "starts_with", "value": "john"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
        
        # Test ends_with
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "invitee_email", "operator": "ends_with", "value": ".com"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
        
        # Test is_empty
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "cancellation_reason", "operator": "is_empty"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
        
        # Test is_not_empty
        conditions = [{
            "operator": "AND",
            "rules": [
                {"field": "invitee_name", "operator": "is_not_empty"}
            ]
        }]
        
        self.assertTrue(evaluate_conditions(conditions, context))
    
    def test_validate_update_booking_fields(self):
        """Test booking field update validation."""
        # Valid updates
        valid_fields = {
            'status': 'completed',
            'cancellation_reason': 'Meeting finished',
            'meeting_link': 'https://zoom.us/j/updated123'
        }
        
        validated = validate_update_booking_fields(valid_fields, self.booking)
        self.assertEqual(validated['status'], 'completed')
        self.assertEqual(validated['cancellation_reason'], 'Meeting finished')
        
        # Invalid status
        invalid_fields = {
            'status': 'invalid_status'
        }
        
        validated = validate_update_booking_fields(invalid_fields, self.booking)
        self.assertNotIn('status', validated)  # Should be filtered out
        
        # Invalid field name
        invalid_fields = {
            'invalid_field': 'some_value'
        }
        
        validated = validate_update_booking_fields(invalid_fields, self.booking)
        self.assertEqual(len(validated), 0)  # Should be empty
    
    def test_create_test_context_data(self):
        """Test test context data creation."""
        context = create_test_context_data()
        
        # Verify required fields are present
        required_fields = [
            'booking_id', 'invitee_name', 'invitee_email', 'event_type_name',
            'organizer_name', 'duration', 'start_time', 'end_time'
        ]
        
        for field in required_fields:
            self.assertIn(field, context)
            self.assertIsNotNone(context[field])


class WorkflowExecutionTestCase(TestCase):
    """Test workflow execution functionality."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
        
        Profile.objects.create(
            user=self.organizer,
            phone='+1234567890'
        )
        
        self.event_type = EventType.objects.create(
            organizer=self.organizer,
            name='Test Meeting',
            duration=30
        )
        
        self.booking = Booking.objects.create(
            event_type=self.event_type,
            organizer=self.organizer,
            invitee_name='Test Invitee',
            invitee_email='invitee@test.com',
            invitee_phone='+1987654321',
            start_time=timezone.now() + timedelta(hours=2),
            end_time=timezone.now() + timedelta(hours=2, minutes=30),
            status='confirmed'
        )
        
        self.workflow = Workflow.objects.create(
            organizer=self.organizer,
            name='Test Workflow',
            trigger='booking_created',
            is_active=True
        )
    
    def test_workflow_execution_with_conditions(self):
        """Test workflow execution with conditional actions."""
        # Create action with conditions
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='Conditional Email',
            action_type='send_email',
            order=1,
            recipient='invitee',
            subject='Welcome {{invitee_name}}',
            message='Thank you for booking {{event_type_name}}',
            conditions=[{
                "operator": "AND",
                "rules": [
                    {"field": "event_type_name", "operator": "equals", "value": "Test Meeting"}
                ]
            }],
            is_active=True
        )
        
        # Execute workflow in test mode
        from .tasks import execute_workflow
        result = execute_workflow(self.workflow.id, self.booking.id, test_mode=True, delay_applied=True)
        
        self.assertIn('executed', result)
        
        # Check execution was logged
        execution = WorkflowExecution.objects.filter(workflow=self.workflow).first()
        self.assertIsNotNone(execution)
        self.assertEqual(execution.status, 'completed')
        self.assertEqual(execution.actions_executed, 1)
        self.assertEqual(execution.actions_failed, 0)
    
    def test_workflow_execution_skipped_conditions(self):
        """Test workflow execution with unmet conditions."""
        # Create action with conditions that won't be met
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='Conditional Email',
            action_type='send_email',
            order=1,
            recipient='invitee',
            subject='Welcome {{invitee_name}}',
            message='Thank you for booking',
            conditions=[{
                "operator": "AND",
                "rules": [
                    {"field": "event_type_name", "operator": "equals", "value": "Different Meeting"}
                ]
            }],
            is_active=True
        )
        
        # Execute workflow in test mode
        from .tasks import execute_workflow
        result = execute_workflow(self.workflow.id, self.booking.id, test_mode=True, delay_applied=True)
        
        # Check execution was logged with skipped action
        execution = WorkflowExecution.objects.filter(workflow=self.workflow).first()
        self.assertIsNotNone(execution)
        self.assertEqual(execution.status, 'completed')
        self.assertEqual(execution.actions_executed, 0)
        self.assertEqual(execution.actions_failed, 0)
        
        # Check execution log shows skipped action
        self.assertEqual(len(execution.execution_log), 1)
        self.assertEqual(execution.execution_log[0]['status'], 'skipped_conditions')
    
    @patch('apps.workflows.tasks.send_notification_task')
    def test_email_action_execution(self, mock_send_task):
        """Test email action execution."""
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='Welcome Email',
            action_type='send_email',
            order=1,
            recipient='invitee',
            subject='Welcome {{invitee_name}}',
            message='Thank you for booking {{event_type_name}}',
            is_active=True
        )
        
        context = build_context_data_from_booking(self.booking)
        
        # Execute action
        result = execute_workflow_action(action, self.booking, context, test_mode=False)
        
        # Verify notification was created and queued
        self.assertEqual(result['action_type'], 'send_email')
        self.assertEqual(result['recipients_count'], 1)
        self.assertTrue(mock_send_task.delay.called)
    
    @patch('apps.workflows.tasks.send_notification_task')
    def test_sms_action_execution(self, mock_send_task):
        """Test SMS action execution."""
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='SMS Reminder',
            action_type='send_sms',
            order=1,
            recipient='invitee',
            message='Reminder: {{event_type_name}} with {{organizer_name}}',
            is_active=True
        )
        
        context = build_context_data_from_booking(self.booking)
        
        # Execute action
        result = execute_workflow_action(action, self.booking, context, test_mode=False)
        
        # Verify SMS was queued
        self.assertEqual(result['action_type'], 'send_sms')
        self.assertEqual(result['recipients_count'], 1)
        self.assertTrue(mock_send_task.delay.called)
    
    @patch('apps.integrations.utils.make_api_request')
    def test_webhook_action_execution(self, mock_api_request):
        """Test webhook action execution."""
        # Mock successful webhook response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b'{"success": true}'
        mock_api_request.return_value = mock_response
        
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='CRM Webhook',
            action_type='webhook',
            order=1,
            webhook_url='https://api.example.com/webhook',
            webhook_data={'source': 'calendly_clone'},
            is_active=True
        )
        
        context = build_context_data_from_booking(self.booking)
        
        # Execute action
        result = execute_workflow_action(action, self.booking, context, test_mode=False)
        
        # Verify webhook was called
        self.assertEqual(result['action_type'], 'webhook')
        self.assertEqual(result['status_code'], 200)
        self.assertTrue(mock_api_request.called)
        
        # Verify payload structure
        call_args = mock_api_request.call_args
        payload = call_args[1]['json_data']
        self.assertIn('booking_id', payload)
        self.assertIn('source', payload)  # From webhook_data
        self.assertEqual(payload['source'], 'calendly_clone')
    
    def test_update_booking_action_execution(self):
        """Test booking update action execution."""
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='Mark Completed',
            action_type='update_booking',
            order=1,
            update_booking_fields={
                'status': 'completed',
                'cancellation_reason': 'Meeting finished successfully'
            },
            is_active=True
        )
        
        context = build_context_data_from_booking(self.booking)
        
        # Store original status
        original_status = self.booking.status
        
        # Execute action
        result = execute_workflow_action(action, self.booking, context, test_mode=False)
        
        # Verify booking was updated
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'completed')
        self.assertEqual(self.booking.cancellation_reason, 'Meeting finished successfully')
        
        # Verify result structure
        self.assertEqual(result['action_type'], 'update_booking')
        self.assertIn('updated_fields', result)
        self.assertIn('status', result['updated_fields'])
        self.assertEqual(result['original_values']['status'], original_status)
    
    def test_workflow_execution_with_delay(self):
        """Test workflow execution with delay."""
        # Create workflow with delay
        delayed_workflow = Workflow.objects.create(
            organizer=self.organizer,
            name='Delayed Workflow',
            trigger='booking_created',
            delay_minutes=5,
            is_active=True
        )
        
        action = WorkflowAction.objects.create(
            workflow=delayed_workflow,
            name='Delayed Email',
            action_type='send_email',
            order=1,
            recipient='invitee',
            subject='Follow-up',
            message='Thank you for your meeting',
            is_active=True
        )
        
        # Execute workflow (should schedule for later)
        from .tasks import execute_workflow
        result = execute_workflow(delayed_workflow.id, self.booking.id, test_mode=True, delay_applied=False)
        
        self.assertIn('scheduled with', result)
        self.assertIn('5 minute delay', result)
    
    def test_workflow_trigger_mechanism(self):
        """Test workflow triggering from booking events."""
        # Create multiple workflows with different triggers
        workflow1 = Workflow.objects.create(
            organizer=self.organizer,
            name='Creation Workflow',
            trigger='booking_created',
            is_active=True
        )
        
        workflow2 = Workflow.objects.create(
            organizer=self.organizer,
            name='Cancellation Workflow',
            trigger='booking_cancelled',
            is_active=True
        )
        
        # Add actions to workflows
        WorkflowAction.objects.create(
            workflow=workflow1,
            name='Creation Email',
            action_type='send_email',
            recipient='invitee',
            subject='Booking Created',
            message='Your booking was created',
            is_active=True
        )
        
        WorkflowAction.objects.create(
            workflow=workflow2,
            name='Cancellation Email',
            action_type='send_email',
            recipient='invitee',
            subject='Booking Cancelled',
            message='Your booking was cancelled',
            is_active=True
        )
        
        # Test booking_created trigger
        from .tasks import trigger_workflows
        result = trigger_workflows('booking_created', self.booking.id)
        
        self.assertIn('Triggered 1 workflows', result)  # Only workflow1 should trigger
        
        # Test booking_cancelled trigger
        result = trigger_workflows('booking_cancelled', self.booking.id)
        
        self.assertIn('Triggered 1 workflows', result)  # Only workflow2 should trigger


class WorkflowFailureScenarioTestCase(TestCase):
    """Test workflow failure scenarios and error handling."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
        
        self.event_type = EventType.objects.create(
            organizer=self.organizer,
            name='Test Meeting',
            duration=30
        )
        
        self.booking = Booking.objects.create(
            event_type=self.event_type,
            organizer=self.organizer,
            invitee_name='Test Invitee',
            invitee_email='invitee@test.com',
            start_time=timezone.now() + timedelta(hours=2),
            end_time=timezone.now() + timedelta(hours=2, minutes=30),
            status='confirmed'
        )
        
        self.workflow = Workflow.objects.create(
            organizer=self.organizer,
            name='Test Workflow',
            trigger='booking_created',
            is_active=True
        )
    
    def test_invalid_email_recipient(self):
        """Test handling of invalid email recipients."""
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='Invalid Email',
            action_type='send_email',
            order=1,
            recipient='custom',
            custom_email='',  # Invalid - empty email
            subject='Test',
            message='Test message',
            is_active=True
        )
        
        context = build_context_data_from_booking(self.booking)
        
        # Should raise ValueError
        with self.assertRaises(ValueError) as cm:
            execute_workflow_action(action, self.booking, context, test_mode=False)
        
        self.assertIn('Custom email recipient not specified', str(cm.exception))
    
    def test_invalid_phone_number(self):
        """Test handling of invalid phone numbers for SMS."""
        # Remove phone from booking
        self.booking.invitee_phone = ''
        self.booking.save()
        
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='SMS to Invitee',
            action_type='send_sms',
            order=1,
            recipient='invitee',
            message='Test SMS',
            is_active=True
        )
        
        context = build_context_data_from_booking(self.booking)
        
        # Should raise ValueError
        with self.assertRaises(ValueError) as cm:
            execute_workflow_action(action, self.booking, context, test_mode=False)
        
        self.assertIn('phone number not available', str(cm.exception))
    
    @patch('apps.integrations.utils.make_api_request')
    def test_webhook_failure_handling(self, mock_api_request):
        """Test webhook failure scenarios."""
        # Mock webhook failure
        mock_api_request.side_effect = Exception("Network error")
        
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='Failing Webhook',
            action_type='webhook',
            order=1,
            webhook_url='https://api.example.com/webhook',
            is_active=True
        )
        
        context = build_context_data_from_booking(self.booking)
        
        # Should raise exception
        with self.assertRaises(Exception) as cm:
            execute_workflow_action(action, self.booking, context, test_mode=False)
        
        self.assertIn('Webhook failed', str(cm.exception))
    
    def test_invalid_booking_update_fields(self):
        """Test handling of invalid booking update fields."""
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='Invalid Update',
            action_type='update_booking',
            order=1,
            update_booking_fields={
                'invalid_field': 'some_value',
                'status': 'invalid_status'
            },
            is_active=True
        )
        
        context = build_context_data_from_booking(self.booking)
        
        # Should handle gracefully by filtering out invalid fields
        result = execute_workflow_action(action, self.booking, context, test_mode=False)
        
        # Should succeed but with no actual updates
        self.assertEqual(result['action_type'], 'update_booking')
        self.assertEqual(len(result['updated_fields']), 0)
    
    def test_malformed_conditions(self):
        """Test handling of malformed condition JSON."""
        action = WorkflowAction.objects.create(
            workflow=self.workflow,
            name='Bad Conditions',
            action_type='send_email',
            order=1,
            recipient='invitee',
            subject='Test',
            message='Test',
            conditions="invalid json",  # Invalid JSON
            is_active=True
        )
        
        context = build_context_data_from_booking(self.booking)
        
        # Should handle gracefully and skip action
        from .utils import evaluate_conditions
        result = evaluate_conditions(action.conditions, context)
        
        # Should return False for safety
        self.assertFalse(result)
    
    def test_workflow_execution_with_all_failures(self):
        """Test workflow execution where all actions fail."""
        # Create actions that will fail
        WorkflowAction.objects.create(
            workflow=self.workflow,
            name='Failing Email',
            action_type='send_email',
            order=1,
            recipient='custom',
            custom_email='',  # Will fail
            subject='Test',
            message='Test',
            is_active=True
        )
        
        WorkflowAction.objects.create(
            workflow=self.workflow,
            name='Failing Webhook',
            action_type='webhook',
            order=2,
            webhook_url='',  # Will fail
            is_active=True
        )
        
        # Execute workflow
        from .tasks import execute_workflow
        result = execute_workflow(self.workflow.id, self.booking.id, test_mode=False, delay_applied=True)
        
        # Check execution was marked as failed
        execution = WorkflowExecution.objects.filter(workflow=self.workflow).first()
        self.assertIsNotNone(execution)
        self.assertEqual(execution.status, 'failed')
        self.assertEqual(execution.actions_executed, 0)
        self.assertEqual(execution.actions_failed, 2)
        
        # Check error message was set
        self.assertIsNotNone(execution.error_message)


class WorkflowAPITestCase(TestCase):
    """Test workflow API endpoints."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
        
        self.workflow = Workflow.objects.create(
            organizer=self.organizer,
            name='Test Workflow',
            trigger='booking_created',
            is_active=True
        )
    
    def test_workflow_validation_endpoint(self):
        """Test workflow validation API endpoint."""
        from django.urls import reverse
        
        self.client.force_login(self.organizer)
        
        url = reverse('workflows:workflow-validate', kwargs={'pk': self.workflow.pk})
        response = self.client.post(url)
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertIn('valid', data)
        self.assertIn('warnings', data)
        self.assertIn('errors', data)
        self.assertIn('runtime_checks', data)
        self.assertIn('overall_status', data)
    
    def test_workflow_test_endpoint(self):
        """Test workflow testing API endpoint."""
        from django.urls import reverse
        
        self.client.force_login(self.organizer)
        
        url = reverse('workflows:workflow-test', kwargs={'pk': self.workflow.pk})
        
        # Test with mock data
        response = self.client.post(url, {
            'test_type': 'mock_data'
        }, content_type='application/json')
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertIn('message', data)
        self.assertIn('task_id', data)
        self.assertEqual(data['test_type'], 'mock_data')
    
    def test_workflow_performance_stats_endpoint(self):
        """Test workflow performance statistics endpoint."""
        from django.urls import reverse
        
        self.client.force_login(self.organizer)
        
        url = reverse('workflows:performance-stats')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertIn('total_workflows', data)
        self.assertIn('active_workflows', data)
        self.assertIn('execution_stats_30_days', data)
        self.assertIn('top_performing_workflows', data)
        self.assertIn('problematic_workflows', data)


class WorkflowConditionTestCase(TestCase):
    """Comprehensive tests for workflow condition evaluation."""
    
    def test_all_operators(self):
        """Test all supported condition operators."""
        context = {
            'string_field': 'Hello World',
            'number_field': 42,
            'empty_field': '',
            'null_field': None,
            'list_field': ['item1', 'item2'],
            'email_field': 'user@example.com'
        }
        
        # Test equals
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "string_field", "operator": "equals", "value": "Hello World"}]
        }], context))
        
        # Test not_equals
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "string_field", "operator": "not_equals", "value": "Goodbye"}]
        }], context))
        
        # Test greater_than
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "number_field", "operator": "greater_than", "value": "40"}]
        }], context))
        
        # Test less_than
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "number_field", "operator": "less_than", "value": "50"}]
        }], context))
        
        # Test contains
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "string_field", "operator": "contains", "value": "World"}]
        }], context))
        
        # Test starts_with
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "string_field", "operator": "starts_with", "value": "Hello"}]
        }], context))
        
        # Test ends_with
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "email_field", "operator": "ends_with", "value": ".com"}]
        }], context))
        
        # Test is_empty
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "empty_field", "operator": "is_empty"}]
        }], context))
        
        # Test is_not_empty
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "string_field", "operator": "is_not_empty"}]
        }], context))
    
    def test_condition_edge_cases(self):
        """Test edge cases in condition evaluation."""
        context = {
            'mixed_case': 'Hello WORLD',
            'numeric_string': '123',
            'boolean_field': True,
            'zero_value': 0,
            'false_value': False
        }
        
        # Test case insensitive comparison
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "mixed_case", "operator": "contains", "value": "world"}]
        }], context))
        
        # Test numeric string comparison
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "numeric_string", "operator": "greater_than", "value": "100"}]
        }], context))
        
        # Test boolean handling
        self.assertTrue(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "boolean_field", "operator": "equals", "value": "true"}]
        }], context))
        
        # Test zero/false handling
        self.assertFalse(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "zero_value", "operator": "is_empty"}]
        }], context))  # 0 is not considered empty
        
        self.assertFalse(evaluate_conditions([{
            "operator": "AND",
            "rules": [{"field": "false_value", "operator": "is_empty"}]
        }], context))  # False is not considered empty


class WorkflowModelTestCase(TestCase):
    """Test workflow model functionality."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
    
    def test_workflow_action_validation(self):
        """Test WorkflowAction model validation."""
        workflow = Workflow.objects.create(
            organizer=self.organizer,
            name='Test Workflow',
            trigger='booking_created'
        )
        
        # Test valid action
        action = WorkflowAction(
            workflow=workflow,
            name='Valid Action',
            action_type='send_email',
            recipient='invitee',
            subject='Test',
            message='Test message',
            conditions=[{
                "operator": "AND",
                "rules": [{"field": "test_field", "operator": "equals", "value": "test"}]
            }]
        )
        
        # Should not raise ValidationError
        action.full_clean()
        
        # Test invalid conditions structure
        action.conditions = "invalid json"
        
        with self.assertRaises(ValidationError):
            action.full_clean()
        
        # Test invalid update_booking_fields
        action.action_type = 'update_booking'
        action.update_booking_fields = {'invalid_field': 'value'}
        
        with self.assertRaises(ValidationError):
            action.full_clean()
    
    def test_workflow_statistics(self):
        """Test workflow execution statistics."""
        workflow = Workflow.objects.create(
            organizer=self.organizer,
            name='Stats Test Workflow',
            trigger='booking_created'
        )
        
        # Initial stats should be zero
        self.assertEqual(workflow.get_success_rate(), 0)
        self.assertEqual(workflow.total_executions, 0)
        
        # Increment stats
        workflow.increment_execution_stats(success=True)
        workflow.increment_execution_stats(success=True)
        workflow.increment_execution_stats(success=False)
        
        # Check updated stats
        workflow.refresh_from_db()
        self.assertEqual(workflow.total_executions, 3)
        self.assertEqual(workflow.successful_executions, 2)
        self.assertEqual(workflow.failed_executions, 1)
        self.assertEqual(workflow.get_success_rate(), 66.67)