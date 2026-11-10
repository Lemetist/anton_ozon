"""Personal Ozon status monitor. Python 3.11+."""
import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('ozon')


def normalize(data):
    """Compare only meaningful, completed milestones, never future stages."""
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise ValueError('Ozon вернул неизвестный формат данных')
    events = []
    for item in data['items']:
        if not isinstance(item, dict):
            raise ValueError('Некорректное событие Ozon')
        event, moment = item.get('event'), item.get('moment')
        if moment:
            if not isinstance(event, str) or not event.strip():
                raise ValueError('У события нет названия')
            events.append({'event': event.strip(), 'moment': str(moment)})
    if not events:
        raise ValueError('В ответе Ozon пока нет состоявшихся событий')
    events.sort(key=lambda item: (item['moment'], item['event']))
    return {'events': events, 'delivery_begin': data.get('deliveryDateBegin'),
            'delivery_end': data.get('deliveryDateEnd')}


def summary(snapshot, track):
    event = snapshot['events'][-1]
    labels = {'CustomsClearanceCompleted': 'Таможенное оформление завершено'}
    def date(value, with_time=False):
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if dt.tzinfo:
                dt = dt.astimezone(ZoneInfo('Europe/Moscow'))
            return dt.strftime('%d.%m.%Y %H:%M МСК' if with_time else '%d.%m.%Y')
        except ValueError:
            return str(value)
    lines = [f'📦 Ozon · {track}', labels.get(event['event'], event['event']), date(event['moment'], True)]
    if snapshot.get('delivery_begin') or snapshot.get('delivery_end'):
        dates = dict.fromkeys(date(d) for d in
                              (snapshot.get('delivery_begin'), snapshot.get('delivery_end')) if d)
        lines.append('Ожидаемая доставка: ' + ' — '.join(dates))
    lines.append(f'https://tracking.ozon.ru/?track={track}')
    return '\n'.join(lines)[:3900]


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            (key, json.dumps(value, ensure_ascii=False)))


class Tracker:
    def __init__(self, track, data_dir):
        if not re.fullmatch(r'[0-9]+(?:-[0-9]+)*', track):
            raise ValueError('Некорректный OZON_TRACK_NUMBER')
        self.track, self.data_dir = track, data_dir

    async def fetch(self, headed=False):
        from playwright.async_api import async_playwright

        endpoint = f'/p-api/ozon-track-bff/tracking/{self.track}'
        async with async_playwright() as pw:
            options = {}
            if os.getenv('CHROMIUM_EXECUTABLE'):
                options['executable_path'] = os.environ['CHROMIUM_EXECUTABLE']
            context = await pw.chromium.launch_persistent_context(
                str(self.data_dir / 'browser'),
                headless=not (headed or os.getenv('OZON_HEADLESS', 'true').lower() == 'false'),
                ignore_default_args=['--enable-automation'],
                args=['--disable-blink-features=AutomationControlled'],
                locale='ru-RU', timezone_id='Europe/Moscow', **options)
            try:
                page = await context.new_page()
                result = asyncio.get_running_loop().create_future()

                async def capture(response):
                    parsed = urlparse(response.url)
                    if (parsed.hostname == 'tracking.ozon.ru' and parsed.path == endpoint
                            and response.status == 200 and not result.done()):
                        try:
                            raw = await response.json()
                            snapshot = normalize(raw)
                            if not result.done():
                                result.set_result(snapshot)
                        except (ValueError, TypeError) as exc:
                            LOG.warning('Ответ Ozon не распознан: %s', exc)

                page.on('response', capture)
                # Challenge redirects can interrupt goto; the response listener stays active.
                try:
                    await page.goto(f'https://tracking.ozon.ru/?track={self.track}',
                                    wait_until='domcontentloaded', timeout=45000)
                except Exception:
                    pass
                try:
                    return await asyncio.wait_for(result, timeout=120 if headed else 45)
                except asyncio.TimeoutError:
                    try:
                        await page.screenshot(path=str(self.data_dir / 'ozon-error.png'))
                        (self.data_dir / 'ozon-error.txt').write_text(
                            await page.locator('body').inner_text(timeout=3000), encoding='utf-8')
                    except Exception:
                        pass
                    raise RuntimeError('Нет данных Ozon: блокировка, сеть или изменившийся формат. '
                                       'Попробуйте python bot.py --login с открытым браузером.') from None
            finally:
                await context.close()


