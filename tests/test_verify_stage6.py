import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('verify_stage6', Path(__file__).resolve().parents[1] / 'scripts/verify_stage6.py')
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


class VerificationTest(unittest.TestCase):
    def test_xml_comments_attribute_order_and_only_wall_duration_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / 'sumo_summary.xml'
            a.write_text('<summary><!-- generated --><step duration="1" time="0"/></summary>')
            first = v.xml_signature(a)
            a.write_text('<summary>\n<step time="0" duration="9"/>\n</summary>')
            second = v.xml_signature(a)
            self.assertEqual(first['sha256'], second['sha256'])
            self.assertNotEqual(first['wall_duration_sha256'], second['wall_duration_sha256'])
            a.write_text('<summary><step time="1" duration="9"/></summary>')
            self.assertNotEqual(second['sha256'], v.xml_signature(a)['sha256'])
            b = Path(tmp) / 'tripinfo.xml'
            b.write_text('<tripinfos><tripinfo duration="1"/></tripinfos>')
            sig = v.xml_signature(b)
            b.write_text('<tripinfos><tripinfo duration="2"/></tripinfos>')
            self.assertNotEqual(sig['sha256'], v.xml_signature(b)['sha256'])

    def test_cohort_source_wait_and_missing_or_duplicate_vehicle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            demand = root / 'demand.xml'
            demand.write_text('<routes><vehicle id="M_flow.0" depart="2"/></routes>')
            (root / 'tripinfo.xml').write_text('<tripinfos><tripinfo id="M_flow.0" depart="5" arrival="12" duration="7" departDelay="3"/></tripinfos>')
            (root / 'vehroute.xml').write_text('<routes><vehicle id="M_flow.0" depart="5" arrival="12"/></routes>')
            self.assertEqual(v.cohort(root, demand, {'M': 1})['M'],
                             {'requested': 1, 'system_seconds': '10', 'source_wait_seconds': '3'})
            (root / 'vehroute.xml').write_text('<routes/>')
            with self.assertRaisesRegex(ValueError, 'ID sets'):
                v.cohort(root, demand, {'M': 1})
            demand.write_text('<routes><vehicle id="M_flow.0"/><vehicle id="M_flow.0"/></routes>')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                v.records(demand, 'vehicle')

    def test_csv_exact_values_order_and_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.csv'
            path.write_text('id,value\na,1\nb,2\n')
            first = v.csv_signature(path)
            path.write_text('id,value\nb,2\na,1\n')
            self.assertNotEqual(first['sha256'], v.csv_signature(path)['sha256'])
            path.write_text('id,value\na,1,extra\n')
            with self.assertRaisesRegex(ValueError, 'width'):
                v.csv_signature(path)


if __name__ == '__main__':
    unittest.main()
