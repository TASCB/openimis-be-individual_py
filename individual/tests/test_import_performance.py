import unittest
from individual.import_performance import optimized_import, defer_reporting

class ReportingScopeTests(unittest.TestCase):
    def test_collects_once_and_resets_scope(self):
        calls = []
        class Service:
            def synchronize_data_for_reporting(self, uploads, _flush=False):
                calls.append((set(uploads), _flush))
        timings = {}
        self.assertFalse(defer_reporting('outside'))
        with optimized_import(Service(), timings):
            for upload in ('one', 'two', 'one'):
                self.assertTrue(defer_reporting(upload))
        self.assertEqual(calls, [({'one','two'}, True)])
        self.assertIn('reporting_seconds', timings)
        self.assertFalse(defer_reporting('outside'))

    def test_failed_import_does_not_report(self):
        class Service:
            def synchronize_data_for_reporting(self, *args, **kwargs):
                raise AssertionError('must not report a failed import')
        with self.assertRaises(ValueError):
            with optimized_import(Service(), {}):
                defer_reporting('upload')
                raise ValueError('failed batch')
        self.assertFalse(defer_reporting('outside'))