class TelegramError(Exception):
    def __init__(self, code, retry_after=5):
        self.code, self.retry_after = code, retry_after
        super().__init__(f'Telegram API: код {code}')


class Telegram:
    def __init__(self, token):
        self.client = httpx.AsyncClient(base_url=f'https://api.telegram.org/bot{token}/', timeout=45)

    async def call(self, method, **payload):
        try:
            response = await self.client.post(method, json=payload)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            # Do not put request URLs (containing the bot token) into logs.
            raise TelegramError('сеть') from None
        if not data.get('ok'):
            raise TelegramError(data.get('error_code', response.status_code),
                                data.get('parameters', {}).get('retry_after', 5))
        return data['result']

    async def send(self, chat_id, text):
        await self.call('sendMessage', chat_id=chat_id, text=text,
                        link_preview_options={'is_disabled': True})


class Bot:
    def __init__(self, telegram, tracker, store, interval):
        self.tg, self.tracker, self.store, self.interval = telegram, tracker, store, interval
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.last_check = 0.0
        self.failures = 0

    def owner(self):
        configured = os.getenv('TELEGRAM_CHAT_ID', '').strip()
        return int(configured) if configured else self.store.get('owner')

    async def check(self):
        async with self.lock:
            self.last_check = time.monotonic()
            try:
                snapshot = await self.tracker.fetch()
            except Exception as exc:
                self.failures += 1
                error_text = 'Ozon не вернул данные отправления.'
                diagnostic = self.tracker.data_dir / 'ozon-error.txt' if isinstance(self.tracker.data_dir, Path) else None
                if diagnostic and diagnostic.exists():
                    body = diagnostic.read_text(encoding='utf-8')
                    if 'Похоже, нет' in body and 'соединения' in body:
                        error_text = 'Ozon показывает «Похоже, нет соединения» и предлагает отключить VPN или сменить сеть.'
                self.store.set('last_error', error_text)
                LOG.warning('Проверка Ozon не удалась (%s)', type(exc).__name__)
                if (self.failures == 1 and self.store.get('enabled') and
                        not self.store.get('snapshot:' + self.tracker.track)):
                    await self.tg.send(self.owner(), '⚠️ Пока не удалось получить статус Ozon. '
                                       'Возможна антибот-проверка. Продолжу попытки автоматически.')
                if self.failures == 3 and self.store.get('enabled'):
                    await self.tg.send(self.owner(), '⚠️ Три проверки Ozon подряд не удались. '
                                       'Продолжаю попытки; последний известный статус сохранён.')
                return
            self.failures = 0
            self.store.set('last_error', None)
            if not self.store.get('enabled'):
                return
            key = 'snapshot:' + self.tracker.track
            previous = self.store.get(key)
            # Persist only after Telegram accepts the notification, so failed sends are retried.
            if snapshot != previous:
                title = '🔔 Статус отправления изменился\n\n' if previous else '✅ Отслеживание начато\n\n'
                await self.tg.send(self.owner(), title + summary(snapshot, self.tracker.track))
                self.store.set(key, snapshot)
            self.store.set('last_success', time.strftime('%Y-%m-%d %H:%M:%S %z'))

    async def command(self, message):
        if message.get('chat', {}).get('type') != 'private':
            return
        chat = message['chat']['id']
        command = message.get('text', '').split(' ', 1)[0].split('@')[0]
        if self.owner() is None:
            if command != '/start':
                await self.tg.send(chat, 'Нажмите /start для подключения.')
                return
            self.store.set('owner', chat)
        if chat != self.owner():
            await self.tg.send(chat, 'Это личный бот. Доступ разрешён только владельцу.')
            return
        if command == '/start':
            self.store.set('enabled', True)
            await self.tg.send(chat, f'Отслеживаю {self.tracker.track}. Проверка каждые '
                               f'{self.interval // 60} мин.\n/status — последний статус\n'
                               '/check — проверить сейчас\n/stop — выключить уведомления')
            self.wake.set()
        elif command == '/stop':
            self.store.set('enabled', False)
            await self.tg.send(chat, 'Уведомления выключены. Возобновить: /start')
            self.wake.set()
        elif command == '/status':
            snapshot = self.store.get('snapshot:' + self.tracker.track)
            text = summary(snapshot, self.tracker.track) if snapshot else 'Успешных проверок пока не было.'
            if self.store.get('last_error'):
                text += '\n⚠️ ' + self.store.get('last_error')
            await self.tg.send(chat, text + '\nПоследняя успешная проверка: ' +
                               self.store.get('last_success', 'ещё не выполнена') +
                               ('\nУведомления выключены.' if not self.store.get('enabled') else ''))
        elif command == '/check':
            if not self.store.get('enabled'):
                await self.tg.send(chat, 'Сначала включите отслеживание: /start')
            elif self.lock.locked() or time.monotonic() - self.last_check < 60:
                await self.tg.send(chat, 'Проверка уже идёт или была менее минуты назад.')
            else:
                await self.tg.send(chat, 'Проверяю. При изменении пришлю уведомление; результат — /status.')
                self.wake.set()

    async def poll(self):
        while True:
            try:
                updates = await self.tg.call('getUpdates', offset=self.store.get('offset', 0),
                                             timeout=30, allowed_updates=['message'])
                for update in updates:
                    await self.command(update.get('message', {}))
                    self.store.set('offset', update['update_id'] + 1)
            except TelegramError as exc:
                LOG.warning('%s', exc)
                if exc.code in (401, 409):
                    raise RuntimeError('Проверьте токен и отсутствие второго экземпляра или webhook.') from None
                await asyncio.sleep(max(5, exc.retry_after))

    async def monitor(self):
        while True:
            self.wake.clear()
            if self.owner() and self.store.get('enabled'):
                try:
                    await self.check()
                except TelegramError as exc:
                    LOG.warning('Уведомление не доставлено: %s', exc)
                    if exc.code == 403:
                        self.store.set('enabled', False)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass


async def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Проверить Ozon без Telegram')
    parser.add_argument('--login', action='store_true', help='Открыть браузер для ручной проверки Ozon')
    args = parser.parse_args()
    data_dir = ROOT / 'data'
    data_dir.mkdir(exist_ok=True, mode=0o700)
    tracker = Tracker(os.getenv('OZON_TRACK_NUMBER', '0105952439-0043-1'), data_dir)
    if args.once or args.login:
        print(summary(await tracker.fetch(headed=args.login), tracker.track))
        return
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise RuntimeError('Укажите TELEGRAM_BOT_TOKEN в .env (см. .env.example)')
    interval = max(60, int(os.getenv('CHECK_INTERVAL_SECONDS', '900')))
    tg = Telegram(token)
    store = Store(data_dir / 'state.sqlite3')
    try:
        me = await tg.call('getMe')
        LOG.info('Бот @%s запущен. Отправьте ему /start.', me['username'])
        bot = Bot(tg, tracker, store, interval)
        async with asyncio.TaskGroup() as group:
            group.create_task(bot.poll())
            group.create_task(bot.monitor())
    finally:
        await tg.client.aclose()
        store.db.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        LOG.error('%s', error)
        raise SystemExit(1) from None
