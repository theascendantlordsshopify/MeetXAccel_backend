"""
Utility functions for workflow execution with enterprise-grade conditional logic.
"""
import logging
from datetime import datetime
from django.utils import timezone
from django.db import models
import re

logger = logging.getLogger(__name__)


def build_context_data_from_booking(booking):
    """
    Build comprehensive context data from booking for condition evaluation.
    
    Args:
        booking: Booking instance
    
    Returns:
        dict: Flattened context data for condition evaluation
    """
    if not booking:
        return {}
    
    context = {
        # Booking fields
        'booking_id': str(booking.id),
        'booking_status': booking.status,
        'invitee_name': booking.invitee_name or '',
        'invitee_email': booking.invitee_email or '',
        'invitee_phone': booking.invitee_phone or '',
        'invitee_timezone': booking.invitee_timezone or 'UTC',
        'attendee_count': getattr(booking, 'attendee_count', 1),
        'start_time': booking.start_time,
        'end_time': booking.end_time,
        'duration': booking.duration_minutes,
        'cancellation_reason': getattr(booking, 'cancellation_reason', '') or '',
        'meeting_link': booking.meeting_link or '',
        'meeting_id': booking.meeting_id or '',
        'meeting_password': booking.meeting_password or '',
        
        # Event type fields
        'event_type_id': str(booking.event_type.id),
        'event_type_name': booking.event_type.name,
        'event_type_slug': booking.event_type.event_type_slug,
        'event_type_description': booking.event_type.description or '',
        'event_type_duration': booking.event_type.duration,
        'event_type_location_type': booking.event_type.location_type,
        'event_type_max_attendees': getattr(booking.event_type, 'max_attendees', 1),
        
        # Organizer fields
        'organizer_id': str(booking.organizer.id),
        'organizer_email': booking.organizer.email,
        'organizer_name': f"{booking.organizer.first_name} {booking.organizer.last_name}".strip(),
        'organizer_first_name': booking.organizer.first_name or '',
        'organizer_last_name': booking.organizer.last_name or '',
        'organizer_timezone': booking.organizer.profile.timezone_name if hasattr(booking.organizer, 'profile') else 'UTC',
        'organizer_company': booking.organizer.profile.company if hasattr(booking.organizer, 'profile') else '',
        
        # Time-based fields for conditions
        'booking_hour': booking.start_time.hour,
        'booking_day_of_week': booking.start_time.weekday(),  # 0=Monday, 6=Sunday
        'booking_date': booking.start_time.date(),
        'is_weekend': booking.start_time.weekday() >= 5,
        'is_business_hours': 9 <= booking.start_time.hour <= 17,
        
        # Custom answers (flattened)
        **{f"custom_{key}": value for key, value in (booking.custom_answers or {}).items()},
        
        # Derived fields
        'invitee_domain': booking.invitee_email.split('@')[1] if booking.invitee_email and '@' in booking.invitee_email else '',
        'has_phone': bool(booking.invitee_phone),
        'has_meeting_link': bool(booking.meeting_link),
        'booking_created_today': booking.created_at.date() == timezone.now().date(),
    }
    
    return context


def evaluate_conditions(conditions_json, context_data):
    """
    Evaluate workflow conditions with enterprise-grade logic.
    
    Args:
        conditions_json: List of condition groups from WorkflowAction.conditions
        context_data: Dictionary of data for evaluation
    
    Returns:
        bool: True if all conditions are met, False otherwise
    """
    if not conditions_json:
        return True  # No conditions means always execute
    
    try:
        # Validate structure
        if not isinstance(conditions_json, list):
            logger.error("Conditions must be a list of condition groups")
            return False
        
        # Evaluate each condition group
        group_results = []
        
        for group in conditions_json:
            if not isinstance(group, dict) or 'operator' not in group or 'rules' not in group:
                logger.error(f"Invalid condition group structure: {group}")
                return False
            
            group_operator = group['operator'].upper()
            rules = group['rules']
            
            if group_operator not in ['AND', 'OR']:
                logger.error(f"Invalid group operator: {group_operator}")
                return False
            
            # Evaluate rules within this group
            rule_results = []
            for rule in rules:
                try:
                    rule_result = evaluate_single_rule(rule, context_data)
                    rule_results.append(rule_result)
                except Exception as e:
                    logger.error(f"Error evaluating rule {rule}: {str(e)}")
                    rule_results.append(False)  # Fail safe
            
            # Apply group operator
            if group_operator == 'AND':
                group_result = all(rule_results)
            else:  # OR
                group_result = any(rule_results)
            
            group_results.append(group_result)
        
        # All groups must be true (implicit AND between groups)
        return all(group_results)
        
    except Exception as e:
        logger.error(f"Error evaluating conditions: {str(e)}")
        return False  # Fail safe


