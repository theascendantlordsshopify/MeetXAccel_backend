"""
Custom authentication backends for SSO integration.
"""
import logging
from django.contrib.auth import get_user_model
from django.utils import timezone
from djangosaml2.backends import Saml2Backend
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from .models import SAMLConfiguration, OIDCConfiguration, SSOSession, Role
from .utils import create_audit_log

logger = logging.getLogger(__name__)
User = get_user_model()


class CustomSAMLBackend(Saml2Backend):
    """
    Custom SAML authentication backend with JIT user provisioning.
    """
    
    def authenticate(self, request, session_info=None, attribute_mapping=None, create_unknown_user=True, **kwargs):
        """
        Authenticate user via SAML assertion with JIT provisioning.
        """
        if session_info is None:
            logger.debug("No SAML session info provided")
            return None
        
        try:
            # Extract attributes from SAML assertion
            attributes = session_info.get('ava', {})
            if not attributes:
                logger.error("No attributes found in SAML assertion")
                return None
            
            # Get SAML configuration based on issuer
            issuer = session_info.get('issuer')
            if not issuer:
                logger.error("No issuer found in SAML assertion")
                return None
            
            # Find matching SAML configuration
            saml_config = self._get_saml_config_by_issuer(issuer)
            if not saml_config:
                logger.error(f"No SAML configuration found for issuer: {issuer}")
                return None
            
            # Extract user information using attribute mapping
            user_info = self._extract_user_info(attributes, saml_config)
            if not user_info.get('email'):
                logger.error("No email found in SAML assertion")
                return None
            
            # Get or create user
            user = self._get_or_create_user(user_info, saml_config, create_unknown_user)
            if not user:
                return None
            
            # Create SSO session record
            self._create_sso_session(user, saml_config, session_info, request)
            
            # Create audit log
            create_audit_log(
                user=user,
                action='saml_login',
                description=f"User logged in via SAML from {saml_config.organization_name}",
                request=request,
                metadata={
                    'saml_config_id': str(saml_config.id),
                    'issuer': issuer,
                    'organization': saml_config.organization_name
                }
            )
            
            return user
            
        except Exception as e:
            logger.error(f"SAML authentication error: {str(e)}")
            return None
    
    def _get_saml_config_by_issuer(self, issuer):
        """Get SAML configuration by issuer/entity ID."""
        try:
            return SAMLConfiguration.objects.get(
                entity_id=issuer,
                is_active=True
            )
        except SAMLConfiguration.DoesNotExist:
            return None
    
    def _extract_user_info(self, attributes, saml_config):
        """Extract user information from SAML attributes."""
        def get_attribute_value(attr_name, default=''):
            """Get first value from SAML attribute list."""
            values = attributes.get(attr_name, [])
            return values[0] if values else default
        
        return {
            'email': get_attribute_value(saml_config.email_attribute),
            'first_name': get_attribute_value(saml_config.first_name_attribute),
            'last_name': get_attribute_value(saml_config.last_name_attribute),
            'role': get_attribute_value(saml_config.role_attribute) if saml_config.role_attribute else None,
        }
    
    def _get_or_create_user(self, user_info, saml_config, create_unknown_user):
        """Get existing user or create new one via JIT provisioning."""
        email = user_info['email'].lower()
        
        try:
            # Try to get existing user
            user = User.objects.get(email=email)
            
            # Update user information if needed
            updated = False
            if user_info['first_name'] and not user.first_name:
                user.first_name = user_info['first_name']
                updated = True
            if user_info['last_name'] and not user.last_name:
                user.last_name = user_info['last_name']
                updated = True
            
            if updated:
                user.save()
            
            return user
            
        except User.DoesNotExist:
            if not create_unknown_user or not saml_config.auto_provision_users:
                logger.info(f"User {email} not found and auto-provisioning disabled")
                return None
            
            # Create new user via JIT provisioning
            user = User.objects.create_user(
                username=email,
                email=email,
                first_name=user_info['first_name'] or '',
                last_name=user_info['last_name'] or '',
                is_email_verified=True,  # Trust SAML assertion
                account_status='active',
                is_organizer=True
            )
            
            # Assign default role
            if saml_config.default_role:
                user.roles.add(saml_config.default_role)
            
            logger.info(f"Created new user via SAML JIT provisioning: {email}")
            return user
    
    def _create_sso_session(self, user, saml_config, session_info, request):
        """Create SSO session record."""
        try:
            session_key = request.session.session_key if request and hasattr(request, 'session') else None
            ip_address = request.META.get('REMOTE_ADDR') if request else None
            user_agent = request.META.get('HTTP_USER_AGENT', '') if request else ''
            
            SSOSession.objects.create(
                user=user,
                sso_type='saml',
                provider_name=saml_config.organization_name,
                external_session_id=session_info.get('session_index', ''),
                session_key=session_key or '',
                ip_address=ip_address or '127.0.0.1',
                user_agent=user_agent,
                expires_at=timezone.now() + timezone.timedelta(hours=8),
                is_active=True
            )
        except Exception as e:
            logger.error(f"Failed to create SSO session: {str(e)}")


