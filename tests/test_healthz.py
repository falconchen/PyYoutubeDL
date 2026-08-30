import unittest

from app import app


class TestHealthz(unittest.TestCase):
    def test_healthz_returns_ok_and_commit(self):
        with app.test_client() as client:
            response = client.get('/healthz')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['status'], 'ok')
        self.assertTrue(payload['commit'])


if __name__ == '__main__':
    unittest.main()
