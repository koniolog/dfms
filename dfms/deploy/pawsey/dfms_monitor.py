#!/usr/bin/python
#
#    ICRAR - International Centre for Radio Astronomy Research
#    (c) UWA - The University of Western Australia, 2016
#    Copyright by UWA (in the framework of the ICRAR)
#    All rights reserved
#
#    This library is free software; you can redistribute it and/or
#    modify it under the terms of the GNU Lesser General Public
#    License as published by the Free Software Foundation; either
#    version 2.1 of the License, or (at your option) any later version.
#
#    This library is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#    Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public
#    License along with this library; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston,
#    MA 02111-1307  USA
#
"""
DFMS Monitor runs outside the Pawsey firewall
--------------------------------------------------------------------------------
          Pawsey Magnus / Galaxy              |     Public         |
             Private Network                  |     Network        |
                                              |                    |
+---------+                +----------+       |      +--------+    |
|  DFMS   |                |  DFMS    |       |      | DFMS   |    |
| DropMgr | <== socket ==> |  Proxy   |<== socket ==>| Monitor|<- http <- Client
+---------+                +----------+       |      +--------+    |   (Browser)
                                              |                    |
                                           FIREWALL             GATEWAY
                                              |                    |
--------------------------------------------------------------------------------
"""

import BaseHTTPServer
import collections
import errno
import json
import logging
import optparse
import os
import select
import socket
import struct
import sys
import threading
import time


BUFF_SIZE = 16384
outstanding_conn = 20
default_publication_port = 20000
default_proxy_port = 30000
default_client_base_port = 30001
FORMAT = "%(asctime)-15s [%(levelname)5.5s] %(name)s#%(funcName)s:%(lineno)s %(message)s"

logger = logging.getLogger('deploy.pawsey.monitor')
delimit = '@#%!$'
dl = len(delimit)

def recvall(sock, count):
    buf = ''
    while count:
        # this will block
        newbuf = sock.recv(count)
        if not newbuf: return None
        buf += newbuf
        count -= len(newbuf)
    return buf

def send_to_dfms(sock, data):
    length = len(data)
    sock.sendall(struct.pack('!I', length))
    sock.sendall(data)

def recv_from_dfms(sock):
    lengthbuf = recvall(sock, 4)
    if (lengthbuf is None):
        return None
    length, = struct.unpack('!I', lengthbuf)
    return recvall(sock, length)

# HTTP support to get the list of available proxies
class Handler(BaseHTTPServer.BaseHTTPRequestHandler):
    def setup(self):
        BaseHTTPServer.BaseHTTPRequestHandler.setup(self)
        self.monitor = self.server.monitor

    def do_GET(self):
        if self.path not in ('/', ''):
            self.send_error(404)
            return

        host = 'localhost'
        if 'Host' in self.headers:
            host = self.headers['Host']
            host = host if ":" not in host else host.split(':')[0]

        self.send_response(200)
        if 'Accept' in self.headers and 'text/html' in self.headers['Accept']:
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            if not self.monitor.proxy_ids:
                self.wfile.write("No proxies available yet")
                return

            aEls = ['<a href="http://{0}:{2}">{1} @ {0}:{2}</a>'.format(host,proxyId,client_port) for proxyId, client_port in self.monitor.proxy_ids.items()]
            html = "</li><li>".join(aEls)
            html = "<ul><li>" + html + "</li></ul>"
            self.wfile.write(html)
            return

        # Else print as JSON
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(self.monitor.proxy_ids, indent=2))

class Server(BaseHTTPServer.HTTPServer):
    def __init__(self, monitor):
        self.monitor = monitor
        BaseHTTPServer.HTTPServer.__init__(self, (monitor.host, monitor.publication_port), Handler)

sockandaddr = collections.namedtuple('sockandaddr', 'sock addr')

