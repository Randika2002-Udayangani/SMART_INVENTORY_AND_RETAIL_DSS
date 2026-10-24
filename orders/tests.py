from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate
from unittest.mock import patch
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import TestCase
from django.test import override_settings
from django.utils import timezone

from orders.models import ChatbotLog, ChatbotRateLimit, Customer
from orders.services.agent import (
    AgentUnavailable, PUBLIC_TOOLS, _resolve_product, _resolve_product_with_candidates, check_purchase_quantity,
    compare_product_sales, execute_tool, find_cheapest_product,
    get_best_selling_products, get_expiring_products, run_agent,
)
from orders.services.gemini import DEFAULT_GEMINI_MODEL, GeminiProvider, GeminiQuotaExceeded, GeminiUnavailable
from orders.views import (
    ChatbotQueryView,
    ChatbotSessionDetailView,
    CustomerChangePasswordView,
    CustomerRegisterView,
    OrderListCreateView,
    _is_pickup_slot_available,
)
from inventory.models import DiscountRecommendation
from products.models import Category, Product
from purchases.models import Purchase, PurchaseBatch
from sales.models import ItemSalesRecord
from suppliers.models import Supplier

from zoneinfo import ZoneInfo

def local_today():
    """The app counts days in Sri Lanka time, so the tests must too."""
    return datetime.now(ZoneInfo('Asia/Colombo')).date()


class PickupSlotAvailabilityTests(TestCase):
    def test_same_day_slot_is_available_until_its_end_time(self):
        now = datetime(2026, 10, 20, 11, 59, tzinfo=ZoneInfo('Asia/Colombo'))
        self.assertTrue(_is_pickup_slot_available(date(2026, 10, 20), 'MORNING', now))

        now = datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo('Asia/Colombo'))
        self.assertFalse(_is_pickup_slot_available(date(2026, 10, 20), 'MORNING', now))

    def test_past_slots_and_dates_are_unavailable_but_future_slots_are_available(self):
        now = datetime(2026, 10, 20, 21, 0, tzinfo=ZoneInfo('Asia/Colombo'))
        self.assertFalse(_is_pickup_slot_available(date(2026, 10, 20), 'EVENING', now))
        self.assertFalse(_is_pickup_slot_available(date(2026, 10, 19), 'MORNING', now))
        self.assertTrue(_is_pickup_slot_available(date(2026, 10, 21), 'MORNING', now))

    @patch('orders.views.timezone.now')
    def test_order_endpoint_rejects_a_pickup_slot_that_has_passed(self, mocked_now):
        from django.urls import reverse

        from orders.models import Customer

        local_now = datetime(2026, 10, 20, 20, 30, tzinfo=ZoneInfo('Asia/Colombo'))
        mocked_now.return_value = local_now
        customer = Customer.objects.create(
            name='Pickup Test Customer',
            email='pickup-test@example.com',
            password_hash='unused',
        )
        request = APIRequestFactory().post(
            reverse('order-list-create'),
            {
                'pickup_date': '2026-10-20',
                'time_slot': 'MORNING',
                'items': [{'product_id': 1, 'quantity': 1}],
            },
            format='json',
        )
        force_authenticate(request, user=customer)

        response = OrderListCreateView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn('pickup time has passed', response.data['error'])


