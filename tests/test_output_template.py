import datetime
import unittest
from unittest.mock import patch

import pytz

from config_util import build_dated_output_template


class TestDatedOutputTemplate(unittest.TestCase):
    def test_prefixes_template_with_zero_padded_local_datetime(self):
        fixed_time = datetime.datetime(
            2026,
            8,
            4,
            1,
            1,
            tzinfo=pytz.timezone('Asia/Shanghai'),
        )

        with patch('config_util.datetime') as mocked_datetime:
            mocked_datetime.now.return_value = fixed_time
            result = build_dated_output_template(
                '%(title)s.%(ext)s',
                'Asia/Shanghai',
            )

        self.assertEqual(result, '08040101-%(title)s.%(ext)s')


if __name__ == '__main__':
    unittest.main()
