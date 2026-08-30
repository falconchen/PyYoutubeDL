import unittest

from config_util import DEFAULT_CONFIG


class TestConfigDefaults(unittest.TestCase):
    def test_flask_port_defaults_to_5100(self):
        self.assertEqual(DEFAULT_CONFIG['FLASK_PORT'], 5100)

    def test_interrupted_download_resume_defaults_to_disabled(self):
        self.assertIs(DEFAULT_CONFIG['RESUME_INTERRUPTED_DOWNLOADS'], False)


if __name__ == '__main__':
    unittest.main()
