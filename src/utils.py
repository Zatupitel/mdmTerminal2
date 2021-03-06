#!/usr/bin/env python3

import base64
import functools
import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time
import traceback
from io import BytesIO

import requests
import socks  # install socks-proxy dependencies - pip install requests[socks]
import urllib3

from languages import YANDEX_SPEAKER, RHVOICE_SPEAKER, AWS_SPEAKER, DEFAULT_SPEAKERS

REQUEST_ERRORS = (
    requests.exceptions.HTTPError, requests.exceptions.RequestException, urllib3.exceptions.NewConnectionError,
    socks.ProxyError
)
CRLF = b'\r\n'


class RuntimeErrorTrace(RuntimeError):
    def __init__(self, *args):
        super().__init__('{}: {}'.format(' '.join([repr(arg) for arg in args]), traceback.format_exc()))


class SignalHandler:
    def __init__(self, signals=(signal.SIGTERM,)):
        self._sleep = threading.Event()
        self._death_time = 0
        [signal.signal(signal_, self._signal_handler)for signal_ in signals]

    def _signal_handler(self, *_):
        self._sleep.set()

    def die_in(self, sec: int):
        self._death_time = sec
        self._sleep.set()

    def interrupted(self) -> bool:
        return self._sleep.is_set()

    def sleep(self, sleep_time):
        self._sleep.wait(sleep_time)
        if self._death_time:
            time.sleep(self._death_time)


class FakeFP(queue.Queue):
    def read(self, _=None):
        return self.get()

    def write(self, n):
        self.put_nowait(n)

    def close(self):
        self.write(b'')


class EnergyControl:
    def __init__(self, cfg, noising, default=700):
        self._cfg = cfg
        self._noising = noising
        self._energy_previous = default
        self._energy_currently = None
        self._lock = threading.Lock()

    def _energy_threshold(self):
        return self._cfg.gts('energy_threshold', 0)

    def correct(self, r, source):
        with self._lock:
            energy_threshold = self._energy_threshold()
            if energy_threshold > 0:
                r.energy_threshold = energy_threshold
                return None
            elif energy_threshold < 0 and self._energy_currently:
                r.energy_threshold = self._energy_currently
            elif energy_threshold < 0 and self._noising():
                # Не подстаиваем автоматический уровень шума если терминал шумит сам.
                # Пусть будет прошлое успешное значение или 700
                r.energy_threshold = self._energy_previous
            else:
                r.adjust_for_ambient_noise(source)
            return r.energy_threshold

    def set(self, energy_threshold):
        with self._lock:
            if self._energy_currently:
                self._energy_previous = self._energy_currently
            self._energy_currently = energy_threshold


