from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from django.db import transaction
from .models import Workflow, WorkflowExecution, WorkflowAction
from .utils import (
    build_context_data_from_booking, evaluate_conditions, validate_update_booking_fields,
    get_workflow_execution_summary, log_workflow_performance, create_test_context_data
)
import requests
import json
import time
import logging

logger = logging.getLogger(__name__)


@shared_task
def execute_workflow(workflow_id, booking_id=None, test_mode=False, delay_applied=False):
    """Execute a workflow for a booking."""
    execution_start_time = time.time()
    
    try:
        workflow = Workflow.objects.get(id=workflow_id, is_active=True)
        
        # Handle delay if not already applied
        if not delay_applied and workflow.delay_minutes > 0:
            logger.info(f"Scheduling workflow {workflow.name} with {workflow.delay_minutes} minute delay")
            execute_workflow.apply_async(
                args=[workflow_id, booking_id, test_mode, True],  # delay_applied=True
                countdown=workflow.delay_minutes * 60
            )
            return f"Workflow {workflow.name} scheduled with {workflow.delay_minutes} minute delay"
        
        if not test_mode and booking_id:
            from apps.events.models import Booking
            booking = Booking.objects.get(id=booking_id)
        elif test_mode:
            booking = None  # Use test data
        else:
            raise ValueError("booking_id is required when not in test_mode")
        
        # Create execution record with transaction safety
        with transaction.atomic():
            execution = WorkflowExecution.objects.create(
                workflow=workflow,
                booking=booking,
                status='running',
                started_at=timezone.now()
            )
        
            # Build context data for condition evaluation
            if test_mode:
                context_data = create_test_context_data()
            else:
                context_data = build_context_data_from_booking(booking)
            
            # Execute actions in order
            actions = workflow.actions.filter(is_active=True).order_by('order')
            execution_log = []
            actions_executed = 0
            actions_failed = 0
            actions_skipped = 0
            
            logger.info(f"Executing workflow '{workflow.name}' with {actions.count()} actions")
        
            for action in actions:
                action_start_time = time.time()
                
                try:
                    # Evaluate conditions first
                    if action.conditions:
                        conditions_met = evaluate_conditions(action.conditions, context_data)
                        if not conditions_met:
                            execution_log.append({
                                'action_id': str(action.id),
                                'action_name': action.name,
                                'action_type': action.action_type,
                                'status': 'skipped_conditions',
                                'message': 'Action skipped due to unmet conditions',
                                'conditions_evaluated': action.conditions,
                                'timestamp': timezone.now().isoformat(),
                                'execution_time_ms': round((time.time() - action_start_time) * 1000, 2)
                            })
                            actions_skipped += 1
                            continue
                    
                    # Execute the action
                    result = execute_workflow_action(action, booking, context_data, test_mode)
                    
                    execution_log.append({
                        'action_id': str(action.id),
                        'action_name': action.name,
                        'action_type': action.action_type,
                        'status': 'success',
                        'result': result,
                        'timestamp': timezone.now().isoformat(),
                        'execution_time_ms': round((time.time() - action_start_time) * 1000, 2)
                    })
                    actions_executed += 1
                    
                    # Update action statistics
                    action.increment_execution_stats(success=True)
                    
                except Exception as e:
                    error_message = str(e)
                    logger.error(f"Error executing action '{action.name}' in workflow '{workflow.name}': {error_message}")
                    
                    execution_log.append({
                        'action_id': str(action.id),
                        'action_name': action.name,
                        'action_type': action.action_type,
                        'status': 'failed',
                        'error': error_message,
                        'timestamp': timezone.now().isoformat(),
                        'execution_time_ms': round((time.time() - action_start_time) * 1000, 2)
                    })
                    actions_failed += 1
                    
                    # Update action statistics
                    action.increment_execution_stats(success=False)
        
            # Calculate total execution time
            total_execution_time = time.time() - execution_start_time
            
            # Update execution record
            execution.status = 'completed' if actions_failed == 0 else 'failed'
            execution.completed_at = timezone.now()
            execution.actions_executed = actions_executed
            execution.actions_failed = actions_failed
            execution.execution_log = execution_log
            
            # Add summary to error message if there were failures
            if actions_failed > 0:
                summary = get_workflow_execution_summary(execution)
                execution.error_message = summary['summary']
            
            execution.save()
            
            # Update workflow statistics
            workflow.increment_execution_stats(success=(actions_failed == 0))
            
            # Log performance metrics
            log_workflow_performance(
                workflow_id, total_execution_time, len(actions), 
                actions_executed, actions_failed
            )
        
        result_message = f"Workflow '{workflow.name}' executed: {actions_executed} success, {actions_failed} failed"
        if actions_skipped > 0:
            result_message += f", {actions_skipped} skipped"
        
        return result_message
        
    
    except Workflow.DoesNotExist:
        error_msg = f"Workflow {workflow_id} not found"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Critical error executing workflow {workflow_id}: {str(e)}"
        logger.error(error_msg)
        
        # Try to update execution record if it exists
        try:
            execution = WorkflowExecution.objects.filter(
                workflow_id=workflow_id,
                status='running'
            ).order_by('-created_at').first()
            
            if execution:
                execution.status = 'failed'
                execution.completed_at = timezone.now()
                execution.error_message = error_msg
                execution.save()
        except Exception:
            pass  # Don't let logging errors crash the task
        
        return error_msg
    except Exception as e:
        return f"Error executing workflow: {str(e)}"


