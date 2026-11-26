import importlib.util
from pathlib import Path
import re
import unittest

spec = importlib.util.spec_from_file_location('release_audit', Path(__file__).resolve().parents[1]/'scripts/release_audit.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class ReleaseAuditTests(unittest.TestCase):
    def test_generic_examples_and_loopback_allowed(self):
        self.assertEqual(audit.findings('/home/user/project test@example.invalid 192.0.2.1 127.0.0.1'), [])

    def test_credentials_report_category_without_matched_value(self):
        artificial = 'sk-' + 'X'*30
        result = audit.findings(artificial)
        self.assertEqual(result, [(1,'possible credential/private key')])
        self.assertNotIn(artificial,str(result))

    def test_private_path_address_and_identity_are_flagged(self):
        path = '/home/' + 'fixture-person/project'
        address = '.'.join(str(part) for part in (10,200,10,12))
        results = audit.findings(path+'\n'+address+'\n'+ 'fictional-name', [re.compile('fictional-name')])
        self.assertEqual([category for _,category in results], ['non-example home path','non-example IP address','current machine identity'])


if __name__ == '__main__': unittest.main()