class CustomOIDCBackend(OIDCAuthenticationBackend):
    """
    Custom OIDC authentication backend with JIT user provisioning.
    """
    
    def authenticate(self, request, **kwargs):
        """
        Authenticate user via OIDC with JIT provisioning.
        """
        # Get the organization domain from session or request
        organization_domain = self._get_organization_domain(request)
        if not organization_domain:
            logger.error("No organization domain found for OIDC authentication")
            return None
        
        # Get OIDC configuration
        oidc_config = self._get_oidc_config(organization_domain)
        if not oidc_config:
            logger.error(f"No OIDC configuration found for domain: {organization_domain}")
            return None
        
        # Set dynamic OIDC settings
        self._configure_oidc_settings(oidc_config)
        
        # Call parent authenticate method
        user = super().authenticate(request, **kwargs)
        
        if user:
            # Create SSO session record
            self._create_sso_session(user, oidc_config, request)
            
            # Create audit log
            create_audit_log(
                user=user,
                action='oidc_login',
                description=f"User logged in via OIDC from {oidc_config.organization_name}",
                request=request,
                metadata={
                    'oidc_config_id': str(oidc_config.id),
                    'issuer': oidc_config.issuer,
                    'organization': oidc_config.organization_name
                }
            )
        
        return user
    
    def create_user(self, claims):
        """
        Create user from OIDC claims with JIT provisioning.
        """
        # Get OIDC configuration from thread-local storage or session
        oidc_config = getattr(self, '_current_oidc_config', None)
        if not oidc_config:
            logger.error("No OIDC configuration available for user creation")
            return None
        
        if not oidc_config.auto_provision_users:
            logger.info("Auto-provisioning disabled for OIDC configuration")
            return None
        
        # Extract user information from claims
        email = claims.get(oidc_config.email_claim, '').lower()
        if not email:
            logger.error("No email found in OIDC claims")
            return None
        
        first_name = claims.get(oidc_config.first_name_claim, '')
        last_name = claims.get(oidc_config.last_name_claim, '')
        
        # Create new user
        user = User.objects.create_user(
            username=email,
            email=email,
            first_name=first_name,
            last_name=last_name,
            is_email_verified=True,  # Trust OIDC claims
            account_status='active',
            is_organizer=True
        )
        
        # Assign default role
        if oidc_config.default_role:
            user.roles.add(oidc_config.default_role)
        
        logger.info(f"Created new user via OIDC JIT provisioning: {email}")
        return user
    
    def update_user(self, user, claims):
        """
        Update user information from OIDC claims.
        """
        oidc_config = getattr(self, '_current_oidc_config', None)
        if not oidc_config:
            return user
        
        # Update user information
        updated = False
        
        first_name = claims.get(oidc_config.first_name_claim, '')
        if first_name and not user.first_name:
            user.first_name = first_name
            updated = True
        
        last_name = claims.get(oidc_config.last_name_claim, '')
        if last_name and not user.last_name:
            user.last_name = last_name
            updated = True
        
        if updated:
            user.save()
        
        return user
    
    def _get_organization_domain(self, request):
        """Extract organization domain from request."""
        if request and hasattr(request, 'session'):
            return request.session.get('oidc_organization_domain')
        return None
    
    def _get_oidc_config(self, organization_domain):
        """Get OIDC configuration by organization domain."""
        try:
            return OIDCConfiguration.objects.get(
                organization_domain=organization_domain,
                is_active=True
            )
        except OIDCConfiguration.DoesNotExist:
            return None
    
    def _configure_oidc_settings(self, oidc_config):
        """Configure OIDC settings dynamically."""
        from django.conf import settings
        
        # Store config for later use
        self._current_oidc_config = oidc_config
        
        # Set dynamic OIDC settings
        settings.OIDC_RP_CLIENT_ID = oidc_config.client_id
        settings.OIDC_RP_CLIENT_SECRET = oidc_config.client_secret
        settings.OIDC_OP_AUTHORIZATION_ENDPOINT = oidc_config.authorization_endpoint or f"{oidc_config.issuer}/auth"
        settings.OIDC_OP_TOKEN_ENDPOINT = oidc_config.token_endpoint or f"{oidc_config.issuer}/token"
        settings.OIDC_OP_USER_ENDPOINT = oidc_config.userinfo_endpoint or f"{oidc_config.issuer}/userinfo"
        settings.OIDC_OP_JWKS_ENDPOINT = oidc_config.jwks_uri or f"{oidc_config.issuer}/.well-known/jwks.json"
    
    def _create_sso_session(self, user, oidc_config, request):
        """Create SSO session record."""
        try:
            session_key = request.session.session_key if request and hasattr(request, 'session') else None
            ip_address = request.META.get('REMOTE_ADDR') if request else None
            user_agent = request.META.get('HTTP_USER_AGENT', '') if request else ''
            
            SSOSession.objects.create(
                user=user,
                sso_type='oidc',
                provider_name=oidc_config.organization_name,
                external_session_id='',  # OIDC doesn't typically provide session IDs
                session_key=session_key or '',
                ip_address=ip_address or '127.0.0.1',
                user_agent=user_agent,
                expires_at=timezone.now() + timezone.timedelta(hours=8),
                is_active=True
            )
        except Exception as e:
            logger.error(f"Failed to create OIDC SSO session: {str(e)}")