def execute_workflow_action(action, booking, context_data, test_mode=False):
    """Execute a single workflow action."""
    logger.debug(f"Executing action '{action.name}' of type '{action.action_type}'")
    
    if action.action_type == 'send_email':
        return execute_email_action(action, booking, context_data, test_mode)
    elif action.action_type == 'send_sms':
        return execute_sms_action(action, booking, context_data, test_mode)
    elif action.action_type == 'webhook':
        return execute_webhook_action(action, booking, context_data, test_mode)
    elif action.action_type == 'update_booking':
        return execute_update_booking_action(action, booking, context_data, test_mode)
    else:
        raise ValueError(f"Unknown action type: {action.action_type}")


def execute_email_action(action, booking, context_data, test_mode=False):
    """Execute email action."""
    if test_mode:
        from apps.notifications.utils import render_template_with_fallbacks
        subject = render_template_with_fallbacks(action.subject, context_data)
        message = render_template_with_fallbacks(action.message, context_data)
        
        # Determine test recipients
        test_recipients = []
        if action.recipient == 'organizer':
            test_recipients.append(context_data.get('organizer_email', 'test-organizer@example.com'))
        elif action.recipient == 'invitee':
            test_recipients.append(context_data.get('invitee_email', 'test-invitee@example.com'))
        elif action.recipient == 'custom':
            test_recipients.append(action.custom_email or 'test-custom@example.com')
        elif action.recipient == 'both':
            test_recipients.extend([
                context_data.get('organizer_email', 'test-organizer@example.com'),
                context_data.get('invitee_email', 'test-invitee@example.com')
            ])
        
        return {
            'action_type': 'send_email',
            'subject': subject,
            'message': message,
            'recipients': test_recipients,
            'test_mode': True
        }
    
    # Production mode - determine recipients
    recipients = []
    if action.recipient == 'organizer':
        recipients.append({
            'email': booking.organizer.email,
            'name': booking.organizer.first_name
        })
    elif action.recipient == 'invitee':
        recipients.append({
            'email': booking.invitee_email,
            'name': booking.invitee_name
        })
    elif action.recipient == 'custom':
        if not action.custom_email:
            raise ValueError("Custom email recipient not specified")
        recipients.append({
            'email': action.custom_email,
            'name': 'Recipient'
        })
    elif action.recipient == 'both':
        recipients.extend([
            {'email': booking.organizer.email, 'name': booking.organizer.first_name},
            {'email': booking.invitee_email, 'name': booking.invitee_name}
        ])
    else:
        raise ValueError(f"Invalid recipient type: {action.recipient}")
    
    # Render content with booking context
    from apps.notifications.utils import render_template_with_fallbacks, get_notification_context_from_booking
    
    # Use booking context for notifications (more comprehensive than workflow context)
    notification_context = get_notification_context_from_booking(booking)
    subject = render_template_with_fallbacks(action.subject, notification_context)
    message = render_template_with_fallbacks(action.message, notification_context)
    
    if not subject.strip():
        raise ValueError("Email subject cannot be empty after template rendering")
    if not message.strip():
        raise ValueError("Email message cannot be empty after template rendering")
    
    # Create and send notification(s)
    from apps.notifications.models import NotificationLog
    from apps.notifications.tasks import send_notification_task
    
    notification_ids = []
    
    for recipient in recipients:
        log = NotificationLog.objects.create(
            organizer=booking.organizer,
            booking=booking,
            notification_type='email',
            recipient_email=recipient['email'],
            subject=subject,
            message=message,
            status='pending'
        )
        
        notification_ids.append(str(log.id))
        send_notification_task.delay(log.id)
    
    return {
        'action_type': 'send_email',
        'subject': subject,
        'recipients_count': len(recipients),
        'notification_ids': notification_ids,
        'status': 'queued'
    }