def evaluate_single_rule(rule, context_data):
    """
    Evaluate a single condition rule.
    
    Args:
        rule: Dictionary with 'field', 'operator', and optionally 'value'
        context_data: Dictionary of data for evaluation
    
    Returns:
        bool: True if rule is satisfied, False otherwise
    """
    field = rule.get('field')
    operator = rule.get('operator')
    expected_value = rule.get('value')
    
    if not field or not operator:
        logger.error(f"Rule missing required fields: {rule}")
        return False
    
    # Get actual value from context
    actual_value = get_nested_value(context_data, field)
    
    # Handle different operators
    try:
        if operator == 'equals':
            return safe_equals(actual_value, expected_value)
        elif operator == 'not_equals':
            return not safe_equals(actual_value, expected_value)
        elif operator == 'greater_than':
            return safe_numeric_compare(actual_value, expected_value, '>')
        elif operator == 'less_than':
            return safe_numeric_compare(actual_value, expected_value, '<')
        elif operator == 'greater_than_or_equal':
            return safe_numeric_compare(actual_value, expected_value, '>=')
        elif operator == 'less_than_or_equal':
            return safe_numeric_compare(actual_value, expected_value, '<=')
        elif operator == 'contains':
            return safe_string_contains(actual_value, expected_value)
        elif operator == 'not_contains':
            return not safe_string_contains(actual_value, expected_value)
        elif operator == 'starts_with':
            return safe_string_operation(actual_value, expected_value, 'startswith')
        elif operator == 'ends_with':
            return safe_string_operation(actual_value, expected_value, 'endswith')
        elif operator == 'is_empty':
            return is_empty_value(actual_value)
        elif operator == 'is_not_empty':
            return not is_empty_value(actual_value)
        elif operator == 'in_list':
            return safe_in_list(actual_value, expected_value)
        elif operator == 'not_in_list':
            return not safe_in_list(actual_value, expected_value)
        elif operator == 'regex_match':
            return safe_regex_match(actual_value, expected_value)
        else:
            logger.error(f"Unknown operator: {operator}")
            return False
            
    except Exception as e:
        logger.error(f"Error evaluating rule {rule}: {str(e)}")
        return False


def get_nested_value(data, field_path):
    """
    Get value from nested dictionary using dot notation.
    
    Args:
        data: Dictionary to search
        field_path: Field path (e.g., 'user.profile.name')
    
    Returns:
        Any: Value at the field path, or None if not found
    """
    try:
        value = data
        for key in field_path.split('.'):
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return None
            
            if value is None:
                return None
        
        return value
    except Exception:
        return None


def safe_equals(actual, expected):
    """Safe equality comparison handling different types."""
    if actual is None and expected is None:
        return True
    if actual is None or expected is None:
        return False
    
    # Convert to strings for comparison if types don't match
    if type(actual) != type(expected):
        return str(actual).lower() == str(expected).lower()
    
    return actual == expected


def safe_numeric_compare(actual, expected, operator):
    """Safe numeric comparison with type conversion."""
    try:
        # Convert to numbers
        if isinstance(actual, str):
            actual_num = float(actual) if '.' in actual else int(actual)
        else:
            actual_num = float(actual)
        
        if isinstance(expected, str):
            expected_num = float(expected) if '.' in expected else int(expected)
        else:
            expected_num = float(expected)
        
        if operator == '>':
            return actual_num > expected_num
        elif operator == '<':
            return actual_num < expected_num
        elif operator == '>=':
            return actual_num >= expected_num
        elif operator == '<=':
            return actual_num <= expected_num
        
    except (ValueError, TypeError):
        return False
    
    return False


def safe_string_contains(actual, expected):
    """Safe string contains check."""
    try:
        actual_str = str(actual).lower() if actual is not None else ''
        expected_str = str(expected).lower() if expected is not None else ''
        return expected_str in actual_str
    except Exception:
        return False


def safe_string_operation(actual, expected, operation):
    """Safe string operation (startswith, endswith)."""
    try:
        actual_str = str(actual).lower() if actual is not None else ''
        expected_str = str(expected).lower() if expected is not None else ''
        
        if operation == 'startswith':
            return actual_str.startswith(expected_str)
        elif operation == 'endswith':
            return actual_str.endswith(expected_str)
        
    except Exception:
        return False
    
    return False


