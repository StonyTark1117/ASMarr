"""Real exact-candidate hold preflight, with only generated isolated state."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ops'))
import maintenance_probe


@unittest.skipUnless((ROOT/'publish/ASMarr').exists(),'Publish self-contained application before hold-probe test')
class MaintenanceProbeTests(unittest.TestCase):
    def test_real_candidate_cannot_consume_or_recover_queued_work_during_probe(self):
        app=ROOT/'publish';commit=json.loads((app/'build-identity.json').read_text())['sourceCommit']
        result=maintenance_probe.probe(app,maintenance_probe.artifact_hash(app),commit)
        self.assertTrue(result['passed']);self.assertTrue(result['queuedWorkUnchanged'])
        self.assertTrue(result['providerExecutionDisabled']);self.assertFalse(result['productionDatabaseOpened'])

    def test_wrong_artifact_identity_rejected_before_any_process_launch(self):
        with patch.object(maintenance_probe.subprocess,'Popen',side_effect=AssertionError('must not start')):
            with self.assertRaises(ValueError):maintenance_probe.probe(ROOT/'publish','0'*64,'0'*40)


if __name__=='__main__':unittest.main()