class CustomerPasswordMinimumTests(TestCase):
    def test_registration_requires_at_least_eight_characters(self):
        factory = APIRequestFactory()
        register = CustomerRegisterView.as_view()

        short_request = factory.post(
            '/api/customer-auth/register/',
            {'name': 'Test Customer', 'email': 'short@example.com', 'password': '1234567'},
            format='json',
        )
        short_response = register(short_request)
        self.assertEqual(short_response.status_code, 400)
        self.assertIn('8 characters', short_response.data['error'])

        valid_request = factory.post(
            '/api/customer-auth/register/',
            {'name': 'Test Customer', 'email': 'valid@example.com', 'password': '12345678'},
            format='json',
        )
        valid_response = register(valid_request)
        self.assertEqual(valid_response.status_code, 201)
        self.assertTrue(Customer.objects.filter(email='valid@example.com').exists())

    def test_change_password_rejects_passwords_shorter_than_eight_characters(self):
        from django.contrib.auth.hashers import make_password

        customer = Customer.objects.create(
            name='Password Test Customer',
            email='password-test@example.com',
            password_hash=make_password('current-password'),
        )
        request = APIRequestFactory().post(
            '/api/customer-auth/change-password/',
            {'old_password': 'current-password', 'new_password': '1234567'},
            format='json',
        )
        force_authenticate(request, user=customer)

        response = CustomerChangePasswordView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn('8 characters', response.data['error'])


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
            supplier=supplier, purchase_date=local_today() - timedelta(days=20)
        )
        return PurchaseBatch.objects.create(
            purchase=purchase, product=product, quantity_received=10,
            cost_price=100, remaining_quantity=remaining,
            expiry_date=local_today() + timedelta(days=days_ahead), status=status,
        )

    def test_customer_can_execute_expiry_tool_with_real_data(self):
        self._batch(self.product, days_ahead=10)
        result, tool_status = execute_tool('get_expiring_products', {}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(len(result['products']), 1)
        entry = result['products'][0]
        self.assertEqual(entry['id'], self.product.id)
        self.assertEqual(entry['product_name'], 'Milk Budget 1L')
        self.assertEqual(entry['expiry_date'], str(local_today() + timedelta(days=10)))
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
            product=product, sale_date=local_today() - timedelta(days=days_ago),
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
            product=product, sale_date=local_today() - timedelta(days=days_ago),
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


class ChatbotDiscountToolTests(TestCase):
    """F09 — get_active_discounts: shared, anonymous-safe, customer-safe."""

    def setUp(self):
        self.customer = Customer.objects.create(
            name='Deal Seeker', email='deals@example.com',
            password_hash='unused',
        )
        self.factory = APIRequestFactory()
        self.url = reverse('chatbot')
        self.category = Category.objects.create(category_name='Hair Care')
        self.product = Product.objects.create(
            product_name='AYUSH ANTI DANDRUFF SHAMPOO 100ml',
            category=self.category,
            unit_price=Decimal('475.00'), cost_price=Decimal('400.00'),
            is_active=True,
        )
        supplier = Supplier.objects.create(supplier_name='Test Supplier')
        purchase = Purchase.objects.create(
            supplier=supplier, purchase_date=local_today() - timedelta(days=20)
        )
        self.batch = PurchaseBatch.objects.create(
            purchase=purchase, product=self.product, quantity_received=10,
            cost_price=Decimal('400.00'), remaining_quantity=10,
            expiry_date=local_today() + timedelta(days=15), status='ACTIVE',
        )
        self.recommendation = DiscountRecommendation.objects.create(
            product=self.product, batch=self.batch,
            days_until_expiry=15,
            current_price=Decimal('475.00'),
            recommended_discount_pct=Decimal('7.84'),
            recommended_price=Decimal('437.77'),
            recovery_sell=Decimal('470.00'),
            recovery_return=Decimal('440.00'),
            best_action='DISCOUNT',
            status='APPLIED',
        )

    def test_discount_tool_is_registered_and_public_not_manager_only(self):
        from orders.services.agent import MANAGER_TOOLS, TOOLS, TOOL_HANDLERS
        self.assertIn('get_active_discounts', TOOL_HANDLERS)
        self.assertIn('get_active_discounts', {tool['name'] for tool in TOOLS})
        self.assertIn('get_active_discounts', PUBLIC_TOOLS)
        self.assertNotIn('get_active_discounts', MANAGER_TOOLS)

    def test_anonymous_visitor_can_execute_discount_tool(self):
        result, tool_status = execute_tool('get_active_discounts', {}, None)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(len(result['discounts']), 1)
        deal = result['discounts'][0]
        self.assertEqual(deal['product_id'], self.product.id)
        self.assertEqual(deal['product_name'], 'AYUSH ANTI DANDRUFF SHAMPOO 100ml')
        self.assertEqual(deal['category'], 'Hair Care')
        self.assertEqual(deal['selling_price'], 475.0)
        self.assertEqual(deal['discount_percentage'], 7.84)
        self.assertEqual(deal['discounted_price'], 437.77)
        self.assertEqual(deal['expiry_date'], str(local_today() + timedelta(days=15)))

    def test_discount_payload_has_no_internal_fields(self):
        result, tool_status = execute_tool('get_active_discounts', {}, None)
        self.assertEqual(tool_status, 'completed')
        deal = result['discounts'][0]
        for internal in (
            'recovery_sell', 'recovery_return', 'recovery_discard',
            'profit_protected', 'best_action', 'status', 'reviewed_by',
            'reviewed_at', 'current_price', 'days_until_expiry', 'batch',
        ):
            self.assertNotIn(internal, deal)

    def test_registered_customer_can_execute_discount_tool(self):
        result, tool_status = execute_tool('get_active_discounts', {}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(len(result['discounts']), 1)

    def test_stale_depleted_deal_is_not_surfaced(self):
        # The recommendation is still APPLIED, but its batch is dead stock.
        self.batch.status = 'DEPLETED'
        self.batch.remaining_quantity = 0
        self.batch.save()
        result, tool_status = execute_tool('get_active_discounts', {}, self.customer)
        self.assertEqual(tool_status, 'completed')
        self.assertEqual(result, {'discounts': []})
        self.assertNotIn('products', result)   # never a product-search fallback

    def test_discount_tool_is_offered_to_anonymous_visitors(self):
        with patch('orders.services.agent.get_provider') as get_provider:
            provider = get_provider.return_value
            provider.make_contents.return_value = []
            provider.generate.side_effect = [SimpleNamespace(function_calls=[], text='no deals')]
            provider.function_calls.side_effect = lambda response: response.function_calls
            provider.response_text.side_effect = lambda response: response.text
            run_agent('any discounts today?', None)
        offered = {tool['name'] for tool in provider.generate.call_args[0][1]}
        self.assertIn('get_active_discounts', offered)
        # Manager-only analytics stay hidden from anonymous visitors.
        self.assertNotIn('get_reorder_recommendations', offered)

    def test_agent_invokes_discount_tool_through_provider_loop(self):
        call = SimpleNamespace(name='get_active_discounts', args={}, id='call-9')
        first = SimpleNamespace(
            function_calls=[call],
            candidates=[SimpleNamespace(content='tool-request')],
        )
        second = SimpleNamespace(function_calls=[], text='No active discounts right now.')
        with patch('orders.services.agent.get_provider') as get_provider:
            provider = get_provider.return_value
            provider.make_contents.return_value = []
            provider.generate.side_effect = [first, second]
            provider.function_calls.side_effect = lambda response: response.function_calls
            provider.response_text.side_effect = lambda response: response.text
            result = run_agent('are there any discounts?', None)
        self.assertTrue(result['query_success'])
        self.assertEqual(
            result['tool_calls'],
            [{'name': 'get_active_discounts', 'status': 'completed'}],
        )
        provider.append_tool_results.assert_called_once()

    def test_system_instructions_route_discount_words_to_tool(self):
        from orders.services.agent import SYSTEM_INSTRUCTIONS
        self.assertIn('get_active_discounts', SYSTEM_INSTRUCTIONS)
        self.assertIn('Never pass the word "discount"', SYSTEM_INSTRUCTIONS)
        self.assertIn('LKR original → LKR discounted (X% OFF)', SYSTEM_INSTRUCTIONS)
        # Existing instruction guarantees must survive the addition.
        self.assertIn('every result on its own line', SYSTEM_INSTRUCTIONS)

    @patch('orders.views.run_agent')
    def test_anonymous_endpoint_accepts_discount_question(self, run_agent):
        run_agent.return_value = {
            'bot_response': 'No active discounts right now.',
            'query_success': True, 'tool_calls': [],
        }
        request = self.factory.post(self.url, {'message': 'any discounts?'}, format='json')
        response = ChatbotQueryView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        log = ChatbotLog.objects.get()
        self.assertIsNone(log.customer_id)
        self.assertEqual(log.user_message, 'any discounts?')

    @patch('orders.views.run_agent')
    def test_registered_endpoint_accepts_discount_question(self, run_agent):
        run_agent.return_value = {
            'bot_response': 'Two deals today.',
            'query_success': True, 'tool_calls': [],
        }
        request = self.factory.post(self.url, {'message': 'any deals today?'}, format='json')
        force_authenticate(request, user=self.customer)
        response = ChatbotQueryView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        log = ChatbotLog.objects.get()
        self.assertEqual(log.customer_id, self.customer.id)

class ChatbotProductMatchingTests(TestCase):
    """Regression coverage for the shared product-matching strategy.

    Uses a realistic mini-catalogue mirroring the reported failures: milk
    powders, a chocolate malt drink and liquid UHT milk in separate
    categories, so cross-type false positives are observable.  No Gemini call
    is made anywhere — tool handlers run against the database directly and
    the agent loop uses deterministic mocks.
    """

    def setUp(self):
        self.customer = Customer.objects.create(
            name='Matching Customer', email='matching@example.com', password_hash='unused')
        self.powder_category = Category.objects.create(category_name='Milk Powder')
        self.malt_category = Category.objects.create(category_name='Malt Drinks')
        self.dairy_category = Category.objects.create(category_name='Dairy')
        self.nido = Product.objects.create(
            product_name='Nido Milk Powder 400g', category=self.powder_category,
            unit_price=1200, cost_price=1000, is_active=True)
        self.anchor = Product.objects.create(
            product_name='Anchor Full Cream Milk Powder 1kg', category=self.powder_category,
            unit_price=1500, cost_price=1250, is_active=True)
        self.milo = Product.objects.create(
            product_name='Nestle Milo Malt Drink 400g', category=self.malt_category,
            unit_price=900, cost_price=750, is_active=True)
        self.uht = Product.objects.create(
            product_name='Ambewela UHT Milk 1L', category=self.dairy_category,
            unit_price=520, cost_price=430, is_active=True)

    @staticmethod
    def _names(result):
        return sorted(product['product_name'] for product in result['products'])

    # 1 & 2 — exact/partial searches, case, spacing, punctuation, plurals.

    def test_search_exact_and_partial_product_names(self):
        result, status = execute_tool('search_products', {'query': 'nido'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(self._names(result), ['Nido Milk Powder 400g'])
        result, _ = execute_tool('search_products', {'query': 'full cream'}, self.customer)
        self.assertEqual(self._names(result), ['Anchor Full Cream Milk Powder 1kg'])

    def test_search_normalises_case_and_extra_whitespace(self):
        for query in ('  NIDO  ', 'NiDo', 'nido\tmilk\npowder', 'milk  powder'):
            with self.subTest(query=query):
                result, status = execute_tool('search_products', {'query': query}, self.customer)
                self.assertEqual(status, 'completed')
                self.assertIn('Nido Milk Powder 400g', self._names(result))

    def test_search_handles_catalogue_punctuation_and_accents(self):
        Product.objects.create(
            product_name="Nestlé's Cocoa Powder 200g", category=self.powder_category,
            unit_price=700, cost_price=550, is_active=True)
        # Accented/possessive catalogue name + a misspelt query word.
        result, _ = execute_tool('search_products', {'query': 'nestles cocoa poweder'}, self.customer)
        self.assertEqual(self._names(result), ["Nestlé's Cocoa Powder 200g"])
        # Possessive query against the plain catalogue name.
        result, _ = execute_tool('search_products', {'query': "nestle's milo"}, self.customer)
        self.assertEqual(self._names(result), ['Nestle Milo Malt Drink 400g'])

    # 3 & 4 — category/type queries and distinguishing similar product types.

    def test_category_type_query_handles_plural_word_order_and_spacing(self):
        expected = ['Anchor Full Cream Milk Powder 1kg', 'Nido Milk Powder 400g']
        for query in ('milk powder', 'milk powders', 'powder milk', 'milk  powders'):
            with self.subTest(query=query):
                result, status = execute_tool('search_products', {'query': query}, self.customer)
                self.assertEqual(status, 'completed')
                self.assertEqual(self._names(result), expected)

    def test_milk_powder_search_excludes_other_product_types(self):
        result, _ = execute_tool('search_products', {'query': 'milk powder'}, self.customer)
        names = self._names(result)
        self.assertNotIn('Nestle Milo Malt Drink 400g', names)  # chocolate malt drink
        self.assertNotIn('Ambewela UHT Milk 1L', names)         # liquid UHT milk

    def test_single_word_search_matches_only_products_containing_the_word(self):
        result, _ = execute_tool('search_products', {'query': 'milk'}, self.customer)
        names = self._names(result)
        self.assertIn('Nido Milk Powder 400g', names)
        self.assertIn('Ambewela UHT Milk 1L', names)
        self.assertNotIn('Nestle Milo Malt Drink 400g', names)

    def test_missing_category_data_falls_back_to_query_matching(self):
        # The model guessed a category row that does not exist in the
        # catalogue; the user's query words must still find the products.
        result, status = execute_tool(
            'search_products', {'query': 'milk powder', 'category': 'Powdered Milk'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(self._names(result),
                         ['Anchor Full Cream Milk Powder 1kg', 'Nido Milk Powder 400g'])
        # Category phrase passed as the category with no query at all.
        result, _ = execute_tool('search_products', {'category': 'milk powder'}, self.customer)
        self.assertEqual(self._names(result),
                         ['Anchor Full Cream Milk Powder 1kg', 'Nido Milk Powder 400g'])

    def test_fuzzy_typo_resolution_stays_on_the_right_product(self):
        self.assertEqual(_resolve_product('nidoo'), self.nido)
        self.assertEqual(_resolve_product('miloo'), self.milo)
        self.assertEqual(_resolve_product('ambewella'), self.uht)

    def test_ambiguous_product_returns_candidates_instead_of_guessing(self):
        Product.objects.create(
            product_name='Nido Milk Powder 500g', category=self.powder_category,
            unit_price=1350, cost_price=1100, is_active=True)
        result, status = execute_tool('get_current_stock', {'product_name': 'milk powder'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertIsNone(result['product'])
        self.assertIn('candidates', result)
        self.assertEqual(result['candidates'], ['Nido Milk Powder 400g', 'Nido Milk Powder 500g'])
        self.assertIn('which one', result['message'])

    def test_details_and_stock_tools_resolve_typos_and_plurals(self):
        result, status = execute_tool('get_product_details', {'product_name': 'miloo'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['product']['product_name'], 'Nestle Milo Malt Drink 400g')
        result, _ = execute_tool('get_current_stock', {'product_name': 'nido milk powders'}, self.customer)
        self.assertEqual(result['product']['product_name'], 'Nido Milk Powder 400g')

    # 5 — price and cheapest-product queries.

    def test_cheapest_product_respects_product_type(self):
        # "milk poweder" must find powders despite the typo; the cheaper
        # chocolate malt drink must not win just for containing a word.
        result, status = execute_tool('find_cheapest_product', {'query': 'milk poweder'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['product']['product_name'], 'Nido Milk Powder 400g')
        self.assertEqual(result['product']['selling_price'], 1200.0)
        self.assertIn('Anchor Full Cream Milk Powder 1kg',
                      [alt['product_name'] for alt in result['alternatives']])

    def test_price_query_returns_matching_product_with_price(self):
        result, status = execute_tool('get_product_details', {'product_name': 'milo'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['product']['product_name'], 'Nestle Milo Malt Drink 400g')
        self.assertEqual(result['product']['selling_price'], 900.0)

    # 6 — sales comparison and stock queries through the improved matcher.

    def test_sales_comparison_tolerates_typos_on_both_sides(self):
        ItemSalesRecord.objects.create(
            product=self.nido, sale_date=local_today() - timedelta(days=1),
            quantity_sold=120, unit_price='1200.00', total_amount='144000.00')
        ItemSalesRecord.objects.create(
            product=self.milo, sale_date=local_today() - timedelta(days=1),
            quantity_sold=40, unit_price='900.00', total_amount='36000.00')
        result, status = execute_tool(
            'compare_product_sales', {'product_a': 'nidoo', 'product_b': 'miloo'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['better_selling'], 'Nido Milk Powder 400g')
        self.assertEqual(result['difference_units'], 80)

    def test_stock_query_by_product_type_lists_candidates_not_a_guess(self):
        # Two equally-plausible powders: "milk powder" is a type, not one
        # product, so the tool must not silently pick whichever row the
        # database returns first — it must offer the tied candidates.
        Product.objects.create(
            product_name='Nido Milk Powder 500g', category=self.powder_category,
            unit_price=1350, cost_price=1100, is_active=True)
        result, status = execute_tool('get_current_stock', {'product_name': 'milk powder'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertIsNone(result['product'])
        self.assertEqual(len(result['candidates']), 2)

    def test_best_selling_category_argument_tolerates_plural_phrasing(self):
        ItemSalesRecord.objects.create(
            product=self.nido, sale_date=local_today() - timedelta(days=1),
            quantity_sold=30, unit_price='1200.00', total_amount='36000.00')
        ItemSalesRecord.objects.create(
            product=self.milo, sale_date=local_today() - timedelta(days=1),
            quantity_sold=999, unit_price='900.00', total_amount='899100.00')
        result, status = execute_tool(
            'get_best_selling_products', {'category': 'milk powders'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual([p['product_name'] for p in result['products']],
                         ['Nido Milk Powder 400g'])

    # 7 — conversational corrections and follow-ups through the agent loop.

    def test_conversational_correction_searches_the_corrected_terms(self):
        call = SimpleNamespace(name='search_products', args={'query': 'milk powder'}, id='call-fix')
        first = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content='tool-request')])
        second = SimpleNamespace(function_calls=[], text='Here are the milk powders.')
        history = [
            {'role': 'user', 'content': 'What are the milk powders available?'},
            {'role': 'assistant', 'content': 'The Nestle Milo chocolate malt drink is a great option.'},
        ]
        with patch('orders.services.agent.get_provider') as get_provider:
            provider = get_provider.return_value
            provider.make_contents.return_value = []
            provider.generate.side_effect = [first, second]
            provider.function_calls.side_effect = lambda response: response.function_calls
            provider.response_text.side_effect = lambda response: response.text
            result = run_agent('Not chocolate, milk powder', self.customer, history=history)
        self.assertTrue(result['query_success'])
        self.assertEqual(result['tool_calls'], [{'name': 'search_products', 'status': 'completed'}])
        # The session history is still passed to the provider unchanged.
        provider.make_contents.assert_called_once_with(history, 'Not chocolate, milk powder')
        # The structured tool result fed back to the model contains only the
        # corrected product type — no chocolate malt drink, no UHT milk.
        _contents, _response, results = provider.append_tool_results.call_args[0]
        payload = results[0][1]
        self.assertEqual(sorted(p['product_name'] for p in payload['products']),
                         ['Anchor Full Cream Milk Powder 1kg', 'Nido Milk Powder 400g'])

    # 8 — no-match and invalid-argument responses.

    def test_search_no_match_returns_clear_empty_payload(self):
        result, status = execute_tool('search_products', {'query': 'ghost item'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['products'], [])
        self.assertEqual(result['count'], 0)
        self.assertIn('message', result)
        result, _ = execute_tool('get_product_details', {'product_name': 'ghost item'}, self.customer)
        self.assertIsNone(result['product'])
        self.assertNotIn('candidates', result)
        self.assertIn('No matching active product', result['message'])

    def test_invalid_tool_arguments_fail_safely_with_a_clear_error(self):
        result, status = execute_tool(
            'search_products', {'query': 'milk', 'max_price': 'cheap'}, self.customer)
        self.assertEqual(status, 'failed')
        self.assertIn('max_price', result['error'])
        result, status = execute_tool(
            'compare_products', {'product_names': 'Nido'}, self.customer)
        self.assertEqual(status, 'failed')
        self.assertIn('product_names', result['error'])
        # Every failure is visible in server logs for diagnosis.
        with self.assertLogs('orders.services.agent', level='WARNING') as logs:
            execute_tool('search_products', {'query': 'milk', 'max_price': 'cheap'}, self.customer)
        self.assertTrue(any('search_products' in line for line in logs.output))

    # 9 — the agent-loop exhaustion that produced the old generic failure
    #     ("The chatbot could not complete this request...").

    def test_agent_loop_exhaustion_returns_graceful_fallback_not_generic_503(self):
        call = SimpleNamespace(name='search_products', args={'query': 'milk'}, id='loop')
        looping = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content='tool-request')])
        with patch('orders.services.agent.get_provider') as get_provider:
            provider = get_provider.return_value
            provider.make_contents.return_value = []
            provider.generate.side_effect = lambda contents, tools: looping
            provider.function_calls.side_effect = lambda response: response.function_calls
            with self.assertLogs('orders.services.agent', level='WARNING') as logs:
                result = run_agent('milk', self.customer)
        self.assertFalse(result['query_success'])
        self.assertIn('rephrase', result['bot_response'].lower())
        self.assertNotIn('simpler question', result['bot_response'])
        self.assertEqual(len(result['tool_calls']), 4)
        self.assertTrue(any('loop ended without a final answer' in line for line in logs.output))

    # 10 — permission boundaries unchanged.

    def test_permission_boundaries_for_public_and_manager_tools(self):
        # Anonymous: public search allowed, manager analytics still blocked.
        result, status = execute_tool('search_products', {'query': 'nido'}, None)
        self.assertEqual(status, 'completed')
        for manager_tool in ('get_low_stock_products', 'get_sales_summary', 'get_profit_margin'):
            result, status = execute_tool(manager_tool, {}, None)
            self.assertEqual(status, 'forbidden', manager_tool)
            self.assertIn('managers', result['error'])
        # Logged-in customer: same manager boundary as before.
        result, status = execute_tool('get_low_stock_products', {}, self.customer)
        self.assertEqual(status, 'forbidden')
        # Manager keeps access.
        manager = get_user_model().objects.create_user(username='mgr-match', password='password')
        group, _ = Group.objects.get_or_create(name='MANAGER')
        manager.groups.add(group)
        with patch('inventory.views.LowStockView') as low_stock_view:
            low_stock_view.return_value.get.return_value = SimpleNamespace(data={'products': []})
            result, status = execute_tool('get_low_stock_products', {}, manager)
        self.assertEqual(status, 'completed')

    # 11 — provider quota and failure handling.

    def test_provider_configuration_failure_keeps_its_mapped_message(self):
        with patch('orders.services.agent.get_provider') as get_provider:
            get_provider.return_value.make_contents.return_value = []
            get_provider.return_value.generate.side_effect = GeminiUnavailable(
                'Chatbot AI authentication is unavailable. Please contact an administrator.')
            with self.assertRaises(AgentUnavailable) as error:
                run_agent('milk', self.customer)
        self.assertEqual(error.exception.status_code, 503)
        self.assertIn('authentication', str(error.exception))


class ChatbotMatchingUnitTests(TestCase):
    """Pure matching-strategy checks that need no catalogue fixtures."""

    def test_normalization_handles_case_spacing_accents_and_punctuation(self):
        from orders.services.matching import normalize_text, tokenize
        self.assertEqual(normalize_text('  NESTLÉ’S  Cocoa  '), 'nestle s cocoa')
        self.assertEqual(tokenize('1 kg of Sugar'), ['1kg', 'sugar'])
        self.assertEqual(tokenize('milk powders'), tokenize('Milk Powder'))

    def test_fuzzy_token_matching_is_conservative(self):
        from orders.services.matching import tokens_match
        self.assertTrue(tokens_match('poweder', 'powder'))
        self.assertTrue(tokens_match('choclate', 'chocolate'))
        self.assertFalse(tokens_match('milk', 'milo'))     # no cross-type bleed
        self.assertFalse(tokens_match('sugar', 'salt'))


class ChatbotProductTypeTests(TestCase):
    """Product-type resolution and cross-type distinction checks."""

    def setUp(self):
        self.customer = Customer.objects.create(
            name='Type Customer', email='type@example.com', password_hash='unused')
        self.powder_category = Category.objects.create(category_name='Milk Powder')
        self.dairy_category = Category.objects.create(category_name='Dairy')
        self.malt_category = Category.objects.create(category_name='Malt Drinks')
        self.general_category = Category.objects.create(category_name='General')

        self.nido_powder = Product.objects.create(
            product_name='Nido Milk Powder 400g', category=self.powder_category,
            unit_price=1200, cost_price=1000, is_active=True)
        self.anchor_powder = Product.objects.create(
            product_name='Anchor Full Cream Milk Powder 1kg', category=self.powder_category,
            unit_price=1500, cost_price=1250, is_active=True)
        self.uht_milk = Product.objects.create(
            product_name='Ambewela UHT Milk 1L', category=self.dairy_category,
            unit_price=520, cost_price=430, is_active=True)
        self.coconut_milk = Product.objects.create(
            product_name='Colo Coconut Milk 400ml', category=self.general_category,
            unit_price=220, cost_price=180, is_active=True)
        self.milk_chocolate = Product.objects.create(
            product_name='Ritzbury Milk Chocolate 93g', category=self.malt_category,
            unit_price=270, cost_price=200, is_active=True)
        self.yoghurt = Product.objects.create(
            product_name='Pelwatte Yoghurt 80g', category=self.general_category,
            unit_price=65, cost_price=50, is_active=True)
        self.drinking_yoghurt = Product.objects.create(
            product_name='Anchor Drinking Yoghurt 200ml', category=self.general_category,
            unit_price=140, cost_price=110, is_active=True)

    def test_milk_products_distinguishes_types(self):
        """Broad 'milk products' search should not filter to one type only."""
        result, status = execute_tool('search_products', {'query': 'milk products'}, self.customer)
        self.assertEqual(status, 'completed')
        names = [p['product_name'] for p in result['products']]
        self.assertIn('Nido Milk Powder 400g', names)
        self.assertIn('Ambewela UHT Milk 1L', names)
        self.assertIn('Colo Coconut Milk 400ml', names)
        self.assertNotIn('Ritzbury Milk Chocolate 93g', names)

    def test_milk_family_search_excludes_confectionery_and_bakery_keyword_hits(self):
        chocolate_bar = Product.objects.create(
            product_name='Ritzbury Milk Bar 50g', category=Category.objects.create(category_name='Chocolate'),
            unit_price=150, cost_price=100, is_active=True)
        shortcake = Product.objects.create(
            product_name='Munchee Milk Shortcake', category=Category.objects.create(category_name='Bakery'),
            unit_price=250, cost_price=180, is_active=True)
        result, status = execute_tool('search_products', {'query': 'milk products'}, self.customer)
        self.assertEqual(status, 'completed')
        names = [p['product_name'] for p in result['products']]
        self.assertNotIn(chocolate_bar.product_name, names)
        self.assertNotIn(shortcake.product_name, names)

    def test_milk_powder_excludes_coconut_and_chocolate(self):
        """Milk powder search must not return coconut milk or milk chocolate."""
        result, _ = execute_tool('search_products', {'query': 'milk powder'}, self.customer)
        names = [p['product_name'] for p in result['products']]
        self.assertIn('Nido Milk Powder 400g', names)
        self.assertIn('Anchor Full Cream Milk Powder 1kg', names)
        self.assertNotIn('Colo Coconut Milk 400ml', names)
        self.assertNotIn('Ritzbury Milk Chocolate 93g', names)
        self.assertNotIn('Ambewela UHT Milk 1L', names)

    def test_coconut_milk_distinguished_from_dairy_milk(self):
        """'coconut milk' returns coconut milk products, not dairy milk."""
        result, _ = execute_tool('search_products', {'query': 'coconut milk'}, self.customer)
        names = [p['product_name'] for p in result['products']]
        self.assertIn('Colo Coconut Milk 400ml', names)
        self.assertNotIn('Ambewela UHT Milk 1L', names)
        self.assertNotIn('Nido Milk Powder 400g', names)

    def test_milk_chocolate_distinguished_from_milk_powder(self):
        """'milk chocolate' returns chocolate, not milk powder."""
        result, _ = execute_tool('search_products', {'query': 'milk chocolate'}, self.customer)
        names = [p['product_name'] for p in result['products']]
        self.assertIn('Ritzbury Milk Chocolate 93g', names)
        self.assertNotIn('Nido Milk Powder 400g', names)

    def test_liquid_milk_distinguished_from_milk_powder(self):
        """'uht milk' returns liquid milk, not milk powder or yoghurt."""
        result, _ = execute_tool('search_products', {'query': 'uht milk'}, self.customer)
        names = [p['product_name'] for p in result['products']]
        self.assertIn('Ambewela UHT Milk 1L', names)
        self.assertNotIn('Nido Milk Powder 400g', names)
        self.assertNotIn('Pelwatte Yoghurt 80g', names)

    def test_yoghurt_distinguished_from_milk_powder(self):
        """'yoghurt' returns yoghurt products, not milk powder."""
        result, _ = execute_tool('search_products', {'query': 'yoghurt'}, self.customer)
        names = [p['product_name'] for p in result['products']]
        self.assertIn('Pelwatte Yoghurt 80g', names)
        self.assertNotIn('Nido Milk Powder 400g', names)

    def test_drinking_yoghurt_not_confused_with_milk_powder(self):
        """'drinking yoghurt' returns drinking yoghurt, not milk powder."""
        result, _ = execute_tool('search_products', {'query': 'drinking yoghurt'}, self.customer)
        names = [p['product_name'] for p in result['products']]
        self.assertIn('Anchor Drinking Yoghurt 200ml', names)
        self.assertNotIn('Anchor Full Cream Milk Powder 1kg', names)

    def test_brand_specific_milk_powder_search(self):
        """Brand-specific milk powder search returns only that brand's powders."""
        result, _ = execute_tool('search_products', {'query': 'nido milk powder'}, self.customer)
        names = [p['product_name'] for p in result['products']]
        self.assertEqual(names, ['Nido Milk Powder 400g'])

    def test_missing_category_uses_name_evidence_conservative_fallback(self):
        """Products without a helpful category are still classified by name."""
        Product.objects.create(
            product_name='Anchor Milk Powder 250g', category=None,
            unit_price=400, cost_price=320, is_active=True)
        result, _ = execute_tool('search_products', {'query': 'anchor milk powder'}, self.customer)
        names = [p['product_name'] for p in result['products']]
        self.assertIn('Anchor Milk Powder 250g', names)

    def test_category_name_variations_do_not_weaken_type_filter(self):
        """'milk powders' plural and spacing variants still filter to powders."""
        expected = ['Anchor Full Cream Milk Powder 1kg', 'Nido Milk Powder 400g']
        for query in ('milk powders', 'milk  powders', 'powdered milk'):
            with self.subTest(query=query):
                result, status = execute_tool('search_products', {'query': query}, self.customer)
                self.assertEqual(status, 'completed')
                self.assertEqual(sorted([p['product_name'] for p in result['products']]),
                                 sorted(expected))

    def test_cheapest_milk_powder_compares_only_powders(self):
        """Cheapest milk powder must not be a chocolate malt drink."""
        result, status = execute_tool('find_cheapest_product', {'query': 'milk powder'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['product']['product_name'], 'Nido Milk Powder 400g')
        self.assertEqual(result['product']['selling_price'], 1200.0)
        self.assertNotIn('Ambewela UHT Milk 1L',
                         [alt['product_name'] for alt in result['alternatives']])


    def test_pack_size_and_unit_in_resolution(self):
        """Size tokens are preserved and used to distinguish products."""
        Product.objects.create(
            product_name='Nido Milk Powder 900g', category=self.powder_category,
            unit_price=2400, cost_price=2000, is_active=True)
        result, status = execute_tool(
            'search_products', {'query': 'nido milk powder 400g'}, self.customer)
        self.assertEqual(status, 'completed')
        names = [p['product_name'] for p in result['products']]
        self.assertEqual(names, ['Nido Milk Powder 400g'])

    def test_cross_type_comparison_refused(self):
        """Comparing milk powder with yoghurt returns structured refusal."""
        result, status = execute_tool(
            'compare_product_sales',
            {'product_a': 'nido milk powder', 'product_b': 'pelwatte yoghurt'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertIsNone(result['better_selling'])
        self.assertIsNone(result['difference_units'])
        self.assertIn('different product types', result['message'])

    def test_same_type_comparison_works(self):
        """Different-size powders are disclosed rather than compared silently."""
        ItemSalesRecord.objects.create(
            product=self.nido_powder, sale_date=local_today() - timedelta(days=1),
            quantity_sold=30, unit_price='1200.00', total_amount='36000.00')
        ItemSalesRecord.objects.create(
            product=self.anchor_powder, sale_date=local_today() - timedelta(days=1),
            quantity_sold=50, unit_price='1500.00', total_amount='75000.00')
        result, status = execute_tool(
            'compare_product_sales',
            {'product_a': 'nido milk powder', 'product_b': 'anchor milk powder'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertIsNone(result['better_selling'])
        self.assertIn('different pack sizes', result['message'])

    def test_equivalent_size_same_type_sales_comparison_works(self):
        anchor_400 = Product.objects.create(
            product_name='Anchor Milk Powder 400g', category=self.powder_category,
            unit_price=1200, cost_price=1000, is_active=True)
        ItemSalesRecord.objects.create(
            product=self.nido_powder, sale_date=local_today() - timedelta(days=1),
            quantity_sold=30, unit_price='1200.00', total_amount='36000.00')
        ItemSalesRecord.objects.create(
            product=anchor_400, sale_date=local_today() - timedelta(days=1),
            quantity_sold=50, unit_price='1200.00', total_amount='60000.00')
        result, status = execute_tool(
            'compare_product_sales',
            {'product_a': 'nido milk powder 400g', 'product_b': 'anchor milk powder 400g'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['better_selling'], anchor_400.product_name)
        self.assertEqual(result['difference_units'], 20)

    def test_rice_products_generalize(self):
        """Rice search works as an unrelated product family."""
        rice_category = Category.objects.create(category_name='Rice & Grains')
        Product.objects.create(
            product_name='Samba Rice 5kg', category=rice_category,
            unit_price=550, cost_price=450, is_active=True)
        Product.objects.create(
            product_name='Nadu Rice 1kg', category=rice_category,
            unit_price=120, cost_price=95, is_active=True)
        result, status = execute_tool('search_products', {'query': 'rice'}, self.customer)
        self.assertEqual(status, 'completed')
        names = [p['product_name'] for p in result['products']]
        self.assertIn('Samba Rice 5kg', names)
        self.assertIn('Nadu Rice 1kg', names)

    def test_ordinary_rice_excludes_rice_pasta(self):
        rice_category = Category.objects.create(category_name='Dry Groceries')
        ordinary = Product.objects.create(
            product_name='Samba Rice 5kg', category=rice_category,
            unit_price=550, cost_price=450, is_active=True)
        pasta = Product.objects.create(
            product_name='Raigam Rice Pasta Penne', category=rice_category,
            unit_price=190, cost_price=150, is_active=True)
        result, status = execute_tool('search_products', {'query': 'rice products'}, self.customer)
        self.assertEqual(status, 'completed')
        names = [p['product_name'] for p in result['products']]
        self.assertIn(ordinary.product_name, names)
        self.assertNotIn(pasta.product_name, names)

    def test_comparison_rejects_mixed_types_and_discloses_pack_size(self):
        result, status = execute_tool('compare_products', {
            'product_names': ['Nido Milk Powder 400g', 'Pelwatte Yoghurt 80g']}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertIn('different product types', result['message'])

        result, status = execute_tool('compare_products', {
            'product_names': ['Nido Milk Powder 400g', 'Anchor Full Cream Milk Powder 1kg']}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertIn('different pack sizes', result['message'])

    def test_existing_ratthi_anchor_400g_records_remain_comparable(self):
        Product.objects.create(product_name='RATTHI 400g', category=self.general_category,
                               unit_price=1200, cost_price=1000, is_active=True)
        Product.objects.create(product_name='ANCHOR 400G', category=self.general_category,
                               unit_price=1150, cost_price=950, is_active=True)
        result, status = execute_tool('compare_products', {
            'product_names': ['RATTHI 400g', 'ANCHOR 400G'], 'product_type': 'milk_powder'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual([p['product_name'] for p in result['products']], ['RATTHI 400g', 'ANCHOR 400G'])
        self.assertIn('enough type information', result['message'])

    def test_cooking_oil_products_generalize(self):
        """Cooking oil search works as another unrelated product family."""
        oil_category = Category.objects.create(category_name='Cooking Oils')
        Product.objects.create(
            product_name='Fortune Vegetable Oil 1L', category=oil_category,
            unit_price=400, cost_price=300, is_active=True)
        Product.objects.create(
            product_name='Sunflower Oil 1L', category=oil_category,
            unit_price=350, cost_price=250, is_active=True)
        result, status = execute_tool('search_products', {'query': 'cooking oil'}, self.customer)
        self.assertEqual(status, 'completed')
        names = [p['product_name'] for p in result['products']]
        self.assertIn('Fortune Vegetable Oil 1L', names)
        self.assertIn('Sunflower Oil 1L', names)



class ChatbotProductResolutionTests(TestCase):
    """Resolution, ranking and ambiguity checks using controlled fixtures."""

    def setUp(self):
        self.customer = Customer.objects.create(
            name='Resolution Customer', email='resolution@example.com', password_hash='unused')
        self.category = Category.objects.create(category_name='Milk Powder')

    def test_exact_match_outranks_fuzzy(self):
        Product.objects.create(
            product_name='Anchor Full Cream Milk Powder 1kg', category=self.category,
            unit_price=1500, cost_price=1250, is_active=True)
        Product.objects.create(
            product_name='Anchor Milk Powder Drink 500g', category=self.category,
            unit_price=900, cost_price=700, is_active=True)
        product = _resolve_product('anchor full cream milk powder 1kg')
        self.assertIsNotNone(product)
        self.assertEqual(product.product_name, 'Anchor Full Cream Milk Powder 1kg')

    def test_ambiguous_resolution_returns_none_not_guess(self):
        Product.objects.create(
            product_name='Anchor Milk Powder 1kg', category=self.category,
            unit_price=1500, cost_price=1250, is_active=True)
        Product.objects.create(
            product_name='Anchor Milk Powder 500g', category=self.category,
            unit_price=900, cost_price=700, is_active=True)
        product = _resolve_product('anchor milk powder')
        self.assertIsNone(product)  # must not guess

    def test_ambiguous_details_returns_candidates_for_clarification(self):
        Product.objects.create(
            product_name='Anchor Milk Powder 1kg', category=self.category,
            unit_price=1500, cost_price=1250, is_active=True)
        Product.objects.create(
            product_name='Anchor Milk Powder 500g', category=self.category,
            unit_price=900, cost_price=700, is_active=True)
        result, status = execute_tool(
            'get_product_details', {'product_name': 'anchor milk powder'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertIsNone(result['product'])
        self.assertEqual(len(result['candidates']), 2)

    def test_duplicate_exact_names_are_ambiguous(self):
        Product.objects.create(product_name='Anchor Milk Powder 1kg', category=self.category,
                               unit_price=1500, cost_price=1250, is_active=True)
        Product.objects.create(product_name='Anchor Milk Powder 1kg', category=self.category,
                               unit_price=1510, cost_price=1260, is_active=True)
        product, candidates = _resolve_product_with_candidates('Anchor Milk Powder 1kg')
        self.assertIsNone(product)
        self.assertEqual(len(candidates), 2)

    def test_search_does_not_drop_relevant_matches_at_fixed_twenty_limit(self):
        for idx in range(25):
            Product.objects.create(product_name=f'Samba Rice {idx + 1}kg', category=self.category,
                                   unit_price=100 + idx, cost_price=80, is_active=True)
        result, status = execute_tool('search_products', {'query': 'rice'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['count'], 25)

    def test_strict_resolution_for_price_lookup(self):
        """get_product_details uses strict resolution, not fuzzy guessing."""
        Product.objects.create(
            product_name='Anchor Milk Powder 1kg', category=self.category,
            unit_price=1500, cost_price=1250, is_active=True)
        result, status = execute_tool(
            'get_product_details', {'product_name': 'anchor milk powder 1kg'}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['product']['product_name'], 'Anchor Milk Powder 1kg')

    def test_stock_tool_resolves_typo(self):
        Product.objects.create(
            product_name='Nido Milk Powder 400g', category=self.category,
            unit_price=1200, cost_price=1000, is_active=True)
        with patch('orders.services.agent.get_available_stock', return_value=5):
            result, status = execute_tool(
                'check_purchase_quantity', {'product_name': 'nidoo', 'quantity': 2}, self.customer)
        self.assertEqual(status, 'completed')
        self.assertEqual(result['product'], 'Nido Milk Powder 400g')

    def test_invalid_arguments_return_clear_errors(self):
        result, status = execute_tool(
            'search_products', {'query': 'milk', 'max_price': 'cheap'}, self.customer)
        self.assertEqual(status, 'failed')
        self.assertIn('max_price', result['error'])

        result, status = execute_tool('compare_products', {'product_names': 'Nido'}, self.customer)
        self.assertEqual(status, 'failed')
        self.assertIn('product_names', result['error'])

        result, status = execute_tool('find_cheapest_product', {'query': ''}, self.customer)
        self.assertEqual(status, 'completed')  # empty query is neutral -> lists




class ChatbotAgentToolSelectionTests(TestCase):
    """Agent tool-selection and conversational behaviour tests."""

    def setUp(self):
        self.customer = Customer.objects.create(
            name='Agent Customer', email='agent@example.com', password_hash='unused')
        self.factory = APIRequestFactory()

    @patch('orders.services.agent.get_provider')
    def test_agent_selects_search_for_product_type(self, get_provider):
        call = SimpleNamespace(name='search_products', args={'query': 'milk powder'}, id='call-1')
        first = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content='tool-request')])
        second = SimpleNamespace(function_calls=[], text='Here are the milk powders.')
        provider = get_provider.return_value
        provider.make_contents.return_value = []
        provider.generate.side_effect = [first, second]
        provider.function_calls.side_effect = lambda response: response.function_calls
        provider.response_text.side_effect = lambda response: response.text
        result = run_agent('what milk powders are available', self.customer)
        self.assertEqual(result['tool_calls'], [{'name': 'search_products', 'status': 'completed'}])

    @patch('orders.services.agent.get_provider')
    def test_agent_selects_cheapest_for_product_type(self, get_provider):
        call = SimpleNamespace(name='find_cheapest_product', args={'query': 'milk powder'}, id='call-1')
        first = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content='tool-request')])
        second = SimpleNamespace(function_calls=[], text='The cheapest is Nido.')
        provider = get_provider.return_value
        provider.make_contents.return_value = []
        provider.generate.side_effect = [first, second]
        provider.function_calls.side_effect = lambda response: response.function_calls
        provider.response_text.side_effect = lambda response: response.text
        result = run_agent('cheapest milk powder', self.customer)
        self.assertEqual(result['tool_calls'], [{'name': 'find_cheapest_product', 'status': 'completed'}])

    @patch('orders.services.agent.get_provider')
    def test_agent_refuses_cross_type_comparison(self, get_provider):
        call = SimpleNamespace(name='compare_product_sales',
                               args={'product_a': 'nido milk powder', 'product_b': 'pelwatte yoghurt'}, id='call-1')
        first = SimpleNamespace(function_calls=[call], candidates=[SimpleNamespace(content='tool-request')])
        second = SimpleNamespace(function_calls=[], text='Cannot compare different product types.')
        provider = get_provider.return_value
        provider.make_contents.return_value = []
        provider.generate.side_effect = [first, second]
        provider.function_calls.side_effect = lambda response: response.function_calls
        provider.response_text.side_effect = lambda response: response.text
        result = run_agent('compare nido milk powder and pelwatte yoghurt', self.customer)
        self.assertEqual(result['tool_calls'], [{'name': 'compare_product_sales', 'status': 'completed'}])
        self.assertIn('different product types', result['bot_response'])

    @patch('orders.services.agent.get_provider')
    def test_anonymous_cannot_access_manager_tool(self, get_provider):
        from orders.services.agent import is_anonymous, PUBLIC_TOOLS, MANAGER_TOOLS
        self.assertTrue(is_anonymous(None))
        self.assertTrue(MANAGER_TOOLS.isdisjoint(PUBLIC_TOOLS))
