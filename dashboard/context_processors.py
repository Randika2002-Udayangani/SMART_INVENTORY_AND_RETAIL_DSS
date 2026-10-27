
from django.conf import settings


def staff_login_url(request):
    return {
        'STAFF_LOGIN_URL': '/' + settings.STAFF_LOGIN_PATH + '/'
    }
