"""YouTube -> Discord notifier. Python 3.10+, no dependencies."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parent
URL = 'https://www.youtube.com/@РоманБобукс'
NS = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}

def config():
    path = BASE / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))
    return os.environ

def request(url, payload=None, token=None):
    headers = {'User-Agent': 'BobuksNotifier/1.0'}
    if token:
        headers['Authorization'] = 'Bot ' + token
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    data = None if payload is None else json.dumps(payload).encode()
    for attempt in range(5):
        try:
            with urlopen(Request(url, data=data, headers=headers), timeout=30) as response:
                return response.read()
        except HTTPError as error:
            if error.code == 429:
                body = json.loads(error.read())
                time.sleep(max(1, float(body.get('retry_after', 5))))
            elif error.code >= 500 and attempt < 4:
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f'HTTP {error.code}: перевірте доступ, токен та ID каналу.') from None
    raise RuntimeError('Сервіс тимчасово обмежив запити. Спробуємо пізніше.')

def resolve_channel(env):
    channel = env.get('YOUTUBE_CHANNEL_ID', '').strip()
    if not channel:
        html = request(quote(URL, safe=':/@')).decode('utf-8')
        patterns = [r'"externalId"\s*:\s*"(UC[\w-]{22})"',
                    r'https://www.youtube.com/channel/(UC[\w-]{22})']
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                channel = match.group(1)
                break
    if not re.fullmatch(r'UC[\w-]{22}', channel):
        raise ValueError('Не визначено канал. Вкажіть YOUTUBE_CHANNEL_ID у .env.')
    return channel

def parse_feed(data, channel):
    root = ET.fromstring(data)
    if root.tag != '{' + NS['a'] + '}feed' or root.findtext('yt:channelId', namespaces=NS) != channel:
        raise ValueError('YouTube повернув неочікувану стрічку.')
    videos = []
    for entry in root.findall('a:entry', NS):
        video = entry.findtext('yt:videoId', namespaces=NS)
        published = entry.findtext('a:published', namespaces=NS)
        if not video or not re.fullmatch(r'[\w-]{11}', video) or not published:
            raise ValueError('Некоректний запис YouTube RSS.')
        videos.append((published, video))
    return [video for _, video in sorted(videos)]

def database(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS seen (channel TEXT, video TEXT, PRIMARY KEY(channel,video))')
    db.execute('CREATE TABLE IF NOT EXISTS initialized (channel TEXT PRIMARY KEY)')
    return db

def process(db, channel, videos, send):
    if not db.execute('SELECT 1 FROM initialized WHERE channel=?', (channel,)).fetchone():
        with db:
            db.executemany('INSERT OR IGNORE INTO seen VALUES (?,?)', [(channel, v) for v in videos])
            db.execute('INSERT INTO initialized VALUES (?)', (channel,))
        logging.info('Перший запуск: збережено %s старих відео. Очікуємо нові.', len(videos))
        return
    for video in videos:
        if db.execute('SELECT 1 FROM seen WHERE channel=? AND video=?', (channel, video)).fetchone():
            continue
        send(video)
        with db:
            db.execute('INSERT INTO seen VALUES (?,?)', (channel, video))
        logging.info('Надіслано відео %s', video)

def send_video(token, destination, video):
    payload = {
        'content': '@everyone\nМаэ бабуксы вишло новое видэо💥🔥:\nhttps://www.youtube.com/watch?v=' + video,
        'allowed_mentions': {'parse': ['everyone']},
        'nonce': hashlib.sha256((destination + video).encode()).hexdigest()[:24],
        'enforce_nonce': True,
    }
    request(f'https://discord.com/api/v10/channels/{destination}/messages', payload, token)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    env = config()
    check = '--check' in sys.argv
    token = env.get('DISCORD_BOT_TOKEN', '')
    destination = env.get('DISCORD_CHANNEL_ID', '')
    if not check and (not token or not re.fullmatch(r'\d{17,20}', destination)):
        raise ValueError('Заповніть DISCORD_BOT_TOKEN і DISCORD_CHANNEL_ID у файлі .env.')
    interval = max(30, int(env.get('CHECK_INTERVAL_SECONDS', '60')))
    channel = resolve_channel(env)
    logging.info('YouTube channel: %s', channel)
    feed = 'https://www.youtube.com/feeds/videos.xml?channel_id=' + channel
    if check:
        videos = parse_feed(request(feed), channel)
        print('YouTube доступний. Відео у стрічці:', len(videos))
        if videos:
            print('Останнє: https://www.youtube.com/watch?v=' + videos[-1])
        return
    db = database(BASE / 'state.sqlite3')
    while True:
        try:
            videos = parse_feed(request(feed), channel)
            process(db, channel, videos, lambda video: send_video(token, destination, video))
        except (RuntimeError, ValueError, OSError, ET.ParseError) as error:
            logging.error('%s', error)
            if '--once' in sys.argv:
                raise
        if '--once' in sys.argv:
            db.close()
            return
        time.sleep(interval)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nБота зупинено.')
    except Exception as error:
        logging.error('%s', error)
        sys.exit(1)
