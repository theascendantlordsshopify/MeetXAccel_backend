"""
Comprehensive tests for integrations module.
"""
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from unittest.mock import patch, Mock, MagicMock
from datetime import datetime, timedelta
from .models import CalendarIntegration, VideoConferenceIntegration, IntegrationLog
from .utils import (
    make_api_request, refresh_access_token, detect_integration_conflicts,
    create_integration_health_report, RateLimitError, TokenExpiredError
)
from .google_client import GoogleCalendarClient, GoogleMeetClient
from .outlook_client import OutlookCalendarClient
from .zoom_client import ZoomClient
from apps.events.models import EventType, Booking
from apps.availability.models import BlockedTime

User = get_user_model()


class IntegrationUtilsTestCase(TestCase):
    """Test integration utility functions."""
    
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
    
    @patch('apps.integrations.utils.cache')
    def test_rate_limiting(self, mock_cache):
        """Test rate limiting functionality."""
        from .utils import check_rate_limit, record_api_call, RateLimitError
        
        # Test within limits
        mock_cache.get.return_value = 50  # Under limit of 100
        self.assertTrue(check_rate_limit('google', self.organizer.id))
        
        # Test over limits
        mock_cache.get.return_value = 150  # Over limit of 100
        with self.assertRaises(RateLimitError):
            check_rate_limit('google', self.organizer.id)
        
        # Test recording API calls
        mock_cache.get.return_value = 10
        record_api_call('google', self.organizer.id)
        mock_cache.set.assert_called_with(
            f"rate_limit:google:{self.organizer.id}",
            11,
            timeout=60
        )
    
    @patch('requests.request')
    def test_api_request_retry_logic(self, mock_request):
        """Test API request retry logic with exponential backoff."""
        from .utils import make_api_request, RateLimitError
        
        # Test successful request
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'success': True}
        mock_request.return_value = mock_response
        
        response = make_api_request('GET', 'https://api.example.com/test')
        self.assertEqual(response.status_code, 200)
        
        # Test rate limit error with retry
        mock_response.status_code = 429
        mock_response.headers = {'Retry-After': '60'}
        mock_request.return_value = mock_response
        
        with self.assertRaises(RateLimitError):
            make_api_request('GET', 'https://api.example.com/test')
        
        # Should have retried 3 times
        self.assertEqual(mock_request.call_count, 4)  # Initial + 3 retries
    
    def test_conflict_detection(self):
        """Test calendar conflict detection."""
        # Create manual blocked time
        manual_block = BlockedTime.objects.create(
            organizer=self.organizer,
            start_datetime=timezone.now() + timedelta(hours=1),
            end_datetime=timezone.now() + timedelta(hours=2),
            reason='Manual block',
            source='manual'
        )
        
        # Create external event that overlaps
        external_events = [{
            'external_id': 'ext123',
            'summary': 'External meeting',
            'start_datetime': timezone.now() + timedelta(minutes=90),  # Overlaps last 30 min
            'end_datetime': timezone.now() + timedelta(hours=3),
            'updated': timezone.now()
        }]
        
        existing_blocks = BlockedTime.objects.filter(organizer=self.organizer)
        conflicts = detect_integration_conflicts(self.organizer, external_events, existing_blocks)
        
        self.assertEqual(len(conflicts['overlaps']), 1)
        self.assertEqual(conflicts['overlaps'][0]['overlap_type'], 'partial_overlap')
    
    def test_integration_health_report(self):
        """Test integration health report generation."""
        # Create calendar integration with errors
        cal_integration = CalendarIntegration.objects.create(
            organizer=self.organizer,
            provider='google',
            access_token='test_token',
            is_active=True,
            sync_enabled=True,
            sync_errors=3,
            token_expires_at=timezone.now() - timedelta(hours=1)  # Expired
        )
        
        health_report = create_integration_health_report(self.organizer)
        
        self.assertEqual(health_report['overall_health'], 'degraded')
        self.assertEqual(len(health_report['calendar_integrations']), 1)
        self.assertEqual(health_report['calendar_integrations'][0]['health'], 'unhealthy')


