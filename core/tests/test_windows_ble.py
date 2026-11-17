import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_run.serial_link import LinkClosed
from agent_run.windows_ble import RX, SERVICE, WindowsBleLink, _Hello, _matches, _wait_for_hello, _write_frame


class DiscoveryTest(unittest.TestCase):
    def test_filters_other_nus_firmware_and_selects_exact_device(self):
        def adv(address, name, *uuids):
            return SimpleNamespace(address=address, name=None), SimpleNamespace(
                local_name=name, service_uuids=uuids)

        found = dict(a=adv("AA", "WorkBuddy-0001", SERVICE), b=adv("BB", "AgentRun-0002"),
                     c=adv("CC", "AgentRun-0003", SERVICE.upper()),
                     d=adv("DD", "AgentRun-0004", SERVICE))
        self.assertEqual([name for _, name in _matches(found, None)], ["AgentRun-0003", "AgentRun-0004"])
        self.assertEqual([dev.address for dev, _ in _matches(found, "agentrun-0003")], ["CC"])
        self.assertEqual([dev.address for dev, _ in _matches(found, "dd")], ["DD"])


class WireTest(unittest.IsolatedAsyncioTestCase):
    async def test_hello_requires_ar1_and_handles_fragmented_notifications(self):
        hello = _Hello()
        client = SimpleNamespace(write_gatt_char=AsyncMock())

        async def respond(characteristic, data, response):
            self.assertEqual((characteristic, data, response), (RX, b"AR1 PING\n", True))
            hello.receive(None, b'{"type":"connected"}\nAR1 HELLO fw=old proto=2\nAR1 HE')
            self.assertFalse(hello.ready.is_set())
            hello.receive(None, b"LLO fw=0.6.2-wb proto=1\r\n")

        client.write_gatt_char.side_effect = respond
        actual = await _wait_for_hello(client, RX, asyncio.Event(), hello)
        self.assertEqual(actual, {"type": "HELLO", "fw": "0.6.2-wb", "proto": "1"})

    async def test_submits_complete_long_frame_to_winrt_in_one_write(self):
        client = SimpleNamespace(write_gatt_char=AsyncMock())
        payload = b"AR1 S seq=17 wb=3 cx=1 ds=1 done=21 wn=2 w=WB:A,CX:Q tm=1342\n"
        await _write_frame(client, RX, payload)
        client.write_gatt_char.assert_awaited_once_with(RX, payload, response=True)


class ReconnectionTest(unittest.TestCase):
    def test_old_link_does_not_send_after_reconnect(self):
        old = SimpleNamespace(hello={"fw": "0.6.2-wb"})
        new = SimpleNamespace(hello={"fw": "0.6.2-wb"})

        class Bridge:
            session = None

            def current(self):
                return self.session

            def write(self, session, line, timeout):
                return session is not None and session is self.session

        bridge = Bridge()
        link = WindowsBleLink(bridge)
        self.assertIsNone(link.handshake())
        bridge.session = old
        self.assertIs(link.handshake(), old.hello)
        self.assertTrue(link.write_line("AR1 PING\n"))
        bridge.session = new
        with self.assertRaises(LinkClosed):
            link.read_lines()
        self.assertFalse(link.write_line("AR1 PING\n"))
        self.assertIs(WindowsBleLink(bridge).handshake(), new.hello)
