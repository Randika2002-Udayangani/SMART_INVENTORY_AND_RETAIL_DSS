from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError


def validate_customer_password(password):
    """Return None if the password is acceptable, otherwise ONE error string."""
    password = password or ''

    if len(password) < 8:
        return 'Password must be at least 8 characters.'

    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return 'Password must contain at least one letter and one number.'

    try:
        validate_password(password)   # NO user argument
    except ValidationError as exc:
        return ' '.join(exc.messages)

    return None