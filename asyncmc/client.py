import tornado.ioloop
import functools
import logging
import re
from .pool import ConnectionPool
from . import constants as const
from .exceptions import ClientException, ValidationException
from tornado import gen


def acquire(func):

    @gen.coroutine
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        conn = yield self.pool.acquire()
        try:
            res = yield func(self, conn, *args, **kwargs)
            return res
        except Exception as exc:
            print(exc)
            raise
        finally:
            self.pool.release(conn)

    return wrapper


class Client(object):

    _FLAG_PICKLE = 1 << 0
    _FLAG_INTEGER = 1 << 1
    _FLAG_LONG = 1 << 2

    def __init__(self, servers=["localhost:11211"], debug=0, **kwargs):
        self.debug = debug
        self.io_loop = tornado.ioloop.IOLoop.instance()
        self.pool = ConnectionPool(
            servers,
            debug=debug,
            loop=self.io_loop,
            **kwargs
        )

    # key supports ascii sans space and control chars
    # \x21 is !, right after space, and \x7e is -, right before DEL
    # also 1 <= len <= 250 as per the spec
    _valid_key_re = re.compile(b'^[\x21-\x7e]{1,250}$')

    @acquire
    @gen.coroutine
    def stats(self, conn, args=None):
        """Runs a stats command on the server."""
        # req  - stats [additional args]\r\n
        # resp - STAT <name> <value>\r\n (one per result)
        #        END\r\n
        if args is None:
            args = b''
        cmd = b''.join((b'stats ', args))
        resp = yield conn.send_cmd(cmd)
        result = {}
        while resp != b'END\r\n':
            terms = resp.split()

            if len(terms) == 2 and terms[0] == b'STAT':
                result[terms[1]] = None
            elif len(terms) == 3 and terms[0] == b'STAT':
                result[terms[1]] = terms[2]
            else:
                raise ClientException('stats failed', resp)

            resp = yield conn.get_stream(cmd).read_until(b'\r\n')

        return result

    @acquire
    @gen.coroutine
    def version(self, conn):
        """Current version of the server.

        :return: ``bytes``, memcached version for current the server.
        """
        command = b'version'
        response = yield conn.send_cmd(command)
        if not response.startswith(const.VERSION):
            raise ClientException('Memcached version failed', response)
        version, number = response.split()
        return number

    @acquire
    @gen.coroutine
    def get(self, conn, key, default=None):
        """Gets a single value from the server.

        :param key: ``bytes``, is the key for the item being fetched
        :param default: default value if there is no value.
        :return: ``bytes``, is the data for this specified key.
        """
        result = yield self._multi_get(conn, key)
        return result[0] if result else default

    @acquire
    @gen.coroutine
    def set(self, conn, key, value, exptime=0):
        """Sets a key to a value on the server
        with an optional exptime (0 means don't auto-expire)

        :param key: ``bytes``, is the key of the item.
        :param value: ``bytes``, data to store.
        :param exptime: ``int``, is expiration time. If it's 0, the
        item never expires.
        :return: ``bool``, True in case of success.
        """
        self._info('insert key {}'.format(key))
        resp = yield self._storage_command(
            conn, b'set', key, value, 0, exptime)
        return resp

    @gen.coroutine
    def _multi_get(self, conn, *keys):
        # req  - get <key> [<key> ...]\r\n
        # resp - VALUE <key> <flags> <bytes> [<cas unique>]\r\n
        #        <data block>\r\n (if exists)
        #        [...]
        #        END\r\n
        if not keys:
            return []

        [self._validate_key(key) for key in keys]
        if len(set(keys)) != len(keys):
            raise ClientException('duplicate keys passed to multi_get')
        stream = conn.get_stream('1')  # TODO more streams
        cmd = b'get ' + b' '.join(keys) + b'\r\n'
        yield stream.write(cmd)
        logging.info(cmd)
        received = {}
        line = yield stream.read_until(b'\n')
        logging.info(line)
        while line != b'END\r\n':
            terms = line.split()

            if len(terms) == 4 and terms[0] == b'VALUE':  # exists
                key = terms[1]
                flags = int(terms[2])
                length = int(terms[3])

                if flags != 0:
                    raise ClientException('received non zero flags')

                val = yield stream.read_bytes(length+2)
                val = val[:-2]
                if key in received:
                    raise ClientException('duplicate results from server')

                received[key] = val
            else:
                raise ClientException('get failed', line)

            line = yield stream.read_until(b'\n')

        if len(received) > len(keys):
            raise ClientException('received too many responses')
        return [received.get(k, None) for k in keys]

    def _info(self, msg):
        if self.debug:
            logging.info(msg)

    @gen.coroutine
    def _storage_command(self, conn, command, key, value,
                         flags=0, exptime=0):
        # req  - set <key> <flags> <exptime> <bytes> [noreply]\r\n
        #        <data block>\r\n
        # resp - STORED\r\n (or others)

        # typically, if val is > 1024**2 bytes server returns:
        #   SERVER_ERROR object too large for cache\r\n
        # however custom-compiled memcached can have different limit
        # so, we'll let the server decide what's too much

        assert self._validate_key(key)

        if not isinstance(exptime, int):
            raise ValidationException('exptime not int', exptime)
        elif exptime < 0:
            raise ValidationException('exptime negative', exptime)

        args = [str(a).encode('utf-8') for a in (flags, exptime, len(value))]
        _cmd = b' '.join([command, key] + args) + b'\r\n'
        cmd = _cmd + value
        resp = yield conn.send_cmd(cmd)

        if resp not in (const.STORED, const.NOT_STORED):
            raise ClientException('stats {} failed'.format(command), resp)
        return resp == const.STORED

    def _validate_key(self, key):
        if not isinstance(key, bytes):  # avoid bugs subtle and otherwise
            raise ValidationException('key must be bytes', key)

        m = self._valid_key_re.match(key)
        if m:
            # in python re, $ matches either end of line or right before
            # \n at end of line. We can't allow latter case, so
            # making sure length matches is simplest way to detect
            if len(m.group(0)) != len(key):
                raise ValidationException('trailing newline', key)
        else:
            raise ValidationException('invalid key', key)

        return key
