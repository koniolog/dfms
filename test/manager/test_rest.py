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
import threading
import unittest

from dfms.manager import constants
from dfms.manager.client import NodeManagerClient
from dfms.manager.node_manager import NodeManager
from dfms.manager.rest import NMRestServer
from dfms import exceptions
import tempfile
from docutils.nodes import sidebar


hostname = 'localhost'

class TestRest(unittest.TestCase):

    def setUp(self):
        unittest.TestCase.setUp(self)
        self.dm = NodeManager(False)
        self._dm_server = NMRestServer(self.dm)
        self._dm_t = threading.Thread(target=self._dm_server.start, args=(hostname, constants.NODE_DEFAULT_REST_PORT))
        self._dm_t.start()

    def tearDown(self):
        unittest.TestCase.tearDown(self)
        self._dm_server.stop()
        self._dm_t.join()
        self.dm.shutdown()
        self.assertFalse(self._dm_t.isAlive())

    def test_errtype(self):

        sid = 'lala'
        c = NodeManagerClient(hostname)
        c.createSession(sid)

        # already exists
        self.assertRaises(exceptions.SessionAlreadyExistsException, c.createSession, sid)

        # different session
        self.assertRaises(exceptions.NoSessionException, c.addGraphSpec, sid + "x", [{}])

        # invalid dropspec, it has no oid/type (is completely empty actually)
        self.assertRaises(exceptions.InvalidGraphException, c.addGraphSpec, sid, [{}])

        # invalid state, the graph status is only queried when the session is running
        self.assertRaises(exceptions.InvalidSessionState, c.getGraphStatus, sid)

        # valid dropspec, but the socket listener app doesn't allow inputs
        c.addGraphSpec(sid, [{'type': 'socket', 'oid': 'a', 'inputs': ['b']}, {'oid': 'b', 'type': 'plain', 'storage': 'memory'}])
        self.assertRaises(exceptions.InvalidRelationshipException, c.deploySession, sid)

        # And here we point to an unexisting file, making an invalid drop
        c.destroySession(sid)
        c.createSession(sid)
        fname = tempfile.mktemp()
        c.addGraphSpec(sid, [{'type': 'plain', 'storage': 'file', 'oid': 'a', 'filepath': fname, 'check_filepath_exists': True}])
        self.assertRaises(exceptions.InvalidDropException, c.deploySession, sid)