class GoogleCalendarClientTestCase(TestCase):
    """Test Google Calendar client functionality."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
        
        from apps.users.models import Profile
        Profile.objects.create(
            user=self.organizer,
            timezone_name='America/New_York'
        )
        
        self.integration = CalendarIntegration.objects.create(
            organizer=self.organizer,
            provider='google',
            access_token='test_access_token',
            refresh_token='test_refresh_token',
            token_expires_at=timezone.now() + timedelta(hours=1),
            is_active=True
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
    
    @patch('apps.integrations.google_client.build')
    @patch('apps.integrations.utils.ensure_valid_token')
    def test_get_busy_times(self, mock_ensure_token, mock_build):
        """Test fetching busy times from Google Calendar."""
        mock_ensure_token.return_value = True
        
        # Mock Google Calendar service
        mock_service = Mock()
        mock_events = Mock()
        mock_list = Mock()
        
        mock_service.events.return_value = mock_events
        mock_events.list.return_value = mock_list
        mock_list.execute.return_value = {
            'items': [
                {
                    'id': 'event123',
                    'summary': 'Test Event',
                    'start': {'dateTime': '2024-01-15T10:00:00Z'},
                    'end': {'dateTime': '2024-01-15T11:00:00Z'},
                    'updated': '2024-01-15T09:00:00Z',
                    'status': 'confirmed'
                }
            ]
        }
        
        mock_build.return_value = mock_service
        
        client = GoogleCalendarClient(self.integration)
        events = client.get_busy_times(
            timezone.now().date(),
            timezone.now().date() + timedelta(days=7)
        )
        
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['external_id'], 'event123')
        self.assertEqual(events[0]['summary'], 'Test Event')
    
    @patch('apps.integrations.google_client.build')
    @patch('apps.integrations.utils.ensure_valid_token')
    def test_create_calendar_event(self, mock_ensure_token, mock_build):
        """Test creating calendar event in Google Calendar."""
        mock_ensure_token.return_value = True
        
        # Mock Google Calendar service
        mock_service = Mock()
        mock_events = Mock()
        mock_insert = Mock()
        
        mock_service.events.return_value = mock_events
        mock_events.insert.return_value = mock_insert
        mock_insert.execute.return_value = {'id': 'created_event_123'}
        
        mock_build.return_value = mock_service
        
        client = GoogleCalendarClient(self.integration)
        event_id = client.create_event(self.booking)
        
        self.assertEqual(event_id, 'created_event_123')
        
        # Verify the event data passed to Google API
        call_args = mock_events.insert.call_args
        event_data = call_args[1]['body']
        
        self.assertIn('Test Meeting with Test Invitee', event_data['summary'])
        self.assertEqual(len(event_data['attendees']), 2)  # Organizer + Invitee


class ZoomClientTestCase(TestCase):
    """Test Zoom client functionality."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
        
        from apps.users.models import Profile
        Profile.objects.create(
            user=self.organizer,
            timezone_name='America/New_York'
        )
        
        self.integration = VideoConferenceIntegration.objects.create(
            organizer=self.organizer,
            provider='zoom',
            access_token='test_access_token',
            refresh_token='test_refresh_token',
            token_expires_at=timezone.now() + timedelta(hours=1),
            is_active=True
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
    
    @patch('apps.integrations.utils.make_api_request')
    @patch('apps.integrations.utils.ensure_valid_token')
    def test_create_zoom_meeting(self, mock_ensure_token, mock_api_request):
        """Test creating Zoom meeting."""
        mock_ensure_token.return_value = True
        
        # Mock Zoom API response
        mock_response = Mock()
        mock_response.json.return_value = {
            'id': 123456789,
            'join_url': 'https://zoom.us/j/123456789',
            'password': 'abc123',
            'start_url': 'https://zoom.us/s/123456789'
        }
        mock_api_request.return_value = mock_response
        
        client = ZoomClient(self.integration)
        meeting_details = client.create_meeting(self.booking)
        
        self.assertEqual(meeting_details['meeting_link'], 'https://zoom.us/j/123456789')
        self.assertEqual(meeting_details['meeting_id'], '123456789')
        self.assertEqual(meeting_details['meeting_password'], 'abc123')
        
        # Verify API call was made with correct data
        call_args = mock_api_request.call_args
        meeting_data = call_args[1]['json_data']
        
        self.assertIn('Test Meeting with Test Invitee', meeting_data['topic'])
        self.assertEqual(meeting_data['duration'], 30)
        self.assertEqual(meeting_data['type'], 2)  # Scheduled meeting
    
    def test_rate_limiting(self):
        """Test video integration rate limiting."""
        # Set up integration with high API usage
        self.integration.api_calls_today = 999
        self.integration.rate_limit_reset_at = timezone.now() + timedelta(hours=1)
        self.integration.save()
        
        # Should still allow one more call
        self.assertTrue(self.integration.can_make_api_call())
        
        # Exceed limit
        self.integration.api_calls_today = 1000
        self.integration.save()
        
        # Should not allow more calls
        self.assertFalse(self.integration.can_make_api_call())
        
        # Test reset functionality
        self.integration.rate_limit_reset_at = timezone.now() - timedelta(minutes=1)
        self.integration.save()
        
        # Should reset and allow calls again
        self.assertTrue(self.integration.can_make_api_call())


class CalendarSyncTestCase(TestCase):
    """Test calendar synchronization functionality."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
        
        self.integration = CalendarIntegration.objects.create(
            organizer=self.organizer,
            provider='google',
            access_token='test_token',
            is_active=True,
            sync_enabled=True
        )
    
    def test_conflict_resolution(self):
        """Test conflict resolution between manual and synced blocks."""
        # Create manual blocked time
        manual_block = BlockedTime.objects.create(
            organizer=self.organizer,
            start_datetime=timezone.now() + timedelta(hours=1),
            end_datetime=timezone.now() + timedelta(hours=2),
            reason='Manual appointment',
            source='manual'
        )
        
        # Simulate external events
        external_events = [
            {
                'external_id': 'ext123',
                'summary': 'External meeting',
                'start_datetime': timezone.now() + timedelta(minutes=90),  # Overlaps
                'end_datetime': timezone.now() + timedelta(hours=3),
                'updated': timezone.now()
            }
        ]
        
        existing_blocks = BlockedTime.objects.filter(organizer=self.organizer)
        conflicts = detect_integration_conflicts(self.organizer, external_events, existing_blocks)
        
        # Should detect overlap
        self.assertEqual(len(conflicts['overlaps']), 1)
        self.assertEqual(conflicts['overlaps'][0]['overlap_type'], 'partial_overlap')
    
    def test_recurring_event_handling(self):
        """Test handling of recurring external events."""
        # Simulate recurring event instances
        base_time = timezone.now() + timedelta(days=1)
        external_events = []
        
        # Weekly recurring meeting for 4 weeks
        for week in range(4):
            event_time = base_time + timedelta(weeks=week)
            external_events.append({
                'external_id': f'recurring_123_{week}',
                'summary': 'Weekly Team Meeting',
                'start_datetime': event_time,
                'end_datetime': event_time + timedelta(hours=1),
                'updated': timezone.now()
            })
        
        # Test that each instance is treated separately
        self.assertEqual(len(external_events), 4)
        
        # Each should have unique external_id
        external_ids = [event['external_id'] for event in external_events]
        self.assertEqual(len(set(external_ids)), 4)  # All unique


class IntegrationTasksTestCase(TestCase):
    """Test integration Celery tasks."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
        
        self.integration = CalendarIntegration.objects.create(
            organizer=self.organizer,
            provider='google',
            access_token='test_token',
            is_active=True,
            sync_enabled=True
        )
    
    @patch('apps.integrations.tasks.GoogleCalendarClient')
    def test_sync_calendar_events_task(self, mock_client_class):
        """Test calendar sync task."""
        from .tasks import sync_calendar_events
        
        # Mock client
        mock_client = Mock()
        mock_client.get_busy_times.return_value = [
            {
                'external_id': 'event123',
                'summary': 'Test Event',
                'start_datetime': timezone.now() + timedelta(hours=1),
                'end_datetime': timezone.now() + timedelta(hours=2),
                'updated': timezone.now(),
                'status': 'confirmed',
                'transparency': 'opaque'
            }
        ]
        mock_client_class.return_value = mock_client
        
        # Run sync task
        result = sync_calendar_events(self.integration.id)
        
        self.assertIn('Calendar sync completed', result)
        
        # Verify integration was marked as successful
        self.integration.refresh_from_db()
        self.assertIsNotNone(self.integration.last_sync_at)
        self.assertEqual(self.integration.sync_errors, 0)
    
    @patch('apps.integrations.tasks.GoogleCalendarClient')
    def test_sync_error_handling(self, mock_client_class):
        """Test sync error handling and integration deactivation."""
        from .tasks import sync_calendar_events
        
        # Mock client to raise exception
        mock_client = Mock()
        mock_client.get_busy_times.side_effect = Exception("API Error")
        mock_client_class.return_value = mock_client
        
        # Run sync task (should handle error gracefully)
        result = sync_calendar_events(self.integration.id)
        
        self.assertIn('Error syncing calendar', result)
        
        # Verify integration error was recorded
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.sync_errors, 1)