class DFMSMonitor:

    def __init__(self, host='0.0.0.0', proxy_port=default_proxy_port, client_base_port=default_client_base_port, publication_port=default_publication_port):
        """
        host:             listening host (string)
        proxy_port:       port exposed to the dfms proxy  (int)
        client_base_port: base port exposed to the client (e.g. Firefox) (int)
        """
        self.host = host
        self.next_client_port = client_base_port

        # All our sets of sockets:
        # * One always listening for all incoming proxy connections
        # * The list of currently opened proxy connections (idx by tag)
        # * Many listening for incoming client connections, one per proxy connection
        # * Many currently opened client connection (idx by port)
        self.proxy_listener_socket = None
        self.proxy_sockets = {}
        self.client_listener_sockets = {}
        self.client_sockets = {}

        # Mapping between proxy socket port numbers and client listening ports
        # This is used to route client requests through the correct proxy socket
        self.client_port_to_proxy_port = {}

        # To save the tags we attach to each client socket
        self.tag_dict = {} # k - socket hash, v - socket tag

        # Proxy IDs to client ports. We publish that information in publication_port
        self.proxy_ids = {}
        self.publication_port = publication_port

        # Set up the single socket that listens for proxy connection
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, proxy_port))
        sock.listen(outstanding_conn)
        self.proxy_listener_socket = sock
        self.ifds = [self.proxy_listener_socket]
        logger.info('Listening for proxies at %s:%d', self.host, proxy_port)

    def tag_for_socket(self, sock, create=True):
        hashcode = hash(sock)
        if not create:
            return self.tag_dict[hashcode]

        tag = '{0}_{1}'.format(hashcode, time.time() - 1E9)
        self.tag_dict[hashcode] = tag
        return tag

    def start_ioloop(self):
        logger.info("Starting IO thread")
        self._running = True
        self._io_thread = threading.Thread(target=self.ioloop)
        self._io_thread.start()

    def stop_ioloop(self):
        logger.info("Joining IO thread")
        self._running = False
        self._io_thread.join(5)
        logger.info("IO thread joined correctly? %d", not self._io_thread.isAlive())

    def main_loop(self):

        self.start_ioloop()

        http_server = Server(self)
        try:
            logger.info("Starting up HTTP server on %s:%d", self.host, self.publication_port)
            http_server.serve_forever()
        except KeyboardInterrupt:
            self.stop_ioloop()
            raise

    def ioloop(self):
        while self._running:
            try:
                inputready, _, _ = select.select(self.ifds, [], [], 0.5)

                # The self.* lists are continuously updated by the on_* methods,
                # so we keep a reference to the initial values they have.
                # This means that the methods must be prepared to accept a socket
                # that is not really working anymore
                proxy_sockets = [x.sock for x in self.proxy_sockets.values()]
                client_sockets = [x.sock for x in self.client_sockets.values()]

                for sock in inputready:
                    if sock == self.proxy_listener_socket:
                        self.on_proxy_connected(sock)
                    elif sock in self.client_listener_sockets.values():
                        self.on_client_connected(sock)
                    elif sock in proxy_sockets:
                        self.on_proxy_data(sock)
                    elif sock in client_sockets:
                        self.on_client_data(sock)
                    else:
                        logger.error("Received data from unknown socket: %r", sock)
            except (OSError, select.error) as e:
                print e
                if e.args[0] == errno.EINTR:
                    break
            except Exception as ex:
                print str(ex)
                logger.exception("Unexpected exception, some communications might have been lost")

    def add_client_listener(self):

        client_listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        bound = False
        port = self.next_client_port
        while not bound:
            try:
                client_listener_socket.bind((self.host, port))
                self.next_client_port += 1
                logger.info('Listening for clients at %s:%d', self.host, port)
                bound = True
            except socket.error as e:
                if e.errno != errno.EADDRINUSE:
                    raise
                logger.info("Port %d already bound, trying next", port)
                port += 1

        client_listener_socket.listen(outstanding_conn)
        return client_listener_socket

    def close_socket(self, sock, shutdown=True):

        # Maybe we already got rid of it...
        if sock not in self.ifds:
            return

        # We don't listen to it anymore, remove it from our records
        self.ifds.remove(sock)

        if shutdown:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except socket.error:
                logger.exception("Error while shutting down socket %r, continuing anyway", sock.getsockname())

        try:
            sock.close()
        except socket.error:
            logger.exception("Error while closing socket %r, continuing anyway", sock.getsockname())

    def remove_client_socket(self, sock):
        tag = self.tag_for_socket(sock, create=False)
        client_sockandaddr = self.client_sockets[tag]
        logger.info("Closing client socket %r (%s)", client_sockandaddr.addr, tag)
        self.close_socket(sock, False)
        del self.client_sockets[tag]
        del self.tag_dict[hash(sock)]

    def remove_proxy_socket(self, sock):

        for proxyport,saa in self.proxy_sockets.items():
            if saa.sock == sock:
                break

        # Close the proxy socket itself
        logger.info("Closing proxy socket %r", saa.addr)
        del self.proxy_sockets[proxyport]
        self.close_socket(sock, False)

        # Close the client listener port associated to this proxy
        # so no more incoming client requests are received
        clientport = None
        for clientport, pp in self.client_port_to_proxy_port.items():
            if pp == proxyport:
                break
        if clientport is None:
            raise Exception("This shouldn't have happened, sorry :-(")

        self.remove_clientlistener_socket(clientport)

        # Free up the ID of this proxy
        proxyId_toDelete = None
        for proxyId, port in self.proxy_ids.items():
            if port == clientport:
                proxyId_toDelete = proxyId
        del self.proxy_ids[proxyId_toDelete]

        clisocksandaddr = self.client_sockets.values()
        for saa in clisocksandaddr:
            this_clientport = saa.addr[1]
            if this_clientport == clientport:
                self.close_socket(saa.sock)

    def remove_clientlistener_socket(self, port):
        cls = self.client_listener_sockets[port]
        logger.info("Closing client listener socket %r", cls.getsockname())
        self.close_socket(cls)
        del self.client_listener_sockets[port]
        del self.client_port_to_proxy_port[port]
        if port < self.next_client_port:
            self.next_client_port = port

    def on_proxy_connected(self, sock):

        proxysock, proxyaddr = sock.accept()
        proxyport = proxyaddr[1]
        self.proxy_sockets[proxyport] = sockandaddr(proxysock, proxyaddr)
        self.ifds.append(proxysock)
        logger.info('Received new connection from dfms_proxy at %r, reading identification', proxyaddr)

        # Read the proxy ID and check we don't have duplicates
        proxy_id = recvall(proxysock, 80).strip()
        if proxy_id in self.proxy_ids:
            proxysock.sendall('0')
            self.close_socket(proxysock, True)
            return

        proxysock.sendall('1')
        logger.info('Proxy identified as %s, fine', proxy_id)

        client_listener_socket = self.add_client_listener()
        clientport = client_listener_socket.getsockname()[1]
        self.client_listener_sockets[clientport] = client_listener_socket
        self.client_port_to_proxy_port[clientport] = proxyport
        self.ifds.append(client_listener_socket)

        # Save the client port associated to this proxy
        self.proxy_ids[proxy_id] = clientport

    def on_client_connected(self, sock):

        clientsock, clientaddr = sock.accept()

        if len(self.proxy_sockets) == 0:
            # This shouldn't happen though...
            logger.debug("Received client connection, but no proxy connections ready yet, ignoring")
            clientsock.shutdown()
            clientsock.close()
            return

        # Unique per-client-connection tag
        tag = self.tag_for_socket(clientsock)
        if tag in self.client_sockets:
            raise Exception("Duplicated tag {0}".format(tag))
        self.client_sockets[tag] = sockandaddr(clientsock, sock.getsockname())

        # Check for incoming data
        self.ifds.append(clientsock)
        logger.info('Received new client connection %r -> %s (%s)', clientaddr, sock.getsockname(), tag)

    def on_proxy_data(self, sock):

        try:
            data = recv_from_dfms(sock)
        except socket.error:
            logger.warning("Error while reading data from proxy, will close it")
            self.remove_proxy_socket(sock)
            return

        if data is None:
            logger.warning("Proxy disconnected")
            self.remove_proxy_socket(sock)
            return

        at = data.find(delimit)
        if at == -1:
            logger.error('No tag id from DFMS proxy, discard the message')
            return

        tag = data[0:at]
        logger.debug("Received %s from DFMS proxy", tag)

        if tag not in self.client_sockets:
            logger.warning("Client %s has already disconnected, discarding data from proxy", tag)
            return

        client_sockandaddr = self.client_sockets[tag]
        if client_sockandaddr is None:
            logger.warning("Couldn't find client for tag '%s' of proxy %r", tag, sock.getsockname())
            return
        client_sock = client_sockandaddr.sock

        to_send = data[at + dl:]
        try:
            client_sock.sendall(to_send)
            logger.debug("Sent data to client %s", tag)
        except socket.error:
            logger.warning("Error while writing to client %r, we'll probably detect it later", client_sockandaddr.addr)

    def on_client_data(self, sock):

        tag = self.tag_for_socket(sock, create=False)

        try:
            data = sock.recv(BUFF_SIZE)
        except socket.error:
            logger.warning("Error while reading data from client, will close it")
            self.remove_client_socket(sock)
            return

        # The client disconnected, remove it
        if not data:
            logger.info("Client %s disconnected", tag)
            self.remove_client_socket(sock)
            return

        logger.debug("Received data from client %s", tag)
        proxy_port = self.client_port_to_proxy_port[sock.getsockname()[1]]
        proxy_socket = None
        for port,proxy_sock in self.proxy_sockets.items():
            if port == proxy_port:
                proxy_socket = proxy_sock.sock
                break

        if proxy_socket is None:
            raise Exception("shouldn't happen, right?")

        try:
            send_to_dfms(proxy_socket, delimit.join([tag, data]))
            logger.debug("Sent data from client %s to proxy", tag)
        except socket.error:
            logger.warning("Error while sending data to proxy, closing proxy connection")
            self.close_socket(proxy_socket)