def execute_sms_action(action, booking, context_data, test_mode=False):
    """Execute SMS action."""
    if test_mode:
        from apps.notifications.utils import render_template_with_fallbacks
        message = render_template_with_fallbacks(action.message, context_data)
        
        # Determine test recipients
        test_recipients = []
        if action.recipient == 'organizer':
            test_recipients.append(context_data.get('organizer_phone', '+1234567890'))
        elif action.recipient == 'invitee':
            test_recipients.append(context_data.get('invitee_phone', '+1234567890'))
        elif action.recipient == 'both':
            test_recipients.extend([
                context_data.get('organizer_phone', '+1234567890'),
                context_data.get('invitee_phone', '+1234567890')
            ])
        
        return {
            'action_type': 'send_sms',
            'message': message,
            'recipients': test_recipients,
            'test_mode': True
        }
    
    # Production mode - determine recipient phones
    recipients = []
    if action.recipient == 'organizer':
        organizer_phone = getattr(booking.organizer.profile, 'phone', '') if hasattr(booking.organizer, 'profile') else ''
        if not organizer_phone:
            raise ValueError("Organizer phone number not available")
        recipients.append({
            'phone': organizer_phone,
            'name': booking.organizer.first_name
        })
    elif action.recipient == 'invitee':
        if not booking.invitee_phone:
            raise ValueError("Invitee phone number not available")
        recipients.append({
            'phone': booking.invitee_phone,
            'name': booking.invitee_name
        })
    elif action.recipient == 'custom':
        # For custom SMS, use custom_email field as phone number
        # This is a design decision - custom_email can store phone for SMS actions
        if not action.custom_email:
            raise ValueError("Custom phone number not specified in custom_email field")
        recipients.append({
            'phone': action.custom_email,  # Repurposed for phone
            'name': 'Recipient'
        })
    elif action.recipient == 'both':
        organizer_phone = getattr(booking.organizer.profile, 'phone', '') if hasattr(booking.organizer, 'profile') else ''
        if organizer_phone:
            recipients.append({
                'phone': organizer_phone,
                'name': booking.organizer.first_name
            })
        if booking.invitee_phone:
            recipients.append({
                'phone': booking.invitee_phone,
                'name': booking.invitee_name
            })
        
        if not recipients:
            raise ValueError("No phone numbers available for SMS")
    else:
        raise ValueError(f"Invalid recipient type: {action.recipient}")
    
    # Render message content
    from apps.notifications.utils import render_template_with_fallbacks, get_notification_context_from_booking
    notification_context = get_notification_context_from_booking(booking)
    message = render_template_with_fallbacks(action.message, notification_context)
    
    if not message.strip():
        raise ValueError("SMS message cannot be empty after template rendering")
    
    # Validate phone numbers
    from apps.notifications.utils import validate_phone_number
    valid_recipients = []
    
    for recipient in recipients:
        phone_validation = validate_phone_number(recipient['phone'])
        if phone_validation['valid']:
            recipient['formatted_phone'] = phone_validation['formatted']
            valid_recipients.append(recipient)
        else:
            logger.warning(f"Invalid phone number for {recipient['name']}: {phone_validation['error']}")
    
    if not valid_recipients:
        raise ValueError("No valid phone numbers found for SMS sending")
    
    # Create and send SMS notification(s)
    from apps.notifications.models import NotificationLog
    from apps.notifications.tasks import send_notification_task
    
    notification_ids = []
    
    for recipient in valid_recipients:
        log = NotificationLog.objects.create(
            organizer=booking.organizer,
            booking=booking,
            notification_type='sms',
            recipient_phone=recipient['formatted_phone'],
            message=message,
            status='pending'
        )
        
        notification_ids.append(str(log.id))
        send_notification_task.delay(log.id)
    
    return {
        'action_type': 'send_sms',
        'message': message,
        'recipients_count': len(valid_recipients),
        'notification_ids': notification_ids,
        'status': 'queued'
    }