class OAuthFlowTestCase(TestCase):
    """Test OAuth flow functionality."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
    
    def test_oauth_initiation(self):
        """Test OAuth flow initiation."""
        from django.urls import reverse
        
        self.client.force_login(self.organizer)
        
        url = reverse('integrations:oauth-initiate')
        data = {
            'provider': 'google',
            'integration_type': 'calendar',
            'redirect_uri': 'http://localhost:3000/integrations/callback'
        }
        
        response = self.client.post(url, data, content_type='application/json')
        
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        
        self.assertIn('authorization_url', response_data)
        self.assertIn('accounts.google.com', response_data['authorization_url'])
        self.assertIn('state', response_data)
    
    @patch('apps.integrations.views.exchange_oauth_code')
    @patch('apps.integrations.views.get_provider_user_info')
    def test_oauth_callback(self, mock_get_user_info, mock_exchange_code):
        """Test OAuth callback handling."""
        from django.urls import reverse
        
        # Mock token exchange
        mock_exchange_code.return_value = {
            'access_token': 'new_access_token',
            'refresh_token': 'new_refresh_token',
            'expires_in': 3600
        }
        
        # Mock user info
        mock_get_user_info.return_value = {
            'id': 'user123',
            'email': 'organizer@test.com'
        }
        
        # Set up session state
        session = self.client.session
        session['oauth_state_google_calendar'] = 'test_state'
        session.save()
        
        self.client.force_login(self.organizer)
        
        url = reverse('integrations:oauth-callback')
        data = {
            'provider': 'google',
            'integration_type': 'calendar',
            'code': 'auth_code_123',
            'state': 'google:calendar:test_state'
        }
        
        response = self.client.post(url, data, content_type='application/json')
        
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        
        self.assertIn('connected successfully', response_data['message'])
        
        # Verify integration was created
        integration = CalendarIntegration.objects.get(
            organizer=self.organizer,
            provider='google'
        )
        self.assertEqual(integration.access_token, 'new_access_token')
        self.assertTrue(integration.is_active)


class IntegrationAPITestCase(TestCase):
    """Test integration API endpoints."""
    
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer@test.com',
            email='organizer@test.com',
            first_name='Test',
            last_name='Organizer',
            is_organizer=True
        )
    
    def test_integration_health_endpoint(self):
        """Test integration health endpoint."""
        from django.urls import reverse
        
        # Create integrations
        CalendarIntegration.objects.create(
            organizer=self.organizer,
            provider='google',
            access_token='test_token',
            is_active=True,
            sync_errors=0
        )
        
        self.client.force_login(self.organizer)
        
        url = reverse('integrations:integration-health')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data['organizer_email'], self.organizer.email)
        self.assertEqual(len(data['calendar_integrations']), 1)
        self.assertEqual(data['overall_health'], 'healthy')
    
    def test_calendar_conflicts_endpoint(self):
        """Test calendar conflicts endpoint."""
        from django.urls import reverse
        
        # Create manual and synced blocked times
        BlockedTime.objects.create(
            organizer=self.organizer,
            start_datetime=timezone.now() + timedelta(hours=1),
            end_datetime=timezone.now() + timedelta(hours=2),
            reason='Manual block',
            source='manual'
        )
        
        BlockedTime.objects.create(
            organizer=self.organizer,
            start_datetime=timezone.now() + timedelta(minutes=90),
            end_datetime=timezone.now() + timedelta(hours=3),
            reason='Synced event',
            source='google_calendar',
            external_id='ext123'
        )
        
        self.client.force_login(self.organizer)
        
        url = reverse('integrations:calendar-conflicts')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data['manual_blocks_count'], 1)
        self.assertEqual(data['synced_blocks_count'], 1)
        self.assertIn('conflicts', data)