class Popen:
    TIMEOUT = 3 * 3600

    def __init__(self, cmd):
        self._cmd = cmd
        self._popen = None

    def _close(self):
        if self._popen:
            for target in (self._popen.stderr, self._popen.stdout):
                try:
                    target.close()
                except BrokenPipeError:
                    pass

    def run(self):
        try:
            return self._run()
        finally:
            self._close()

    def _run(self):
        try:
            self._popen = subprocess.Popen(self._cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        except FileNotFoundError as e:
            raise RuntimeError(e)
        try:
            self._popen.wait(self.TIMEOUT)
        except subprocess.TimeoutExpired as e:
            self._popen.kill()
            raise RuntimeError(e)
        if self._popen.poll():
            raise RuntimeError('{}: {}'.format(self._popen.poll(), repr(self._popen.stderr.read().decode())))
        return self._popen.stdout.read().decode()


class Connect:
    CHUNK_SIZE = 1024 * 4

    def __init__(self, conn, ip_info, work=True):
        self._conn = conn
        self._ip_info = ip_info
        self._work = work
        self._r_wait = False

    def stop(self):
        self._work = False

    def r_wait(self):
        self._r_wait = True

    @property
    def ip(self):
        return self._ip_info[0] if self._ip_info else None

    @property
    def port(self):
        return self._ip_info[1] if self._ip_info else None

    def settimeout(self, timeout):
        if self._conn:
            self._conn.settimeout(timeout)

    def close(self):
        if self._conn:
            try:
                # Сообщаем серверу о завершении сеанса отпрвкой CRLFCRLF
                self._conn_sender(CRLF)
            except RuntimeError:
                pass
            self._conn.close()

    def extract(self):
        if self._conn:
            try:
                return Connect(self._conn, self._ip_info, self._work)
            finally:
                self._conn = None
                self._ip_info = None

    def insert(self, conn, ip_info):
        self._conn = conn
        self._ip_info = ip_info

    def read(self):
        """
        Генератор,
        читает байты из сокета, разделяет их по \r\n и возвращает результаты в str,
        получение пустых данных(\r\n\r\n), любая ошибка сокета или завершение работы прерывает итерацию.
        Для совместимости: Если в данных вообще не было \r\n, сделаем вид что получили <data>\r\n\r\n.
        """
        if self._conn:
            return self._conn_reader()

    def write(self, data):
        """
        Преобразует dict -> json, str -> bytes, (nothing) -> bytes('') и отправляет байты в сокет.
        В конце автоматически добавляет \r\n.
        В любой непонятной ситуации кидает RuntimeError.
        """
        if self._conn:
            self._conn_sender(data)

    def raise_recv_err(self, cmd, code, msg, pmdl_name=None):
        data = {'cmd': cmd, 'code': code, 'msg': msg}
        if pmdl_name is not None:
            data['filename'] = pmdl_name
        self.write(data)
        raise RuntimeError(msg)

    def _conn_sender(self, data):
        if not data:
            data = b''
        elif isinstance(data, dict):
            try:
                data = json.dumps(data, ensure_ascii=False).encode()
            except TypeError as e:
                raise RuntimeError(e)
        elif isinstance(data, str):
            data = data.encode()
        elif not isinstance(data, bytes):
            raise RuntimeError('Unsupported data type: {}'.format(repr(type(data))))

        with BytesIO(data) as fp:
            del data
            chunk = True
            while chunk:
                chunk = fp.read(self.CHUNK_SIZE)
                try:
                    self._conn.send(chunk or CRLF)
                except (BrokenPipeError, socket.timeout, InterruptedError, OSError) as e:
                    raise RuntimeError(e)

    def _conn_reader(self):
        data = b''
        this_legacy = True
        while self._work:
            try:
                chunk = self._conn.recv(self.CHUNK_SIZE)
            except socket.timeout:
                if self._r_wait:
                    continue
                else:
                    break
            except (BrokenPipeError, ConnectionResetError, AttributeError, OSError):
                break
            if not chunk:
                # сокет закрыли, пустой объект
                break
            data += chunk
            while CRLF in data:
                # Обрабатываем все строки разделенные \r\n отдельно, пустая строка завершает сеанс
                this_legacy = False
                line, data = data.split(CRLF, 1)
                if not line:
                    return
                try:
                    yield line.decode()
                except UnicodeDecodeError:
                    pass
                del line
        if this_legacy and data and self._work:
            # Данные пришли без \r\n, обработаем их как есть
            try:
                yield data.decode()
            except UnicodeDecodeError:
                pass


def fix_speakers(cfg: dict) -> bool:
    modify = False
    for name, speakers in (
            ('rhvoice', RHVOICE_SPEAKER),
            ('rhvoice-rest', RHVOICE_SPEAKER),
            ('yandex', YANDEX_SPEAKER),
            ('aws', AWS_SPEAKER)):
        if not isinstance(cfg.get(name), dict) or 'speaker' not in cfg[name]:
            continue
        if cfg[name]['speaker'] in speakers:
            continue
        def_name = name if name != 'rhvoice-rest' else 'rhvoice'
        if def_name in DEFAULT_SPEAKERS:
            cfg[name]['speaker'] = DEFAULT_SPEAKERS[def_name]
            modify = True
    return modify


def get_ip_address():
    s = socket.socket(type=socket.SOCK_DGRAM)
    s.connect(('8.8.8.8', 80))
    return s.getsockname()[0]


def is_int(test: str) -> bool:
    return test.lstrip('-').isdigit()


def pretty_time(sec) -> str:
    ends = ['sec', 'ms']  # , 'ns']
    max_index = len(ends) - 1
    index = 0
    while sec < 1 and index < max_index and sec:
        sec *= 1000
        index += 1
    sec = int(sec) if sec % 1 < 0.01 else round(sec, 2)
    return '{} {}'.format(sec, ends[index])


def pretty_size(size) -> str:
    ends = ['Bytes', 'KiB', 'MiB', 'GiB', 'TiB']
    max_index = len(ends) - 1
    index = 0
    while size >= 1024 and index < max_index:
        size /= 1024.0
        index += 1
    size = int(size) if size % 1 < 0.1 else round(size, 1)
    return '{} {}'.format(size, ends[index])


def write_permission_check(path):
    return os.access(os.path.dirname(os.path.abspath(path)), os.W_OK)


def rhvoice_rest_sets(data: dict):
    ignore = 50
    sets = {}
    for param in ['rate', 'pitch', 'volume']:
        val = data.get(param, ignore)
        if val != ignore:
            sets[param] = val
    return sets


def check_phrases(phrases):
    if phrases is None:
        return
    if not isinstance(phrases, dict):
        raise ValueError('Not a dict - {}'.format(type(phrases)))
    keys = ['hello', 'deaf', 'ask']
    for key in keys:
        if not isinstance(phrases.get(key), list):
            raise ValueError('{} must be list, not a {}'.format(key, type(phrases.get(key))))
        if not phrases[key]:
            raise ValueError('{} empty'.format(key))
    if not isinstance(phrases.get('chance'), int):
        raise ValueError('chance must be int type, not a {}'.format(type(phrases.get('chance'))))
    if phrases['chance'] < 0:
        raise ValueError('chance must be 0 or greater, not a {}'.format(phrases['chance']))


def timed_cache(**timedelta_kwargs):
    """Кэширует результат вызова с учетом параметров на interval"""
    # https://gist.github.com/Morreski/c1d08a3afa4040815eafd3891e16b945
    def _wrapper(f):
        maxsize = timedelta_kwargs.pop('maxsize', 128)
        typed = timedelta_kwargs.pop('typed', False)
        update_delta = timedelta_kwargs.pop('interval', 1.0)
        next_update = time.time() - update_delta
        # Apply @lru_cache to f
        f = functools.lru_cache(maxsize=maxsize, typed=typed)(f)

        @functools.wraps(f)
        def _wrapped(*args, **kwargs):
            nonlocal next_update
            now = time.time()
            if now >= next_update:
                f.cache_clear()
                next_update = now + update_delta
            return f(*args, **kwargs)
        return _wrapped
    return _wrapper


def state_cache(interval):
    """
    Кэширует результат вызова без учета параметров на interval
    Чуть быстрее чем timed_cache, актуально если вызовы очень частые
    """
    def _wrapper(f):
        update_interval = interval
        next_update = time.time() - update_interval
        state = None

        @functools.wraps(f)
        def _wrapped(*args, **kwargs):
            nonlocal next_update, state
            now = time.time()
            if now >= next_update:
                next_update = now + update_interval
                state = f(*args, **kwargs)
            return state
        return _wrapped
    return _wrapper


def bool_cast(value) -> bool:
    """Интерпретируем что угодно как bool или кидаем ValueError"""
    if isinstance(value, str):
        value = value.lower()
        if value in ('on', '1', 'true', 'yes', 'enable'):
            return True
        elif value in ('off', '0', 'false', 'no', 'disable'):
            return False
    elif isinstance(value, bool):
        return value
    elif isinstance(value, int) and value in (1, 0):
        return bool(value)
    raise ValueError('Wrong type or value')


def yandex_speed_normalization(speed):
    return min(3.0, max(0.1, speed))


def singleton(cls):
    instances = {}

    def get_instance():
        if cls not in instances:
            instances[cls] = cls()
        return instances[cls]

    return get_instance


def file_to_base64(file_name: str) -> str:
    with open(file_name, 'rb') as fp:
        return base64.b64encode(fp.read()).decode()


def base64_to_bytes(data):
    try:
        return base64.b64decode(data)
    except (ValueError, TypeError) as e:
        raise RuntimeError(e)


def mask_off(obj):
    iterable_type = (list, tuple, set, dict)
    if not obj:
        return obj
    iterable = isinstance(obj, iterable_type)
    if not iterable:
        obj = (obj,)
    masked = []
    for key in obj:
        if not key or isinstance(key, bool):
            masked.append(key)
        elif isinstance(key, iterable_type):
            masked.append(mask_off(key))
        elif isinstance(key, (int, float)):
            key_ = str(key)
            if len(key_) < 3:
                masked.append(key)
            else:
                masked.append('*' * len(key_))
        elif isinstance(key, str):
            key_len = len(key)
            if key_len < 14:
                key = '*' * key_len
            else:
                key = '{}**LENGTH<{}>**{}'.format(key[:2], key_len, key[-2:])
            masked.append(key)
        else:
            masked.append('**HIDDEN OBJECT**')
    return masked if iterable or not masked else masked[0]