def execute_webhook_action(action, booking, context_data, test_mode=False):
    """Execute webhook action."""
    if not action.webhook_url:
        raise ValueError("Webhook URL is required for webhook actions")
    
    if not action.webhook_url.startswith(('http://', 'https://')):
        raise ValueError("Webhook URL must start with http:// or https://")
    
    if test_mode:
        # Build test payload
        payload = {
            'action': action.name,
            'workflow_id': str(action.workflow.id),
            'workflow_name': action.workflow.name,
            'test_mode': True,
            'timestamp': timezone.now().isoformat(),
            **context_data
        }
        
        # Add custom webhook data
        if action.webhook_data:
            payload.update(action.webhook_data)
        
        return {
            'action_type': 'webhook',
            'url': action.webhook_url,
            'payload': payload,
            'test_mode': True,
            'payload_size_bytes': len(json.dumps(payload))
        }
    
    # Prepare webhook payload
    payload = {
        'action': action.name,
        'workflow_id': str(action.workflow.id),
        'workflow_name': action.workflow.name,
        'booking_id': str(booking.id),
        'event_type_name': booking.event_type.name,
        'event_type_slug': booking.event_type.event_type_slug,
        'organizer_email': booking.organizer.email,
        'invitee_name': booking.invitee_name,
        'invitee_email': booking.invitee_email,
        'start_time': booking.start_time.isoformat(),
        'end_time': booking.end_time.isoformat(),
        'duration_minutes': booking.duration_minutes,
        'status': booking.status,
        'timestamp': timezone.now().isoformat()
    }
    
    # Add custom webhook data
    if action.webhook_data:
        payload.update(action.webhook_data)
    
    # Send webhook using robust integration utilities
    from apps.integrations.utils import make_api_request
    
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Calendly-Clone-Workflow/1.0',
        'X-Workflow-ID': str(action.workflow.id),
        'X-Action-ID': str(action.id)
    }
    
    try:
        response = make_api_request(
            method='POST',
            url=action.webhook_url,
            headers=headers,
            json_data=payload,
            provider='webhook',
            organizer_id=booking.organizer.id
        )
        
        return {
            'action_type': 'webhook',
            'url': action.webhook_url,
            'status_code': response.status_code,
            'response_size_bytes': len(response.content),
            'payload_size_bytes': len(json.dumps(payload)),
            'status': 'success'
        }
        
    except Exception as e:
        # Log the webhook failure with details
        logger.error(f"Webhook failed for action '{action.name}': {str(e)}")
        raise Exception(f"Webhook failed: {str(e)}")


