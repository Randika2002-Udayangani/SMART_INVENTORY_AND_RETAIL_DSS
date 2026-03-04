from rest_framework_simplejwt.tokens import RefreshToken


def get_tokens_for_customer(customer):
    """
    Generate JWT access + refresh tokens for a Customer.
    Same token format as staff tokens — no conflict.
    """
    refresh = RefreshToken()
    refresh['customer_id'] = customer.id
    refresh['email'] = customer.email
    refresh['name'] = customer.name
    refresh['type'] = 'customer'

    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }