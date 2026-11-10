import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from bot import Bot, Store, TelegramError, normalize


def payload(event='В пути'):
    return {'items': [{'event': event, 'moment': '2026-11-10T10:00:00Z'},
                      {'event': 'Доставлено', 'moment': None}],
            'deliveryDateBegin': '2026-11-15'}


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.db')
        self.store.set('owner', 123)
        self.store.set('enabled', True)
        self.tg = AsyncMock()
        self.tracker = AsyncMock()
        self.tracker.track = '0105952439-0043-1'
        self.tracker.fetch.return_value = normalize(payload())
        self.bot = Bot(self.tg, self.tracker, self.store, 900)

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_initial_change_and_restart_deduplication(self):
        await self.bot.check()
        await Bot(self.tg, self.tracker, self.store, 900).check()
        self.assertEqual(self.tg.send.await_count, 1)
        self.tracker.fetch.return_value = normalize(payload('На таможне'))
        await self.bot.check()
        self.assertEqual(self.tg.send.await_count, 2)

    async def test_failed_notification_is_retried(self):
        self.tg.send.side_effect = TelegramError('сеть')
        with self.assertRaises(TelegramError):
            await self.bot.check()
        self.assertIsNone(self.store.get('snapshot:' + self.tracker.track))
        self.tg.send.side_effect = None
        await self.bot.check()
        self.assertEqual(self.tg.send.await_count, 2)

    async def test_ozon_failure_preserves_last_status_and_warns(self):
        await self.bot.check()
        old = self.store.get('snapshot:' + self.tracker.track)
        self.tracker.fetch.side_effect = RuntimeError('antibot')
        for _ in range(4):
            await self.bot.check()
        self.assertEqual(self.store.get('snapshot:' + self.tracker.track), old)
        self.assertEqual(self.tg.send.await_count, 2)

    async def test_stop_during_fetch_suppresses_notification(self):
        async def stop():
            self.store.set('enabled', False)
            return normalize(payload())
        self.tracker.fetch.side_effect = stop
        await self.bot.check()
        self.tg.send.assert_not_awaited()

    async def test_other_chat_cannot_control_bot(self):
        await self.bot.command({'chat': {'id': 456, 'type': 'private'}, 'text': '/stop'})
        self.assertTrue(self.store.get('enabled'))


class PayloadTests(unittest.TestCase):
    def test_future_stages_and_extra_metadata_do_not_trigger_change(self):
        before = normalize(payload())
        after = payload()
        after['items'].append({'event': 'Другой будущий этап', 'moment': ''})
        after['requestId'] = 'random'
        after['items'].reverse()
        self.assertEqual(before, normalize(after))

    def test_completed_milestone_and_delivery_date_trigger_change(self):
        before = normalize(payload())
        after = payload()
        after['items'][1]['moment'] = '2026-11-11T10:00:00Z'
        self.assertNotEqual(before, normalize(after))
        after = payload()
        after['deliveryDateBegin'] = '2026-11-16'
        self.assertNotEqual(before, normalize(after))

    def test_error_payload_is_not_a_status(self):
        for data in ({'error': 'blocked'}, {'items': []}, {'items': [{'event': 'В пути'}]}):
            with self.assertRaises(ValueError):
                normalize(data)


if __name__ == '__main__':
    unittest.main()
