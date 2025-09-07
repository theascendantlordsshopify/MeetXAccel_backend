from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
import re


class CustomPasswordValidator:
    """
    Custom password validator that enforces:
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    - No common patterns
    """
    
    def validate(self, password, user=None):
        if not re.search(r'[A-Z]', password):
            raise ValidationError(
                _("Password must contain at least one uppercase letter."),
                code='password_no_upper',
            )
        
        if not re.search(r'[a-z]', password):
            raise ValidationError(
                _("Password must contain at least one lowercase letter."),
                code='password_no_lower',
            )
        
        if not re.search(r'\d', password):
            raise ValidationError(
                _("Password must contain at least one digit."),
                code='password_no_digit',
            )
        
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            raise ValidationError(
                _("Password must contain at least one special character."),
                code='password_no_special',
            )
        
        # Check for common patterns
        common_patterns = [
            r'123',
            r'abc',
            r'qwerty',
            r'password',
            r'admin',
        ]
        
        for pattern in common_patterns:
            if re.search(pattern, password.lower()):
                raise ValidationError(
                    _("Password contains common patterns that are not allowed."),
                    code='password_common_pattern',
                )
        
        # Check if password is too similar to user information
        if user:
            user_info = [
                user.first_name.lower() if user.first_name else '',
                user.last_name.lower() if user.last_name else '',
                user.email.split('@')[0].lower() if user.email else '',
            ]
            
            for info in user_info:
                if info and len(info) > 2 and info in password.lower():
                    raise ValidationError(
                        _("Password is too similar to your personal information."),
                        code='password_too_similar',
                    )
    
    def get_help_text(self):
        return _(
            "Your password must contain at least one uppercase letter, "
            "one lowercase letter, one digit, and one special character. "
            "It should not contain common patterns or be similar to your personal information."
        )