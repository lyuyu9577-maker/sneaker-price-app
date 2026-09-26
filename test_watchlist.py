import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import watchlist as w


class WatchlistTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'watch.json'
        self.target = dict(platform='PChome', product_id='DICF9I-A900HXYZZ', query='Jordan 1 Low')
        self.sample = dict(self.target, title='Jordan 1 Low', url='https://24h.pchome.com.tw/prod/DICF9I-A900HXYZZ')

    def test_register_persists_and_deduplicates(self):
        with patch('watchlist.collect_fixed', return_value=([self.sample], {})), patch('tracking.save_observations') as save:
            w.register(self.target, self.path)
            self.assertEqual(len(w.load_watchlist(self.path)), 1)
            w.register(self.target, self.path)
            self.assertEqual(save.call_count, 1)

    def test_missing_product_not_registered(self):
        with patch('watchlist.collect_fixed', return_value=([], {})), patch('tracking.save_observations') as save:
            with self.assertRaises(ValueError):
                w.register(self.target, self.path)
            self.assertEqual(w.load_watchlist(self.path), [])
            save.assert_not_called()

    def test_cap_checked_before_fetch(self):
        self.path.write_text(json.dumps([dict(self.sample, product_id=f'DICF9I-A900X{i}') for i in range(20)]), encoding='utf-8')
        with patch('watchlist.collect_fixed') as fetch:
            with self.assertRaises(ValueError):
                w.register(self.target, self.path)
            fetch.assert_not_called()

    def test_exact_product_fallback(self):
        with patch('tracking.collect_platform', side_effect=[([{'product_id':'OTHER'}], {}), ([{'product_id':self.target['product_id']}], {})]):
            rows, status = w.collect_fixed(self.target)
            self.assertEqual(rows[0]['query'], self.target['query'])
            self.assertEqual(status['status'], 'ok')

    def test_never_substitute_other_product(self):
        with patch('tracking.collect_platform', side_effect=[([{'product_id':'OTHER'}], {}), ([], {})]):
            rows, status = w.collect_fixed(self.target)
            self.assertEqual(rows, [])
            self.assertEqual(status['status'], 'no_matches')

    def test_reject_invalid_input(self):
        for changes in ({'platform':'unknown'}, {'product_id':'https://other.example'}, {'query':''}, {'query':'x'*101}):
            with self.assertRaises(ValueError):
                w.validate_request(dict(self.target, **changes))


if __name__ == '__main__':
    unittest.main()