if __name__ == '__main__':

    parser = optparse.OptionParser()
    parser.add_option("-H", "--host", action="store", type="string",
                    dest="host", help="The network interface the monitor is bind",
                    default='0.0.0.0')
    parser.add_option("-o", "--monitor_port", action="store", type="int",
                    dest="monitor_port", help = "The monitor port exposed to the dfms proxy",
                    default=default_proxy_port)
    parser.add_option("-c", "--client_port", action="store", type="int",
                    dest="client_port", help = "The proxy port exposed to the client",
                    default=default_client_base_port)
    parser.add_option("-l", "--log_dir", action="store", type="string",
                    dest="log_dir", help="log directory for dfms monitor server", default=os.path.realpath(__file__))
    parser.add_option("-p", "--publication_port", action="store", type="int",
                      dest="publication_port", help="Port used to publish the list of proxies for clients to look at", default=default_publication_port)
    parser.add_option("-d", "--debug",
                  action="store_true", dest="debug", default=False,
                  help="Whether to log debug info")
    (options, args) = parser.parse_args()

    logfile = "{0}/dfms_monitor.log".format(os.path.dirname(options.log_dir))
    if (options.debug):
        ll = logging.DEBUG
    else:
        ll = logging.INFO
    logging.basicConfig(stream=sys.stdout, level=ll, format=FORMAT)

    server = DFMSMonitor(options.host, options.monitor_port, options.client_port, publication_port=options.publication_port)
    try:
        server.main_loop()
    except KeyboardInterrupt:
        logger.warning("Ctrl C - Stopping DFMS Monitor server")
        sys.exit(1)
