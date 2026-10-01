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
    AgentUnavailable, PUBLIC_TOOLS, _resolve_product, check_purchase_quantity,
    compare_product_sales, execute_tool, find_cheapest_product,
    get_best_selling_products, get_expiring_products, run_agent,
)
from orders.services.gemini import DEFAULT_GEMINI_MODEL, GeminiProvider, GeminiQuotaExceeded
from orders.views import ChatbotQueryView, ChatbotSessionDetailView
from products.models import Category, Product
from purchases.models import Purchase, PurchaseBatch
from sales.models import ItemSalesRecord
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

    @patch('orders.views.run_agent')
    def test_anonymous_visitor_can_call_chatbot_for_public_query(self, run_agent):
        run_agent.return_value = {'bot_response': 'Milk Budget 1L is Rs. 250.', 'query_success': True, 'tool_calls': []}
        response = self.post_chat({'message': 'cheapest milk'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['bot_response'], 'Milk Budget 1L is Rs. 250.')
        # Anonymous request must carry no customer identity.
        log = ChatbotLog.objects.get()
        self.assertIsNone(log.customer_id)
        self.assertIsNone(log.staff_user_id)

    @patch('orders.views.run_agent')
    def test_browser_anonymous_request_with_null_bearer_token_is_public(self, run_agent):
        """Reproduces the real browser request: an anonymous visitor whose
        JS used to send 'Authorization: Bearer null'. Must be allowed."""
        run_agent.return_value = {'bot_response': 'ok', 'query_success': True, 'tool_calls': []}
        request = self.factory.post(
            self.url, {'message': 'cheapest milk'}, format='json',
            HTTP_AUTHORIZATION='Bearer null',
        )
        response = ChatbotQueryView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        log = ChatbotLog.objects.get()
        self.assertIsNone(log.customer_id)
        self.assertIsNone(log.staff_user_id)

    @patch('orders.views.run_agent')
    def test_browser_request_with_expired_or_garbage_token_is_anonymous(self, run_agent):
        """A stale/tampered customer JWT must be treated as anonymous access
        to public tools, not rejected with 401."""
        run_agent.return_value = {'bot_response': 'ok', 'query_success': True, 'tool_calls': []}
        from orders.tokens import get_tokens_for_customer
        stale = get_tokens_for_customer(self.customer)['access'][:-4] + 'AAAA'
        request = self.factory.post(
            self.url, {'message': 'milk'}, format='json',
            HTTP_AUTHORIZATION=f'Bearer {stale}',
        )
        response = ChatbotQueryView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        log = ChatbotLog.objects.get()
        self.assertIsNone(log.customer_id)
        self.assertIsNone(log.staff_user_id)

    @patch('orders.views.run_agent')
    def test_valid_customer_jwt_header_still_authenticates(self, run_agent):
        """A valid customer JWT sent as a header (real browser flow) must
        still resolve to that customer — not be downgraded to anonymous."""
        run_agent.return_value = {'bot_response': 'ok', 'query_success': True, 'tool_calls': []}
        from orders.tokens import get_tokens_for_customer
        token = get_tokens_for_customer(self.customer)['access']
        request = self.factory.post(
            self.url, {'message': 'milk'}, format='json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        response = ChatbotQueryView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ChatbotLog.objects.get().customer_id, self.customer.id)

    def test_anonymous_visitor_can_use_public_readonly_tool(self):
        result, tool_status = execute_tool('search_products', {'query': 'Milk'}, None)
        self.assertEqual(tool_status, 'completed')
        self.assertIn('Milk Budget 1L', [item['product_name'] for item in result['products']])

    def test_anonymous_visitor_cannot_access_customer_specific_operations(self):
        # No tool outside PUBLIC_TOOLS may run without an identity.
        self.assertNotIn('get_reorder_recommendations', PUBLIC_TOOLS)
        # Public tools degrade to the customer-safe payload: no stock figures
        # and no manager analytics for anonymous callers.
        result, tool_status = execute_tool('get_product_details', {'product_name': 'Milk Budget 1L'}, None)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(result['product']['product_name'], 'Milk Budget 1L')
        self.assertNotIn('current_stock', result['product'])

    def test_anonymous_visitor_cannot_access_manager_tools(self):
        for manager_tool in (
            'get_low_stock_products', 'get_reorder_recommendations', 'get_product_lifecycle',
            'get_health_score', 'get_sales_summary', 'get_slow_moving_products', 'get_profit_margin',
        ):
            result, tool_status = execute_tool(manager_tool, {}, None)
            self.assertEqual(tool_status, 'forbidden', manager_tool)
            self.assertIn('managers', result['error'])

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

    @override_settings(CHATBOT_RATE_LIMIT=2, CHATBOT_RATE_WINDOW_SECONDS=60)
    @patch('orders.views.run_agent')
    def test_anonymous_requests_respect_rate_limit(self, run_agent):
        run_agent.return_value = {'bot_response': 'ok', 'query_success': True, 'tool_calls': []}
        self.assertEqual(self.post_chat({'message': 'milk'}).status_code, 200)
        self.assertEqual(self.post_chat({'message': 'milk'}).status_code, 200)
        blocked = self.post_chat({'message': 'milk'})
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked.headers)
        self.assertEqual(run_agent.call_count, 2)
        # Anonymous quota is tracked per client IP, not skipped entirely.
        self.assertTrue(
            ChatbotRateLimit.objects.filter(actor_key__startswith='anonymous:').exists()
        )

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

    def _sales_record(self, product, units, days_ago=1, price='250.00'):
        ItemSalesRecord.objects.create(
            product=product, sale_date=date.today() - timedelta(days=days_ago),
            quantity_sold=units, unit_price=price, total_amount=str(float(price) * units),
        )

    def test_compare_product_sales_a_beats_b(self):
        self.product.product_name = 'Ambewela Set Yoghurt'
        self.product.save(update_fields=['product_name'])
        self._sales_record(self.product, 125)
        other = Product.objects.create(product_name='Chello Jelly Yoghurt', unit_price=180, cost_price=120, is_active=True)
        self._sales_record(other, 82)
        result, tool_status = execute_tool('compare_product_sales', {'product_a': 'Ambewela', 'product_b': 'Chello'}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(result['product_a']['units_sold'], 125)
        self.assertEqual(result['product_b']['units_sold'], 82)
        self.assertEqual(result['better_selling'], 'Ambewela Set Yoghurt')
        self.assertEqual(result['difference_units'], 43)

    def test_compare_product_sales_b_beats_a_and_equal_and_missing(self):
        self.product.product_name = 'Ambewela Set Yoghurt'
        self.product.save(update_fields=['product_name'])
        self._sales_record(self.product, 10)
        other = Product.objects.create(product_name='Chello Jelly Yoghurt', unit_price=180, cost_price=120, is_active=True)
        self._sales_record(other, 40)
        result, _ = execute_tool('compare_product_sales', {'product_a': 'Ambewela', 'product_b': 'Chello'}, self.customer)
        self.assertEqual(result['better_selling'], 'Chello Jelly Yoghurt')
        # Equal sales → better_selling is None, difference 0.
        ItemSalesRecord.objects.all().delete()
        self._sales_record(self.product, 7)
        self._sales_record(other, 7)
        result, _ = execute_tool('compare_product_sales', {'product_a': 'Ambewela', 'product_b': 'Chello'}, self.customer)
        self.assertIsNone(result['better_selling'])
        self.assertEqual(result['difference_units'], 0)
        # Unknown product → structured not_found, no exception.
        result, tool_status = execute_tool('compare_product_sales', {'product_a': 'Ambewela', 'product_b': 'Ghost Item'}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(result['not_found'], ['Ghost Item'])

    def test_compare_product_sales_requires_both_products(self):
        result, tool_status = execute_tool('compare_product_sales', {'product_a': 'Milk'}, self.customer)
        self.assertEqual(tool_status, 'failed')
        self.assertIn('product_a and product_b', result['error'])

    def test_best_selling_products_units_only_for_customer(self):
        self._sales_record(self.product, 30)
        other = Product.objects.create(product_name='Juice 1L', unit_price=300, cost_price=200, is_active=True)
        self._sales_record(other, 50)
        result, tool_status = execute_tool('get_best_selling_products', {'limit': 2}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual([p['product_name'] for p in result['products']], ['Juice 1L', 'Milk Budget 1L'])
        self.assertEqual(result['products'][0]['units_sold'], 50)
        self.assertNotIn('revenue', result['products'][0])

    def test_best_selling_products_revenue_for_manager(self):
        manager = get_user_model().objects.create_user(username='mgr2', password='password')
        group, _ = Group.objects.get_or_create(name='MANAGER')
        manager.groups.add(group)
        self._sales_record(self.product, 30)
        result, tool_status = execute_tool('get_best_selling_products', {}, manager)
        self.assertEqual(tool_status, 'completed')
        self.assertIn('revenue', result['products'][0])

    def test_check_purchase_quantity_scenarios(self):
        with patch('orders.services.agent.get_available_stock', return_value=1850):
            result, tool_status = execute_tool('check_purchase_quantity', {'product_name': 'Milk Budget', 'quantity': 2000}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(result['available_quantity'], 1850)
        self.assertFalse(result['can_fulfill'])
        self.assertEqual(result['shortfall'], 150)
        self.assertEqual(result['remaining'], 0)
        with patch('orders.services.agent.get_available_stock', return_value=2500):
            result, _ = execute_tool('check_purchase_quantity', {'product_name': 'Milk Budget', 'quantity': 2000}, self.customer)
        self.assertTrue(result['can_fulfill'])
        self.assertEqual(result['remaining'], 500)
        with patch('orders.services.agent.get_available_stock', return_value=2000):
            result, _ = execute_tool('check_purchase_quantity', {'product_name': 'Milk Budget', 'quantity': 2000}, self.customer)
        self.assertTrue(result['can_fulfill'])
        self.assertEqual(result['remaining'], 0)

    def test_check_purchase_quantity_rejects_bad_input_and_unknown_product(self):
        result, tool_status = execute_tool('check_purchase_quantity', {'product_name': 'Milk Budget', 'quantity': -5}, self.customer)
        self.assertEqual(tool_status, 'failed')
        self.assertIn('positive', result['error'])
        result, tool_status = execute_tool('check_purchase_quantity', {'product_name': 'Milk Budget', 'quantity': 'many'}, self.customer)
        self.assertEqual(tool_status, 'failed')
        result, tool_status = execute_tool('check_purchase_quantity', {'product_name': 'Ghost Item', 'quantity': 5}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertIsNone(result['product'])

    def _sales_record(self, product, units, days_ago=1, price='250.00'):
        ItemSalesRecord.objects.create(
            product=product, sale_date=date.today() - timedelta(days=days_ago),
            quantity_sold=units, unit_price=price, total_amount=str(float(price) * units),
        )

    def test_resolve_product_exact_match(self):
        # Exact case-insensitive name is the highest priority.
        self.assertIsNone(_resolve_product(''))
        self.assertEqual(_resolve_product('Milk Budget 1L'), self.product)
        self.assertEqual(_resolve_product('milk budget 1l'), self.product)

    def test_resolve_product_natural_token_aware(self):
        # Reorder + filler word "of" ignored; inline size token "1kg" matched.
        Product.objects.create(product_name='Fortune Vegetable Oil 1kg', unit_price=400, cost_price=300, is_active=True)
        product = _resolve_product('1kg of fortune vegetable oil')
        self.assertIsNotNone(product)
        self.assertEqual(product.product_name, 'Fortune Vegetable Oil 1kg')

    def test_resolve_product_supports_inline_size_and_case(self):
        # "1L" size token matches across case + word order (not an exact match).
        product = _resolve_product('budget milk 1L')
        self.assertIsNotNone(product)
        self.assertEqual(product, self.product)

    def test_resolve_product_disambiguates_by_size(self):
        Product.objects.create(product_name='Sugar Premium 1kg', unit_price=120, cost_price=80, is_active=True)
        Product.objects.create(product_name='Sugar Premium 1L', unit_price=130, cost_price=90, is_active=True)
        # The size token breaks the tie deterministically.
        self.assertEqual(_resolve_product('Sugar 1kg').product_name, 'Sugar Premium 1kg')

    def test_resolve_product_ambiguous_returns_none(self):
        # Two equally-strong sugar products -> must not guess an arbitrary one.
        Product.objects.create(product_name='Sugar Premium 1kg', unit_price=120, cost_price=80, is_active=True)
        Product.objects.create(product_name='Sugar Premium 1L', unit_price=130, cost_price=90, is_active=True)
        self.assertIsNone(_resolve_product('Sugar Premium'))

    def test_resolve_product_no_match(self):
        self.assertIsNone(_resolve_product('Ghost Item'))

    def test_check_purchase_quantity_result_shape(self):
        with patch('orders.services.agent.get_available_stock', return_value=1850):
            result, tool_status = execute_tool(
                'check_purchase_quantity', {'product_name': 'Milk Budget 1L', 'quantity': 2000}, self.customer
            )
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(result['product'], 'Milk Budget 1L')
        self.assertEqual(result['requested_quantity'], 2000)
        self.assertEqual(result['available_quantity'], 1850)
        self.assertFalse(result['can_fulfill'])
        self.assertEqual(result['shortfall'], 150)
        self.assertEqual(result['remaining'], 0)

    def test_check_purchase_quantity_uses_natural_resolver(self):
        # A token-only request (not an exact name) must still resolve correctly.
        with patch('orders.services.agent.get_available_stock', return_value=2500):
            result, tool_status = execute_tool(
                'check_purchase_quantity', {'product_name': 'milk budget', 'quantity': 2000}, self.customer
            )
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(result['product'], 'Milk Budget 1L')
        self.assertTrue(result['can_fulfill'])
        self.assertEqual(result['remaining'], 500)

    def test_agent_selects_sales_comparison_tool(self):
        call = SimpleNamespace(name='compare_product_sales', args={'product_a': 'Ambewela', 'product_b': 'Chello'}, id='call-3')
        first = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content='tool-request')])
        second = SimpleNamespace(function_calls=[], text='Ambewela is selling more.')
        with patch('orders.services.agent.get_provider') as get_provider:
            provider = get_provider.return_value
            provider.make_contents.return_value = []
            provider.generate.side_effect = [first, second]
            provider.function_calls.side_effect = lambda response: response.function_calls
            provider.response_text.side_effect = lambda response: response.text
            result = run_agent('Which is best selling Ambewela or Chello?', self.customer)
        self.assertEqual(result['tool_calls'], [{'name': 'compare_product_sales', 'status': 'completed'}])
        self.assertEqual(result['bot_response'], 'Ambewela is selling more.')

    def test_agent_selects_purchase_quantity_tool(self):
        call = SimpleNamespace(name='check_purchase_quantity', args={'product_name': 'Fortune Vegetable Oil', 'quantity': 2000}, id='call-4')
        first = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content='tool-request')])
        second = SimpleNamespace(function_calls=[], text='Not enough stock for 2000 units.')
        with patch('orders.services.agent.get_provider') as get_provider:
            provider = get_provider.return_value
            provider.make_contents.return_value = []
            provider.generate.side_effect = [first, second]
            provider.function_calls.side_effect = lambda response: response.function_calls
            provider.response_text.side_effect = lambda response: response.text
            result = run_agent('Can I buy 2000 Fortune Vegetable Oil?', self.customer)
        self.assertEqual(result['tool_calls'], [{'name': 'check_purchase_quantity', 'status': 'completed'}])
        self.assertEqual(result['bot_response'], 'Not enough stock for 2000 units.')

    def test_best_selling_without_category(self):
        other = Product.objects.create(product_name='Juice 1L', unit_price=300, cost_price=200, is_active=True)
        self._sales_record(self.product, 30)
        self._sales_record(other, 50)
        result, tool_status = execute_tool('get_best_selling_products', {'limit': 2}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual([p['product_name'] for p in result['products']], ['Juice 1L', 'Milk Budget 1L'])
        self.assertEqual(result['products'][0]['units_sold'], 50)
        self.assertNotIn('revenue', result['products'][0])

    def test_best_selling_with_category(self):
        oils = Category.objects.create(category_name='Cooking Oils')
        drinks = Category.objects.create(category_name='Beverages')
        oil_a = Product.objects.create(product_name='Fortune Vegetable Oil 1L', unit_price=400, cost_price=300, is_active=True, category=oils)
        oil_b = Product.objects.create(product_name='Sunflower Oil 1L', unit_price=350, cost_price=250, is_active=True, category=oils)
        drink = Product.objects.create(product_name='Cola 1L', unit_price=50, cost_price=20, is_active=True, category=drinks)
        self._sales_record(oil_a, 10)
        self._sales_record(oil_b, 5)
        self._sales_record(drink, 999)
        result, tool_status = execute_tool('get_best_selling_products', {'limit': 10, 'category': 'cooking oils'}, self.customer)
        self.assertEqual(tool_status, 'completed')
        names = [p['product_name'] for p in result['products']]
        self.assertEqual(names, ['Fortune Vegetable Oil 1L', 'Sunflower Oil 1L'])
        self.assertEqual(result['products'][0]['units_sold'], 10)
        self.assertNotIn('Cola 1L', names)
        self.assertNotIn('revenue', result['products'][0])

    def test_best_selling_nonexistent_category(self):
        oils = Category.objects.create(category_name='Cooking Oils')
        oil = Product.objects.create(product_name='Fortune Vegetable Oil 1L', unit_price=400, cost_price=300, is_active=True, category=oils)
        self._sales_record(oil, 10)
        result, tool_status = execute_tool('get_best_selling_products', {'category': 'Nonexistent Category'}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(result['products'], [])
        self.assertIn('period', result)

    def test_system_instructions_require_tool_before_refusal(self):
        from orders.services.agent import SYSTEM_INSTRUCTIONS
        self.assertIn('Never say you lack access to sales, stock, price, or\nexpiry information before calling the matching tool', SYSTEM_INSTRUCTIONS)
        self.assertIn('compare_product_sales', SYSTEM_INSTRUCTIONS)
        self.assertIn('check_purchase_quantity', SYSTEM_INSTRUCTIONS)

    def test_best_selling_category_role_boundary(self):
        manager = get_user_model().objects.create_user(username='mgr4', password='password')
        group, _ = Group.objects.get_or_create(name='MANAGER')
        manager.groups.add(group)
        oils = Category.objects.create(category_name='Cooking Oils')
        oil = Product.objects.create(product_name='Fortune Vegetable Oil 1L', unit_price=400, cost_price=300, is_active=True, category=oils)
        self._sales_record(oil, 10)
        cust_result, _ = execute_tool('get_best_selling_products', {'category': 'cooking oils'}, self.customer)
        self.assertNotIn('revenue', cust_result['products'][0])
        mgr_result, _ = execute_tool('get_best_selling_products', {'category': 'cooking oils'}, manager)
        self.assertIn('revenue', mgr_result['products'][0])
        # Role boundary unchanged: best-selling is customer-safe, reorder stays manager-only.
        forbidden, status = execute_tool('get_reorder_recommendations', {}, self.customer)
        self.assertEqual(status, 'forbidden')
        self.assertIn('managers', forbidden['error'])

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
