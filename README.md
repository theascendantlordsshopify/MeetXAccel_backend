# Calendly Clone Backend

An enterprise-grade Django REST API backend for a scheduling application similar to Calendly.

## Features

- User authentication and profile management
- Event type creation and management
- Public booking URLs for unauthenticated users
- Advanced availability calculation with timezone support
- External calendar integrations (Google Calendar, Outlook)
- Video conferencing integrations (Zoom, Google Meet)
- Robust rate limiting and error handling for external APIs
- Conflict resolution between manual and synced calendar events
- Automatic token refresh and integration health monitoring
- Automated workflows and notifications
- Asynchronous task processing with Celery
- Redis caching for performance optimization

## Tech Stack

- **Backend**: Django 4.2 + Django REST Framework
- **Database**: PostgreSQL
- **Cache/Message Broker**: Redis
- **Task Queue**: Celery
- **Containerization**: Docker & Docker Compose

## Project Structure

```
calendly_clone/
├── config/                 # Django project configuration
│   ├── settings/          # Environment-specific settings
│   ├── celery.py          # Celery configuration
│   ├── urls.py            # Main URL dispatcher
│   ├── wsgi.py            # WSGI configuration
│   └── asgi.py            # ASGI configuration
├── apps/                  # Django applications
│   ├── users/             # User management
│   ├── events/            # Event types and bookings
│   ├── availability/      # Availability calculation
│   ├── integrations/      # External service integrations
│   ├── workflows/         # Automated workflows
│   ├── notifications/     # Email/SMS notifications
│   └── contacts/          # Contact management
├── requirements.txt       # Python dependencies
├── docker-compose.yml     # Docker services configuration
├── Dockerfile            # Docker image configuration
└── manage.py             # Django management script
```

## Setup Instructions

1. Clone the repository
2. Copy `.env.example` to `.env` and configure your environment variables
   - Set up Google OAuth credentials for calendar integration
   - Set up Microsoft Graph API credentials for Outlook integration  
   - Set up Zoom API credentials for video conferencing
   - Configure Twilio for SMS notifications
3. Install dependencies: `pip install -r requirements.txt`
4. Run migrations: `python manage.py migrate`
5. Create superuser: `python manage.py createsuperuser`
6. Start the development server: `python manage.py runserver`

## Running with Docker

```bash
docker-compose up --build
```

## Integration Management

### Calendar Sync
```bash
# Manually sync all calendars
python manage.py sync_all_calendars

# Check integration health
python manage.py check_integration_health

# Sync specific provider
python manage.py sync_all_calendars --provider google
```

### Monitoring
```bash
# View integration logs
python manage.py shell -c "from apps.integrations.models import IntegrationLog; print(IntegrationLog.objects.filter(success=False).count())"
```

## API Documentation

The API follows RESTful conventions and includes endpoints for:

- User authentication and management
- Event type CRUD operations
- Public booking endpoints
- Availability calculation
- Integration management
- Calendar synchronization and conflict resolution
- Video conferencing link generation
- Workflow automation

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests
5. Submit a pull request

## License

This project is licensed under the MIT License.