from django.urls import path
from .views import chatbot_query

urlpatterns = [
    # Chatbot API endpoint
    path('chatbot/query/', chatbot_query),
]