def execute_update_booking_action(action, booking, context_data, test_mode=False):
    """Execute booking update action."""
    if not action.update_booking_fields:
        raise ValueError("No fields specified for booking update")
    
    if test_mode:
        # Validate update fields without actually updating
        try:
            validated_fields = validate_update_booking_fields(action.update_booking_fields, booking)
            return {
                'action_type': 'update_booking',
                'fields_to_update': validated_fields,
                'test_mode': True,
                'validation_passed': True
            }
        except Exception as e:
            return {
                'action_type': 'update_booking',
                'test_mode': True,
                'validation_passed': False,
                'validation_error': str(e)
            }
    
    # Production mode - actually update the booking
    try:
        # Validate and prepare update fields
        validated_fields = validate_update_booking_fields(action.update_booking_fields, booking)
        
        if not validated_fields:
            raise ValueError("No valid fields to update after validation")
        
        # Store original values for logging
        original_values = {}
        for field_name in validated_fields.keys():
            original_values[field_name] = getattr(booking, field_name, None)
        
        # Apply updates
        updated_fields = []
        for field_name, new_value in validated_fields.items():
            try:
                # Special handling for custom_answers (merge instead of replace)
                if field_name == 'custom_answers':
                    current_answers = booking.custom_answers or {}
                    if isinstance(new_value, dict):
                        current_answers.update(new_value)
                        setattr(booking, field_name, current_answers)
                    else:
                        setattr(booking, field_name, new_value)
                else:
                    setattr(booking, field_name, new_value)
                
                updated_fields.append(field_name)
                
            except Exception as e:
                logger.error(f"Error setting field {field_name} to {new_value}: {str(e)}")
                continue
        
        if not updated_fields:
            raise ValueError("No fields were successfully updated")
        
        # Save the booking with only the updated fields
        booking.save(update_fields=updated_fields)
        
        # Create audit log for the update
        from apps.users.utils import create_audit_log
        create_audit_log(
            user=booking.organizer,
            action='booking_updated_by_workflow',
            description=f"Booking updated by workflow '{action.workflow.name}' action '{action.name}'",
            content_object=booking,
            metadata={
                'workflow_id': str(action.workflow.id),
                'action_id': str(action.id),
                'updated_fields': updated_fields,
                'original_values': original_values,
                'new_values': {field: getattr(booking, field) for field in updated_fields}
            }
        )
        
        return {
            'action_type': 'update_booking',
            'updated_fields': updated_fields,
            'original_values': original_values,
            'new_values': {field: getattr(booking, field) for field in updated_fields},
            'status': 'success'
        }
        
    except Exception as e:
        logger.error(f"Error updating booking in workflow action: {str(e)}")
        raise Exception(f"Booking update failed: {str(e)}")


@shared_task
def trigger_workflows(trigger_type, booking_id, **kwargs):
    """
    Trigger workflows based on booking events with enhanced error handling.
    
    Args:
        trigger_type: Type of trigger (booking_created, booking_cancelled, etc.)
        booking_id: UUID of the booking
        **kwargs: Additional context data
    """
    try:
        from apps.events.models import Booking
        booking = Booking.objects.get(id=booking_id)
        
        logger.info(f"Triggering workflows for {trigger_type} on booking {booking_id}")
        
        # Find workflows that match the trigger
        workflows = Workflow.objects.filter(
            organizer=booking.organizer,
            trigger=trigger_type,
            is_active=True
        ).prefetch_related('event_types', 'actions')
        
        triggered_count = 0
        
        # Filter and trigger workflows
        for workflow in workflows:
            try:
                # Check if workflow applies to this event type
                if workflow.event_types.exists():
                    if booking.event_type not in workflow.event_types.all():
                        logger.debug(f"Workflow '{workflow.name}' skipped - event type not in scope")
                        continue
                
                # Check if workflow has active actions
                if not workflow.actions.filter(is_active=True).exists():
                    logger.warning(f"Workflow '{workflow.name}' has no active actions")
                    continue
                
                # Schedule workflow execution
                if workflow.delay_minutes > 0:
                    logger.info(f"Scheduling workflow '{workflow.name}' with {workflow.delay_minutes} minute delay")
                    execute_workflow.apply_async(
                        args=[workflow.id, booking_id, False, False],  # test_mode=False, delay_applied=False
                        countdown=workflow.delay_minutes * 60
                    )
                else:
                    logger.info(f"Executing workflow '{workflow.name}' immediately")
                    execute_workflow.delay(workflow.id, booking_id, False, True)  # delay_applied=True
                
                triggered_count += 1
                
            except Exception as e:
                logger.error(f"Error triggering workflow '{workflow.name}': {str(e)}")
                continue
        
        result_message = f"Triggered {triggered_count} workflows for {trigger_type}"
        logger.info(result_message)
        return result_message
        
    except Booking.DoesNotExist:
        error_msg = f"Booking {booking_id} not found for workflow trigger"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Critical error triggering workflows for {trigger_type}: {str(e)}"
        logger.error(error_msg)
        return error_msg
    
    # This would implement booking update logic based on action conditions
    # For now, just return a success message
    return "Booking updated successfully (placeholder implementation)"


@shared_task
def cleanup_old_workflow_executions():
    """Clean up old workflow execution logs to prevent database bloat."""
    cutoff_date = timezone.now() - timezone.timedelta(days=90)  # Keep 90 days
    
    old_executions = WorkflowExecution.objects.filter(created_at__lt=cutoff_date)
    count = old_executions.count()
