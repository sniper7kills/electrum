#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import aiosocks
import os
import re
import ssl
import sys
import threading
import time
import traceback
import asyncio
import json
import asyncio.streams
from asyncio.sslproto import SSLProtocol

import requests

from aiosocks.errors import SocksError
from concurrent.futures import TimeoutError

from .util import print_error
from .ssl_in_socks import sslInSocksReaderWriter

ca_path = requests.certs.where()

from . import util
from . import x509
from . import pem

def get_ssl_context(cert_reqs, ca_certs):
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_certs)
    context.check_hostname = False
    context.verify_mode = cert_reqs
    context.options |= ssl.OP_NO_SSLv2
    context.options |= ssl.OP_NO_SSLv3
    context.options |= ssl.OP_NO_TLSv1
    return context

class Interface(util.PrintError):
    """The Interface class handles a socket connected to a single remote
    electrum server.  It's exposed API is:

    - Member functions close(), fileno(), get_response(), has_timed_out(),
      ping_required(), queue_request(), send_request()
    - Member variable server.
    """

    def __init__(self, server, loop, config_path, proxy_config):
        self.addr = self.auth = None
        if proxy_config is not None:
            if proxy_config["mode"] == "socks5":
                self.addr = aiosocks.Socks5Addr(proxy_config["host"], proxy_config["port"])
                self.auth = aiosocks.Socks5Auth(proxy_config["user"], proxy_config["password"]) if proxy_config["user"] != "" else None
            elif proxy_config["mode"] == "socks4":
                self.addr = aiosocks.Socks4Addr(proxy_config["host"], proxy_config["port"])
                self.auth = aiosocks.Socks4Auth(proxy_config["password"]) if proxy_config["password"] != "" else None
            else:
                raise Exception("proxy mode not supported")

        self.server = server
        self.loop = loop
        self.config_path = config_path
        host, port, protocol = self.server.split(':')
        self.host = host
        self.port = int(port)
        self.use_ssl = (protocol=='s')
        self.reader = self.writer = None
        self.lock = asyncio.Lock(loop=loop)
        # Dump network messages.  Set at runtime from the console.
        self.debug = False
        self.unsent_requests = asyncio.PriorityQueue(loop=loop)
        self.unanswered_requests = {}
        self.last_ping = 0
        self.closed_remotely = False

    def conn_coro(self, context):
        return asyncio.open_connection(self.host, self.port, loop=self.loop, ssl=context)

    async def _get_read_write(self):
        async with self.lock:
            if self.reader is not None and self.writer is not None:
                return self.reader, self.writer
            if self.use_ssl:
                cert_path = os.path.join(self.config_path, 'certs', self.host)
                if not os.path.exists(cert_path):
                    context = get_ssl_context(cert_reqs=ssl.CERT_NONE, ca_certs=None)
                    if self.addr is not None:
                        proto_factory = lambda: SSLProtocol(self.loop, asyncio.Protocol(), context, None)
                        socks_create_coro = aiosocks.create_connection(proto_factory, \
                                            proxy=self.addr, \
                                            proxy_auth=self.auth, \
                                            dst=(self.host, self.port),
                                            loop=self.loop)
                        transport, protocol = await asyncio.wait_for(socks_create_coro, 5, loop=self.loop)
                        while True:
                            try:
                                if protocol._sslpipe is not None:
                                    dercert = protocol._sslpipe.ssl_object.getpeercert(True)
                                    break
                            except ValueError:
                                print("sleeping for cert")
                                await asyncio.sleep(1)
                        transport.close()
                    else:
                        reader, writer = await asyncio.wait_for(self.conn_coro(context), 5, loop=self.loop)
                        dercert = writer.get_extra_info('ssl_object').getpeercert(True)
                        writer.close()
                    cert = ssl.DER_cert_to_PEM_cert(dercert)
                    temporary_path = cert_path + '.temp'
                    with open(temporary_path, "w") as f:
                        f.write(cert)
                    is_new = True
                else:
                    is_new = False
                ca_certs = temporary_path if is_new else cert_path
            try:
                if self.addr is not None:
                    if not self.use_ssl:
                        open_coro = aiosocks.open_connection(proxy=self.addr, proxy_auth=self.auth, dst=(self.host, self.port), loop=self.loop)
                        self.reader, self.writer = await asyncio.wait_for(open_coro, 5, loop=self.loop)
                    else:
                        asyncio.set_event_loop(self.loop)
                        self.reader, self.writer = await sslInSocksReaderWriter(self.addr, self.auth, self.host, self.port, ca_certs)
                else:
                    context = get_ssl_context(cert_reqs=ssl.CERT_REQUIRED, ca_certs=ca_certs) if self.use_ssl else None
                    self.reader, self.writer = await asyncio.wait_for(self.conn_coro(context), 5, loop=self.loop)
            except BaseException as e:
                traceback.print_exc()
                print("Previous exception will now be reraised")
                raise e
            if self.use_ssl and is_new:
                self.print_error("saving new certificate for", self.host)
                os.rename(temporary_path, cert_path)
            return self.reader, self.writer

    async def send_all(self, list_of_requests):
        _, w = await self._get_read_write()
        for i in list_of_requests:
            w.write(json.dumps(i).encode("ascii") + b"\n")
        await w.drain()

    def close(self):
        if self.writer:
            self.writer.close()

    async def get(self):
        reader, _ = await self._get_read_write()

        obj = b""
        while True:
            if len(obj) > 3000000:
                raise BaseException("too much data: " + str(len(obj)))
            try:
                obj += await reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as e:
                print("LimitOverrunError with", e.consumed, "consumed")
                obj += await reader.read(e.consumed)
            except asyncio.streams.IncompleteReadError as e:
                return None
            try:
                obj = json.loads(obj.decode("ascii"))
            except ValueError:
                continue
            else:
                self.last_action = time.time()
                return obj

    def idle_time(self):
        return time.time() - self.last_action

    def diagnostic_name(self):
        return self.host

    async def queue_request(self, *args):  # method, params, _id
        '''Queue a request, later to be send with send_requests when the
        socket is available for writing.
        '''
        self.request_time = time.time()
        await self.unsent_requests.put((self.request_time, args))

    def num_requests(self):
        '''Keep unanswered requests below 100'''
        n = 100 - len(self.unanswered_requests)
        return min(n, self.unsent_requests.qsize())

    async def send_request(self):
        '''Sends queued requests.  Returns False on failure.'''
        make_dict = lambda m, p, i: {'method': m, 'params': p, 'id': i}
        n = self.num_requests()
        prio, request = await self.unsent_requests.get()
        try:
            await self.send_all([make_dict(*request)])
        except (SocksError, OSError, TimeoutError) as e:
            if type(e) is SocksError:
                print(e)
            await self.unsent_requests.put((prio, request))
            return False
        if self.debug:
            self.print_error("-->", request)
        self.unanswered_requests[request[2]] = request
        self.last_action = time.time()
        return True

    def ping_required(self):
        '''Maintains time since last ping.  Returns True if a ping should
        be sent.
        '''
        now = time.time()
        if now - self.last_ping > 60:
            self.last_ping = now
            return True
        return False

    def has_timed_out(self):
        '''Returns True if the interface has timed out.'''
        if (self.unanswered_requests and time.time() - self.request_time > 10
            and self.idle_time() > 10):
            self.print_error("timeout", len(self.unanswered_requests))
            return True
        return False

    async def get_response(self):
        '''Call if there is data available on the socket.  Returns a list of
        (request, response) pairs.  Notifications are singleton
        unsolicited responses presumably as a result of prior
        subscriptions, so request is None and there is no 'id' member.
        Otherwise it is a response, which has an 'id' member and a
        corresponding request.  If the connection was closed remotely
        or the remote server is misbehaving, a (None, None) will appear.
        '''
        response = await self.get()
        if not type(response) is dict:
            print("response type not dict!", response)
            if response is None:
                self.closed_remotely = True
                self.print_error("connection closed remotely")
            return None, None
        if self.debug:
            self.print_error("<--", response)
        wire_id = response.get('id', None)
        if wire_id is None:  # Notification
            return None, response
        else:
            request = self.unanswered_requests.pop(wire_id, None)
            if request:
                return request, response
            else:
                self.print_error("unknown wire ID", wire_id)
                return None, None # Signal

def check_cert(host, cert):
    try:
        b = pem.dePem(cert, 'CERTIFICATE')
        x = x509.X509(b)
    except:
        traceback.print_exc(file=sys.stdout)
        return

    try:
        x.check_date()
        expired = False
    except:
        expired = True

    m = "host: %s\n"%host
    m += "has_expired: %s\n"% expired
    util.print_msg(m)


# Used by tests
def _match_hostname(name, val):
    if val == name:
        return True

    return val.startswith('*.') and name.endswith(val[1:])


def test_certificates():
    from .simple_config import SimpleConfig
    config = SimpleConfig()
    mydir = os.path.join(config.path, "certs")
    certs = os.listdir(mydir)
    for c in certs:
        p = os.path.join(mydir,c)
        with open(p) as f:
            cert = f.read()
        check_cert(c, cert)

if __name__ == "__main__":
    test_certificates()
