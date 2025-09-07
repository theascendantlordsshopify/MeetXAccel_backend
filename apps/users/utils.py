from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from .models import AuditLog


def get_client_ip(request):
    """Get client IP address from request."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def get_user_agent(request):
    """Get user agent from request."""
    return request.META.get('HTTP_USER_AGENT', '')


def create_audit_log(user, action, description, request=None, metadata=None, content_object=None):
    """Create an audit log entry."""
    audit_data = {
        'user': user,
        'action': action,
        'description': description or '',
        'metadata': metadata or {}
    }
    
    # Add related object if provided
    if content_object:
        audit_data['content_type'] = ContentType.objects.get_for_model(content_object)
        audit_data['object_id'] = content_object.pk
    
    if request:
        audit_data.update({
            'ip_address': get_client_ip(request),
            'user_agent': get_user_agent(request),
            'session_key': request.session.session_key if hasattr(request, 'session') else None
        })
    
    return AuditLog.objects.create(**audit_data)


def get_geolocation_from_ip(ip_address):
    """Get geolocation data from IP address."""
    # This is a placeholder function. In production, you would integrate
    # with a geolocation service like MaxMind GeoIP2, IPStack, or similar.
    # For now, return empty values
    return {
        'country': '',
        'city': ''
    }
    
    # Example implementation with a hypothetical service:
    # try:
    #     import requests
    #     response = requests.get(f'http://ip-api.com/json/{ip_address}', timeout=5)
    #     if response.status_code == 200:
    #         data = response.json()
    #         return {
    #             'country': data.get('country', ''),
    #             'city': data.get('city', '')
    #         }
    # except Exception:
    #     pass
    # return {'country': '', 'city': ''}
def parse_user_agent(user_agent):
    """Parse user agent string to extract device information."""
    # This is a simplified parser. In production, you might want to use
    # a library like user-agents or httpagentparser
    device_info = {
        'browser': 'Unknown',
        'os': 'Unknown',
        'device': 'Unknown'
    }
    
    if not user_agent:
        return device_info
    
    user_agent = user_agent.lower()
    
    # Browser detection
    if 'chrome' in user_agent:
        device_info['browser'] = 'Chrome'
    elif 'firefox' in user_agent:
        device_info['browser'] = 'Firefox'
    elif 'safari' in user_agent and 'chrome' not in user_agent:
        device_info['browser'] = 'Safari'
    elif 'edge' in user_agent:
        device_info['browser'] = 'Edge'
    
    # OS detection
    if 'windows' in user_agent:
        device_info['os'] = 'Windows'
    elif 'mac' in user_agent:
        device_info['os'] = 'macOS'
    elif 'linux' in user_agent:
        device_info['os'] = 'Linux'
    elif 'android' in user_agent:
        device_info['os'] = 'Android'
    elif 'iphone' in user_agent or 'ipad' in user_agent:
        device_info['os'] = 'iOS'
    
    # Device detection
    if 'mobile' in user_agent:
        device_info['device'] = 'Mobile'
    elif 'tablet' in user_agent or 'ipad' in user_agent:
        device_info['device'] = 'Tablet'
    else:
        device_info['device'] = 'Desktop'
    
    return device_info


def is_password_compromised(password):
    """Check if password appears in known breach databases."""
    # This is a placeholder. In production, you might want to integrate
    # with services like HaveIBeenPwned API or maintain your own
    # compromised password database
    
    common_passwords = [
        'password', '123456', 'password123', 'admin', 'qwerty',
        'letmein', 'welcome', 'monkey', '1234567890'
    ]
    
    return password.lower() in common_passwords


def generate_secure_token(length=32):
    """Generate a cryptographically secure random token."""
    import secrets
    return secrets.token_urlsafe(length)


def validate_phone_number(phone):
    """Validate phone number format."""
    import re
    
    # Remove all non-digit characters
    digits_only = re.sub(r'\D', '', phone)
    
    # Check if it's a valid length (10-15 digits)
    if len(digits_only) < 10 or len(digits_only) > 15:
        return False
    
    # Basic format validation
    phone_pattern = r'^\+?1?\d{9,15}$'
    return bool(re.match(phone_pattern, phone))


def mask_sensitive_data(data, fields_to_mask=None):
    """Mask sensitive data for logging purposes."""
    if fields_to_mask is None:
        fields_to_mask = ['password', 'token', 'secret', 'key']
    
    if isinstance(data, dict):
        masked_data = {}
        for key, value in data.items():
            if any(field in key.lower() for field in fields_to_mask):
                masked_data[key] = '***MASKED***'
            elif isinstance(value, (dict, list)):
                masked_data[key] = mask_sensitive_data(value, fields_to_mask)
            else:
                masked_data[key] = value
        return masked_data
    elif isinstance(data, list):
        return [mask_sensitive_data(item, fields_to_mask) for item in data]
    else:
        return data


def get_saml_config_dict(saml_config):
    """
    Convert SAMLConfiguration model instance to python3-saml config dict.
    """
    from django.conf import settings
    
    base_url = getattr(settings, 'SAML_BASE_URL', 'http://localhost:8000')
    
    return {
        'debug': settings.DEBUG,
        'sp': {
            'entityId': f"{base_url}/saml/metadata/",
            'assertionConsumerService': {
                'url': f"{base_url}/saml/acs/",
                'binding': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST'
            },
            'singleLogoutService': {
                'url': f"{base_url}/saml/sls/",
                'binding': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect'
            },
            'NameIDFormat': 'urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress',
            'x509cert': '',
            'privateKey': ''
        },
        'idp': {
            'entityId': saml_config.entity_id,
            'singleSignOnService': {
                'url': saml_config.sso_url,
                'binding': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect'
            },
            'singleLogoutService': {
                'url': saml_config.slo_url,
                'binding': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect'
            } if saml_config.slo_url else {},
            'x509cert': saml_config.x509_cert
        }
    }


def validate_saml_configuration(saml_config):
    """
    Validate SAML configuration for completeness.
    """
    errors = []
    
    if not saml_config.entity_id:
        errors.append("Entity ID is required")
    
    if not saml_config.sso_url:
        errors.append("SSO URL is required")
    
    if not saml_config.x509_cert:
        errors.append("X.509 certificate is required")
    
    if not saml_config.email_attribute:
        errors.append("Email attribute mapping is required")
    
    return errors


def validate_oidc_configuration(oidc_config):
    """
    Validate OIDC configuration for completeness.
    """
    errors = []
    
    if not oidc_config.issuer:
        errors.append("Issuer URL is required")
    
    if not oidc_config.client_id:
        errors.append("Client ID is required")
    
    if not oidc_config.client_secret:
        errors.append("Client Secret is required")
    
    if not oidc_config.email_claim:
        errors.append("Email claim mapping is required")
    
    return errors