@shared_task
def monitor_workflow_performance():
    """Monitor workflow performance and alert on issues."""
    from datetime import timedelta
    
    # Check workflows with high failure rates
    recent_executions = WorkflowExecution.objects.filter(
        created_at__gte=timezone.now() - timedelta(hours=24)
    )
    
    problem_workflows = []
    
    # Group by workflow and calculate failure rates
    workflow_stats = {}
    for execution in recent_executions:
        workflow_id = execution.workflow.id
        if workflow_id not in workflow_stats:
            workflow_stats[workflow_id] = {
                'workflow': execution.workflow,
                'total': 0,
                'failed': 0,
                'avg_execution_time': 0,
                'execution_times': []
            }
        
        workflow_stats[workflow_id]['total'] += 1
        if execution.status == 'failed':
            workflow_stats[workflow_id]['failed'] += 1
        
        # Calculate execution time if available
        if execution.started_at and execution.completed_at:
            exec_time = (execution.completed_at - execution.started_at).total_seconds()
            workflow_stats[workflow_id]['execution_times'].append(exec_time)
    
    # Identify problematic workflows
    for workflow_id, stats in workflow_stats.items():
        failure_rate = (stats['failed'] / stats['total']) * 100 if stats['total'] > 0 else 0
        
        # Calculate average execution time
        if stats['execution_times']:
            stats['avg_execution_time'] = sum(stats['execution_times']) / len(stats['execution_times'])
        
        # Flag workflows with high failure rate (>50%) or slow execution (>60s avg)
        if (failure_rate > 50 and stats['total'] >= 3) or stats['avg_execution_time'] > 60:
            problem_workflows.append({
                'workflow': stats['workflow'],
                'failure_rate': failure_rate,
                'total_executions': stats['total'],
                'avg_execution_time': stats['avg_execution_time']
            })
    
    # Alert administrators if problems found
    if problem_workflows:
        alert_workflow_performance_issues.delay(problem_workflows)
    
    return f"Monitored {len(workflow_stats)} workflows, found {len(problem_workflows)} with issues"
    old_executions.delete()
    
@shared_task
def alert_workflow_performance_issues(problem_workflows):
    """Alert administrators about workflow performance issues."""
    try:
        subject = f"Workflow Performance Issues Detected ({len(problem_workflows)} workflows)"
        
        message_lines = [
            "The following workflows are experiencing performance issues:\n"
        ]
        
        for problem in problem_workflows:
            workflow = problem['workflow']
            message_lines.append(
                f"• {workflow.name} (ID: {workflow.id})\n"
                f"  Organizer: {workflow.organizer.email}\n"
                f"  Failure Rate: {problem['failure_rate']:.1f}%\n"
                f"  Total Executions (24h): {problem['total_executions']}\n"
                f"  Avg Execution Time: {problem['avg_execution_time']:.1f}s\n"
            )
        
        message_lines.append(
            "\nPlease review these workflows in the admin panel and check for:\n"
            "- Invalid webhook URLs\n"
            "- Malformed conditions\n"
            "- External service issues\n"
            "- Network connectivity problems\n"
        )
        
        message = '\n'.join(message_lines)
        
        # Send to admin emails if configured
        admin_emails = getattr(settings, 'ADMIN_NOTIFICATION_EMAILS', [])
        if admin_emails:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                admin_emails,
                fail_silently=True,
            )
            
            logger.info(f"Workflow performance alert sent to {len(admin_emails)} administrators")
        
        return f"Performance alert sent for {len(problem_workflows)} problematic workflows"
        
    except Exception as e:
        logger.error(f"Error sending workflow performance alert: {str(e)}")
        return f"Error sending performance alert: {str(e)}"
    logger.info(f"Cleaned up {count} old workflow executions")
    return f"Cleaned up {count} old workflow executions"
