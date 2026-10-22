"""Camera confirmation must tolerate unavailable observations within its deadline."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from run_mcp import Client


class CameraWait(unittest.TestCase):
    arguments = {'id': 'device', 'run': 'current', 'path': None}

    def client(self, *states):
        return SimpleNamespace(tool=Mock(side_effect=[{'status': 'pending'}, *states]))

    def state(self, *, run='current', finished=False):
        return {'runtime': {'session': {'output': run, 'finished': finished,
                                       'camera_change': {'status': 'applied'}}}}

    @patch('run_mcp.time.sleep')
    def test_unavailable_observation_does_not_resubmit_selection(self, _):
        client = self.client({'runtime': None}, self.state())
        self.assertEqual(Client.camera(client, self.arguments), {'status': 'applied'})
        self.assertEqual([call.args[0] for call in client.tool.call_args_list],
                         ['lisem_camera_input', 'lisem_status', 'lisem_status'])

    @patch('run_mcp.time.sleep')
    def test_finished_run_does_not_confirm_selection(self, _):
        with self.assertRaises(AssertionError):
            Client.camera(self.client(self.state(finished=True)), self.arguments)

    @patch('run_mcp.time.sleep')
    def test_replacement_run_does_not_confirm_selection(self, _):
        with self.assertRaises(AssertionError):
            Client.camera(self.client(self.state(run='replacement')), self.arguments)

    @patch('run_mcp.time.sleep')
    @patch('run_mcp.time.monotonic', side_effect=[0, 1, 16])
    def test_unavailable_observation_keeps_original_deadline(self, *_):
        with self.assertRaisesRegex(AssertionError, 'never acknowledged'):
            Client.camera(self.client({'runtime': None}), self.arguments)
