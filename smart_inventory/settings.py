"""
Django settings for smart_inventory project.
"""
from pathlib import Path
import os
from datetime import timedelta
from dotenv import load_dotenv

# Load .env file
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


SECRET_KEY = os.getenv('SECRET_KEY')


DEBUG = os.getenv('DEBUG', 'False') == 'True'

# ─────────────────────────────────────────────────────────────────
# ALLOWED_HOSTS
# Local dev hosts are always included. Add your hosted domain(s)
# via the ALLOWED_HOSTS env var (comma-separated) once deployed,
# e.g. in Render's environment settings:
#     ALLOWED_HOSTS=smartinventory.onrender.com,www.yourdomain.com
# ─────────────────────────────────────────────────────────────────
ALLOWED_HOSTS = ['127.0.0.1', 'localhost']
_extra_hosts = os.getenv('ALLOWED_HOSTS', '')
if _extra_hosts:
    ALLOWED_HOSTS += [h.strip() for h in _extra_hosts.split(',') if h.strip()]


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',   # add this
    'corsheaders',
    # Project apps
    'core',
    'products',
    'suppliers',
    'inventory',
    'purchases',
    'sales',
    'users',

    'customer',

    'orders',
    'analytics',
    'dashboard',

]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'smart_inventory.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'smart_inventory.wsgi.application'


# ─────────────────────────────────────────────────────────────────
# Database — PostgreSQL
# All values loaded from .env file — never hardcode credentials.
# (Previously defined three times in this file — the last definition
#  silently won and was missing CONN_MAX_AGE. Consolidated to one.)
# ─────────────────────────────────────────────────────────────────
DATABASES = {
    'default': {
        'ENGINE'      : 'django.db.backends.postgresql',
        'NAME'        : os.getenv('DATABASE_NAME'),
        'USER'        : os.getenv('DATABASE_USER'),
        'PASSWORD'    : os.getenv('DATABASE_PASSWORD'),
        'HOST'        : os.getenv('DATABASE_HOST'),
        'PORT'        : os.getenv('DATABASE_PORT'),
        'CONN_MAX_AGE': 60,
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE     = 'UTC'
USE_I18N      = True
USE_TZ        = True

STATIC_URL  = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL  = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ─────────────────────────────────────────────────────────────────
# Django REST Framework
# Default: all endpoints require a valid JWT token
# Override per-view using permission_classes = [AllowAny]
# Required on: LoginView, RegisterView, CustomerLoginView
# ─────────────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
}

# ─────────────────────────────────────────────────────────────────
# SimpleJWT token lifetimes
# Access token:  8 hours  — staff work shift duration
# Refresh token: 1 day    — allows silent token refresh
# ─────────────────────────────────────────────────────────────────
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME' : timedelta(hours=8),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
}

# ─────────────────────────────────────────────────────────────────
# CORS
# CORS_ALLOW_ALL_ORIGINS was True (dev-only setting) — now restricted
# to explicit origins via env var. Set in .env, comma-separated:
#     CORS_ALLOWED_ORIGINS=http://localhost:5173,https://yourfrontend.com
# Falls back to localhost dev origins if the env var isn't set, so
# local development still works out of the box.
# ─────────────────────────────────────────────────────────────────
_cors_origins = os.getenv('CORS_ALLOWED_ORIGINS', '')
if _cors_origins:
    CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins.split(',') if o.strip()]
else:
    CORS_ALLOWED_ORIGINS = [
        'http://127.0.0.1:8000',
        'http://localhost:8000',
    ]