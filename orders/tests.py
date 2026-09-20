from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate
from unittest.mock import patch
import os
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import TestCase
from django.test import override_settings
from django.utils import timezone

from orders.models import ChatbotLog, ChatbotRateLimit, Customer
from orders.services.agent import (
    AgentUnavailable, execute_tool, find_cheapest_product, get_expiring_products, run_agent,
)
from orders.services.gemini import DEFAULT_GEMINI_MODEL, GeminiProvider, GeminiQuotaExceeded
from orders.views import ChatbotQueryView, ChatbotSessionDetailView
from products.models import Product
from purchases.models import Purchase, PurchaseBatch
from suppliers.models import Supplier


class ChatbotAgentTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(name='Customer One', email='one@example.com', password_hash='unused')
        self.other_customer = Customer.objects.create(name='Customer Two', email='two@example.com', password_hash='unused')
        self.factory = APIRequestFactory()
        self.url = reverse('chatbot')
        self.product = Product.objects.create(product_name='Milk Budget 1L', unit_price=250, cost_price=200, is_active=True)

    def post_chat(self, data, user=None):
        request = self.factory.post(self.url, data, format='json')
        if user is not None:
            force_authenticate(request, user=user)
        return ChatbotQueryView.as_view()(request)

    def test_unauthenticated_chatbot_request_is_rejected(self):
        self.assertEqual(self.post_chat({'message': 'cheapest milk'}).status_code, 401)

    @patch('orders.views.run_agent')
    def test_authenticated_request_uses_authenticated_customer_not_body_id(self, run_agent):
        run_agent.return_value = {'bot_response': 'Current result', 'query_success': True, 'tool_calls': []}
        response = self.post_chat({'message': 'cheapest milk', 'customer_id': self.other_customer.id}, self.customer)
        self.assertEqual(response.status_code, 200)
        log = ChatbotLog.objects.get()
        self.assertEqual(log.customer_id, self.customer.id)
        self.assertEqual(response.data['bot_response'], 'Current result')

    @override_settings(CHATBOT_RATE_LIMIT=2, CHATBOT_RATE_WINDOW_SECONDS=60)
    @patch('orders.views.run_agent')
    def test_chatbot_rate_limit_returns_429_and_resets_after_window(self, run_agent):
        run_agent.return_value = {'bot_response': 'Current result', 'query_success': True, 'tool_calls': []}
        self.assertEqual(self.post_chat({'message': 'milk'}, self.customer).status_code, 200)
        self.assertEqual(self.post_chat({'message': 'milk'}, self.customer).status_code, 200)
        blocked = self.post_chat({'message': 'milk'}, self.customer)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('limit reached', blocked.data['error'])
        self.assertIn('Retry-After', blocked.headers)
        self.assertEqual(run_agent.call_count, 2)

        counter = ChatbotRateLimit.objects.get(actor_key=f'customer:{self.customer.pk}')
        counter.window_started_at = timezone.now() - timedelta(seconds=61)
        counter.save(update_fields=['window_started_at'])
        self.assertEqual(self.post_chat({'message': 'milk'}, self.customer).status_code, 200)
        self.assertEqual(run_agent.call_count, 3)

    @patch('orders.views.run_agent')
    def test_customer_cannot_read_another_customers_logs(self, run_agent):
        run_agent.return_value = {'bot_response': 'Current result', 'query_success': True, 'tool_calls': []}
        response = self.post_chat({'message': 'milk', 'session_id': 'private'}, self.customer)
        self.assertEqual(response.status_code, 200)
        request = self.factory.get(reverse('chatbot-session-detail', args=['private']))
        force_authenticate(request, user=self.other_customer)
        self.assertEqual(ChatbotSessionDetailView.as_view()(request, session_id='private').status_code, 404)

    def test_cheapest_product_comes_from_real_product_data(self):
        Product.objects.create(product_name='Milk Premium 1L', unit_price=300, cost_price=220, is_active=True)
        with patch('orders.services.agent.get_available_stock', return_value=5):
            result = find_cheapest_product({'query': 'milk', 'available_only': True}, self.customer)
        self.assertEqual(result['product']['id'], self.product.id)
        self.assertEqual(result['product']['selling_price'], 250.0)

    def _batch(self, product, days_ahead, status='ACTIVE', remaining=5):
        """Create a real purchase batch expiring N days from today."""
        supplier = Supplier.objects.create(supplier_name='Test Supplier')
        purchase = Purchase.objects.create(
            supplier=supplier, purchase_date=date.today() - timedelta(days=20)
        )
        return PurchaseBatch.objects.create(
            purchase=purchase, product=product, quantity_received=10,
            cost_price=100, remaining_quantity=remaining,
            expiry_date=date.today() + timedelta(days=days_ahead), status=status,
        )

    def test_customer_can_execute_expiry_tool_with_real_data(self):
        self._batch(self.product, days_ahead=10)
        result, tool_status = execute_tool('get_expiring_products', {}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(len(result['products']), 1)
        entry = result['products'][0]
        self.assertEqual(entry['id'], self.product.id)
        self.assertEqual(entry['product_name'], 'Milk Budget 1L')
        self.assertEqual(entry['expiry_date'], str(date.today() + timedelta(days=10)))
        self.assertEqual(entry['days_until_expiry'], 10)
        self.assertTrue(entry['is_available'])

    def test_expiry_tool_ignores_out_of_window_and_dead_batches(self):
        self._batch(self.product, days_ahead=60)                       # beyond 30-day window
        self._batch(self.product, days_ahead=2, status='EXPIRED')      # expired status
        self._batch(self.product, days_ahead=3, remaining=0)           # depleted batch
        other = Product.objects.create(product_name='Juice 1L', unit_price=300, cost_price=200, is_active=True)
        self._batch(other, days_ahead=5)                               # inside window
        result, tool_status = execute_tool('get_expiring_products', {}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual([p['id'] for p in result['products']], [other.id])

    def test_expiry_tool_filters_by_product_name(self):
        self._batch(self.product, days_ahead=5)
        other = Product.objects.create(product_name='Bread Loaf', unit_price=120, cost_price=80, is_active=True)
        self._batch(other, days_ahead=3)
        result, tool_status = execute_tool('get_expiring_products', {'product_name': 'milk'}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual([p['id'] for p in result['products']], [self.product.id])

    def test_expiry_tool_validates_days_argument(self):
        result, tool_status = execute_tool('get_expiring_products', {'days': 'soon'}, self.customer)
        self.assertEqual(tool_status, 'failed')
        self.assertIn('days', result['error'])
        result, tool_status = execute_tool('get_expiring_products', {'days': 10000}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(result['window_days'], 90)

    def test_agent_invokes_expiry_tool_through_provider_loop(self):
        call = SimpleNamespace(name='get_expiring_products', args={'days': 30}, id='call-2')
        first = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content='tool-request')])
        second = SimpleNamespace(function_calls=[], text='Milk Budget 1L expires soonest.')
        with patch('orders.services.agent.get_available_stock', return_value=4), \
                patch('orders.services.agent.get_provider') as get_provider:
            provider = get_provider.return_value
            provider.make_contents.return_value = []
            provider.generate.side_effect = [first, second]
            provider.function_calls.side_effect = lambda response: response.function_calls
            provider.response_text.side_effect = lambda response: response.text
            result = run_agent('Which products are expiring soon?', self.customer)
        self.assertTrue(result['query_success'])
        self.assertEqual(result['bot_response'], 'Milk Budget 1L expires soonest.')
        self.assertEqual(result['tool_calls'], [{'name': 'get_expiring_products', 'status': 'completed'}])
        provider.append_tool_results.assert_called_once()

    def test_expiry_tool_database_failure_is_safe(self):
        with patch('orders.services.agent.Product') as mock_product:
            mock_product.objects.filter.side_effect = RuntimeError('internal database details')
            result, tool_status = execute_tool('get_expiring_products', {}, self.customer)
        self.assertEqual(tool_status, 'failed')
        self.assertEqual(result['error'], 'The requested data is temporarily unavailable.')
        self.assertNotIn('internal', result['error'])

    def test_customer_cannot_execute_manager_tool(self):
        result, tool_status = execute_tool('get_reorder_recommendations', {}, self.customer)
        self.assertEqual(tool_status, 'forbidden')
        self.assertIn('managers', result['error'])

    def test_manager_can_execute_manager_tool(self):
        manager = get_user_model().objects.create_user(username='manager', password='password')
        group, _ = Group.objects.get_or_create(name='MANAGER')
        manager.groups.add(group)
        with patch('orders.services.agent.check_reorder_needs', return_value=[]):
            result, tool_status = execute_tool('get_reorder_recommendations', {}, manager)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(result['recommendations'], [])

    def test_agent_reports_clean_configuration_error_without_api_key(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': ''}, clear=True):
            with self.assertRaises(AgentUnavailable):
                run_agent('cheapest milk', self.customer)

    @patch('google.genai.Client')
    def test_gemini_provider_uses_current_default_model(self, client):
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}, clear=True):
            provider = GeminiProvider('System instruction')
        self.assertEqual(provider.model, DEFAULT_GEMINI_MODEL)

    @patch('google.genai.Client')
    def test_gemini_provider_migrates_retired_configured_model(self, client):
        with patch.dict(os.environ, {
            'GEMINI_API_KEY': 'test-key',
            'GEMINI_MODEL': 'gemini-2.5-flash-lite',
        }, clear=True):
            provider = GeminiProvider('System instruction')
        self.assertEqual(provider.model, DEFAULT_GEMINI_MODEL)

    @patch('orders.services.agent.get_provider')
    def test_agent_executes_provider_requested_tool_and_returns_final_response(self, get_provider):
        call = SimpleNamespace(name='find_cheapest_product', args={'query': 'milk', 'available_only': False}, id='call-1')
        first = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content='tool-request')])
        second = SimpleNamespace(function_calls=[], text='Milk Budget 1L is Rs. 250.')
        provider = get_provider.return_value
        provider.make_contents.return_value = []
        provider.generate.side_effect = [first, second]
        provider.function_calls.side_effect = lambda response: response.function_calls
        provider.response_text.side_effect = lambda response: response.text

        result = run_agent('What is the cheapest milk?', self.customer)

        self.assertTrue(result['query_success'])
        self.assertEqual(result['bot_response'], 'Milk Budget 1L is Rs. 250.')
        self.assertEqual(result['tool_calls'], [{'name': 'find_cheapest_product', 'status': 'completed'}])
        provider.append_tool_results.assert_called_once()

    @patch('orders.services.agent.get_provider')
    def test_provider_failure_returns_a_safe_error(self, get_provider):
        get_provider.return_value.make_contents.return_value = []
        get_provider.return_value.generate.side_effect = RuntimeError('provider details')
        with self.assertRaises(AgentUnavailable) as error:
            run_agent('cheapest milk', self.customer)
        self.assertEqual(str(error.exception), 'The chatbot service is temporarily unavailable. Please try again.')

    @patch('orders.services.agent.get_provider')
    def test_gemini_quota_failure_maps_to_429_without_provider_details(self, get_provider):
        get_provider.return_value.make_contents.return_value = []
        get_provider.return_value.generate.side_effect = GeminiQuotaExceeded('quota details')
        with self.assertRaises(AgentUnavailable) as error:
            run_agent('cheapest milk', self.customer)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(str(error.exception), 'quota details')

    @patch('orders.services.agent.get_provider')
    def test_provider_failure_is_logged_without_secret(self, get_provider):
        get_provider.return_value.make_contents.return_value = []
        get_provider.return_value.generate.side_effect = RuntimeError('request rejected: test-secret')
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-secret'}):
            with self.assertLogs('orders.services.agent', level='WARNING') as logs:
                with self.assertRaises(AgentUnavailable):
                    run_agent('cheapest milk', self.customer)
        self.assertIn('RuntimeError', logs.output[0])
        self.assertIn('[redacted]', logs.output[0])
        self.assertNotIn('test-secret', logs.output[0])

    def test_gemini_tool_result_uses_supported_function_response_shape(self):
        provider = object.__new__(GeminiProvider)
        provider.types = SimpleNamespace(
            Part=SimpleNamespace(from_function_response=Mock(return_value='tool-result')),
            Content=Mock(side_effect=lambda **kwargs: kwargs),
        )
        contents = []
        response = SimpleNamespace(candidates=[SimpleNamespace(content='model-tool-call')])
        call = SimpleNamespace(name='find_cheapest_product', id='call-1')

        provider.append_tool_results(contents, response, [(call, {'product': {'id': 1}})])

        provider.types.Part.from_function_response.assert_called_once_with(
            name='find_cheapest_product', response={'result': {'product': {'id': 1}}}
        )

    def test_tool_failure_does_not_expose_an_exception(self):
        def failing_tool(arguments, user):
            raise RuntimeError('database password should never be exposed')
        with patch.dict('orders.services.agent.TOOL_HANDLERS', {'failing_tool': failing_tool}, clear=False):
            result, tool_status = execute_tool('failing_tool', {}, self.customer)
        self.assertEqual(tool_status, 'failed')
        self.assertEqual(result['error'], 'The requested data is temporarily unavailable.')

    def test_frontend_uses_backend_response_and_has_no_demo_products(self):
        template = open('customer/templates/customer/chatbot.html', encoding='utf-8').read()
        self.assertIn('data.bot_response', template)
        self.assertIn('formatBotMessage', template)
        self.assertNotIn('demoProducts', template)
        self.assertIn('function updateCartBadge', template)
        self.assertIn('buildNav();', template)
        self.assertNotIn('getBotResponse', template)

    def test_intent_choices_include_agent_and_expiry_queries(self):
        choices = [choice[0] for choice in ChatbotLog.INTENT_CHOICES]
        self.assertIn('AGENT_QUERY', choices)
        self.assertIn('EXPIRY_QUERY', choices)

    def test_chatbot_instructions_require_readable_product_lists_and_order_limit(self):
        from orders.services.agent import SYSTEM_INSTRUCTIONS
        self.assertIn('every result on its own line', SYSTEM_INSTRUCTIONS)
        self.assertIn('cannot process online\norders or checkouts through this chat', SYSTEM_INSTRUCTIONS)
        self.assertIn('Never include it in product\nsearch', SYSTEM_INSTRUCTIONS)

# Create your tests here.
