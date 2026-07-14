from django.test import TestCase


class APITest(TestCase):

    def test_status_api(self):
        response = self.client.get('/api/status/')

        self.assertEqual(response.status_code, 200)