@shared_task
def validate_all_workflow_configurations():
    """Validate all workflow configurations and report issues."""
    from .utils import validate_workflow_configuration
    
    all_workflows = Workflow.objects.filter(is_active=True)
    validation_results = []
    
    for workflow in all_workflows:
        try:
            validation = validate_workflow_configuration(workflow)
            if not validation['valid'] or validation['warnings']:
                validation_results.append({
                    'workflow_id': str(workflow.id),
                    'workflow_name': workflow.name,
                    'organizer_email': workflow.organizer.email,
                    'validation': validation
                })
        except Exception as e:
            validation_results.append({
                'workflow_id': str(workflow.id),
                'workflow_name': workflow.name,
                'organizer_email': workflow.organizer.email,
                'validation': {
                    'valid': False,
                    'errors': [f"Validation error: {str(e)}"],
                    'warnings': []
                }
            })
    
    # Log results
    if validation_results:
        logger.warning(f"Found {len(validation_results)} workflows with configuration issues")
        for result in validation_results:
            logger.warning(f"Workflow '{result['workflow_name']}' ({result['organizer_email']}): {result['validation']}")
    else:
        logger.info("All active workflows have valid configurations")
    
    return f"Validated {all_workflows.count()} workflows, found {len(validation_results)} with issues"
@shared_task
def test_workflow_with_real_data(workflow_id, booking_id):
    """
    Test a workflow using real booking data but in safe test mode.
    
    Args:
        workflow_id: UUID of workflow to test
        booking_id: UUID of booking to use as test data
    """
    try:
        workflow = Workflow.objects.get(id=workflow_id, is_active=True)
        from apps.events.models import Booking
        booking = Booking.objects.get(id=booking_id)
        
        # Verify organizer owns both workflow and booking
        if workflow.organizer != booking.organizer:
            raise ValueError("Workflow and booking must belong to the same organizer")
        
        # Execute in test mode
        result = execute_workflow(workflow_id, booking_id, test_mode=True, delay_applied=True)
        
        return f"Test completed for workflow '{workflow.name}': {result}"
        
    except (Workflow.DoesNotExist, Booking.DoesNotExist) as e:
        error_msg = f"Workflow or booking not found: {str(e)}"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Error testing workflow: {str(e)}"
        logger.error(error_msg)
        return error_msg


@shared_task
def bulk_execute_workflows(workflow_ids, booking_ids, test_mode=False):
    """
    Execute multiple workflows in bulk for testing or batch operations.
    
    Args:
        workflow_ids: List of workflow UUIDs
        booking_ids: List of booking UUIDs (must match length of workflow_ids)
        test_mode: Whether to run in test mode
    """
    if len(workflow_ids) != len(booking_ids):
        return "Error: workflow_ids and booking_ids must have the same length"
    
    results = []
    
    for workflow_id, booking_id in zip(workflow_ids, booking_ids):
        try:
            # Add small delay between executions to avoid overwhelming external services
            if len(results) > 0:
                time.sleep(1)
            
            result = execute_workflow(workflow_id, booking_id, test_mode, delay_applied=True)
            results.append({
                'workflow_id': workflow_id,
                'booking_id': booking_id,
                'status': 'success',
                'result': result
            })
            
        except Exception as e:
            results.append({
                'workflow_id': workflow_id,
                'booking_id': booking_id,
                'status': 'error',
                'error': str(e)
            })
    
    successful = sum(1 for r in results if r['status'] == 'success')
    failed = len(results) - successful
    
    return f"Bulk execution completed: {successful} successful, {failed} failed"


@shared_task  
def execute_delayed_workflow_action(action_id, booking_id, context_data, delay_minutes):
    """
    Execute a single workflow action with delay (for future enhancements).
    
    Args:
        action_id: UUID of the action to execute
        booking_id: UUID of the booking
        context_data: Pre-built context data
        delay_minutes: Minutes to delay execution
    """
    
        action = WorkflowAction.objects.get(id=action_id, is_active=True)
    except Booking.DoesNotExist:
        return f"Booking {booking_id} not found"
        
        # Execute the single action
        result = execute_workflow_action(action, booking, context_data, test_mode=False)
        
        # Update action statistics
        action.increment_execution_stats(success=True)
        
        return f"Delayed action '{action.name}' executed successfully: {result}"
        
    except (WorkflowAction.DoesNotExist, Booking.DoesNotExist) as e:
        error_msg = f"Action or booking not found: {str(e)}"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Error executing delayed action: {str(e)}"
        logger.error(error_msg)
        
        # Update action statistics
        try:
            action = WorkflowAction.objects.get(id=action_id)
            action.increment_execution_stats(success=False)
        except:
            pass
        
        return error_msg