def safe_in_list(actual, expected_list):
    """Safe 'in list' check."""
    try:
        if not isinstance(expected_list, list):
            # Try to parse as JSON if it's a string
            if isinstance(expected_list, str):
                expected_list = json.loads(expected_list)
            else:
                return False
        
        # Convert actual to string for comparison
        actual_str = str(actual).lower() if actual is not None else ''
        expected_list_lower = [str(item).lower() for item in expected_list]
        
        return actual_str in expected_list_lower
        
    except Exception:
        return False


def safe_regex_match(actual, pattern):
    """Safe regex matching."""
    try:
        actual_str = str(actual) if actual is not None else ''
        return bool(re.search(pattern, actual_str, re.IGNORECASE))
    except Exception:
        return False


def is_empty_value(value):
    """Check if value is considered empty."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == '':
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    
    return False


def validate_update_booking_fields(update_fields, booking):
    """
    Validate and prepare booking field updates.
    
    Args:
        update_fields: Dictionary of fields to update
        booking: Booking instance to validate against
    
    Returns:
        dict: Validated and type-cast update fields
    """
    if not isinstance(update_fields, dict):
        raise ValueError("update_fields must be a dictionary")
    
    validated_fields = {}
    
    # Define field types and validation rules
    field_validators = {
        'status': {
            'type': str,
            'choices': ['confirmed', 'cancelled', 'rescheduled', 'completed'],
            'validator': lambda x: x in ['confirmed', 'cancelled', 'rescheduled', 'completed']
        },
        'cancellation_reason': {
            'type': str,
            'max_length': 500,
            'validator': lambda x: len(str(x)) <= 500
        },
        'meeting_link': {
            'type': str,
            'validator': lambda x: x == '' or x.startswith(('http://', 'https://'))
        },
        'meeting_id': {
            'type': str,
            'max_length': 100,
            'validator': lambda x: len(str(x)) <= 100
        },
        'meeting_password': {
            'type': str,
            'max_length': 50,
            'validator': lambda x: len(str(x)) <= 50
        },
        'custom_answers': {
            'type': dict,
            'validator': lambda x: isinstance(x, dict)
        }
    }
    
    for field_name, new_value in update_fields.items():
        if field_name not in field_validators:
            logger.warning(f"Unknown field for booking update: {field_name}")
            continue
        
        validator_config = field_validators[field_name]
        
        try:
            # Type conversion
            if validator_config['type'] == str:
                converted_value = str(new_value) if new_value is not None else ''
            elif validator_config['type'] == dict:
                if isinstance(new_value, dict):
                    converted_value = new_value
                elif isinstance(new_value, str):
                    import json
                    converted_value = json.loads(new_value)
                else:
                    raise ValueError(f"Cannot convert {type(new_value)} to dict")
            else:
                converted_value = validator_config['type'](new_value)
            
            # Validation
            if 'validator' in validator_config:
                if not validator_config['validator'](converted_value):
                    raise ValueError(f"Validation failed for field {field_name}")
            
            validated_fields[field_name] = converted_value
            
        except Exception as e:
            logger.error(f"Error validating field {field_name} with value {new_value}: {str(e)}")
            # Skip invalid fields rather than failing the entire update
            continue
    
    return validated_fields


def get_workflow_execution_summary(execution):
    """
    Generate a human-readable summary of workflow execution.
    
    Args:
        execution: WorkflowExecution instance
    
    Returns:
        dict: Summary information
    """
    if not execution.execution_log:
        return {
            'summary': 'No execution log available',
            'total_actions': 0,
            'successful_actions': 0,
            'failed_actions': 0,
            'skipped_actions': 0
        }
    
    total_actions = len(execution.execution_log)
    successful_actions = sum(1 for log in execution.execution_log if log.get('status') == 'success')
    failed_actions = sum(1 for log in execution.execution_log if log.get('status') == 'failed')
    skipped_actions = sum(1 for log in execution.execution_log if log.get('status') == 'skipped_conditions')
    
    # Generate summary text
    if execution.status == 'completed' and failed_actions == 0:
        summary = f"Successfully executed all {total_actions} actions"
    elif execution.status == 'completed' and failed_actions > 0:
        summary = f"Completed with {failed_actions} failures out of {total_actions} actions"
    elif execution.status == 'failed':
        summary = f"Failed execution: {successful_actions} successful, {failed_actions} failed"
    else:
        summary = f"Execution {execution.status}: {successful_actions}/{total_actions} actions completed"
    
    if skipped_actions > 0:
        summary += f" ({skipped_actions} skipped due to conditions)"
    
    return {
        'summary': summary,
        'total_actions': total_actions,
        'successful_actions': successful_actions,
        'failed_actions': failed_actions,
        'skipped_actions': skipped_actions,
        'success_rate': round((successful_actions / total_actions * 100), 2) if total_actions > 0 else 0
    }


def validate_workflow_configuration(workflow):
    """
    Validate complete workflow configuration.
    
    Args:
        workflow: Workflow instance
    
    Returns:
        dict: Validation results with warnings and errors
    """
    warnings = []
    errors = []
    
    # Check if workflow has actions
    actions = workflow.actions.filter(is_active=True)
    if not actions.exists():
        warnings.append("Workflow has no active actions")
    
    # Validate each action
    for action in actions:
        try:
            action.full_clean()
        except ValidationError as e:
            errors.append(f"Action '{action.name}': {str(e)}")
        
        # Check action-specific configurations
        if action.action_type == 'send_email':
            if not action.subject and not action.message:
                warnings.append(f"Email action '{action.name}' has no subject or message")
            
            if action.recipient == 'custom' and not action.custom_email:
                errors.append(f"Email action '{action.name}' requires custom_email when recipient is 'custom'")
        
        elif action.action_type == 'send_sms':
            if not action.message:
                warnings.append(f"SMS action '{action.name}' has no message")
        
        elif action.action_type == 'webhook':
            if not action.webhook_url:
                errors.append(f"Webhook action '{action.name}' requires webhook_url")
            elif not action.webhook_url.startswith(('http://', 'https://')):
                errors.append(f"Webhook action '{action.name}' has invalid URL format")
        
        elif action.action_type == 'update_booking':
            if not action.update_booking_fields:
                warnings.append(f"Update booking action '{action.name}' has no fields to update")
    
    # Check for duplicate action orders
    orders = list(actions.values_list('order', flat=True))
    if len(orders) != len(set(orders)):
        warnings.append("Workflow has actions with duplicate order values")
    
    return {
        'valid': len(errors) == 0,
        'warnings': warnings,
        'errors': errors
    }


def create_test_context_data():
    """
    Create test context data for workflow testing.
    
    Returns:
        dict: Test context data
    """
    return {
        'booking_id': 'test-booking-123',
        'booking_status': 'confirmed',
        'invitee_name': 'John Doe',
        'invitee_email': 'john.doe@example.com',
        'invitee_phone': '+1234567890',
        'invitee_timezone': 'America/New_York',
        'attendee_count': 1,
        'start_time': timezone.now() + timezone.timedelta(hours=2),
        'end_time': timezone.now() + timezone.timedelta(hours=2, minutes=30),
        'duration': 30,
        'cancellation_reason': '',
        'meeting_link': 'https://zoom.us/j/test123456',
        'meeting_id': 'test123456',
        'meeting_password': 'testpass',
        'event_type_id': 'test-event-type-123',
        'event_type_name': 'Discovery Call',
        'event_type_slug': 'discovery-call',
        'event_type_description': 'Initial consultation meeting',
        'event_type_duration': 30,
        'event_type_location_type': 'video_call',
        'event_type_max_attendees': 1,
        'organizer_id': 'test-organizer-123',
        'organizer_email': 'organizer@example.com',
        'organizer_name': 'Jane Smith',
        'organizer_first_name': 'Jane',
        'organizer_last_name': 'Smith',
        'organizer_timezone': 'America/Los_Angeles',
        'organizer_company': 'Test Company',
        'booking_hour': 14,
        'booking_day_of_week': 1,  # Tuesday
        'booking_date': timezone.now().date(),
        'is_weekend': False,
        'is_business_hours': True,
        'invitee_domain': 'example.com',
        'has_phone': True,
        'has_meeting_link': True,
        'booking_created_today': True,
        'custom_company': 'Acme Corp',
        'custom_budget': '10000'
    }


def log_workflow_performance(workflow_id, execution_time, actions_count, success_count, failure_count):
    """
    Log workflow performance metrics for monitoring.
    
    Args:
        workflow_id: UUID of the workflow
        execution_time: Time taken to execute workflow (seconds)
        actions_count: Total number of actions
        success_count: Number of successful actions
        failure_count: Number of failed actions
    """
    logger.info(
        f"Workflow Performance - ID: {workflow_id}, "
        f"Execution Time: {execution_time:.3f}s, "
        f"Actions: {actions_count}, "
        f"Success: {success_count}, "
        f"Failures: {failure_count}, "
        f"Success Rate: {(success_count/actions_count*100):.1f}%"
    )
    
    # Log warning for slow workflows
    if execution_time > 30:  # 30 seconds threshold
        logger.warning(f"Slow workflow execution detected: {workflow_id} took {execution_time:.3f}s")
    
    # Log warning for high failure rate
    if actions_count > 0 and (failure_count / actions_count) > 0.5:
        logger.warning(f"High failure rate in workflow {workflow_id}: {failure_count}/{actions_count} actions failed")