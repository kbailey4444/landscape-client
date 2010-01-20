"""Expose the methods of a remote object over AMP."""

from uuid import uuid4
from twisted.internet.defer import Deferred
from twisted.internet.protocol import ServerFactory, ClientFactory
from twisted.protocols.amp import Argument, String, Command, AMP
from twisted.python.failure import Failure

from landscape.lib.bpickle import loads, dumps, dumps_table


class MethodCallArgument(Argument):
    """A bpickle-compatible argument."""

    def toString(self, inObject):
        """Serialize an argument."""
        return dumps(inObject)

    def fromString(self, inString):
        """Unserialize an argument."""
        return loads(inString)

    @classmethod
    def check(cls, inObject):
        """Check if an argument is serializable."""
        return type(inObject) in dumps_table


class MethodCallError(Exception):
    """Raised when a L{MethodCall} command fails."""


class MethodCall(Command):
    """Call a method on the object exposed by a L{MethodCallProtocol}."""

    arguments = [("method", String()),
                 ("args", MethodCallArgument()),
                 ("kwargs", MethodCallArgument())]

    response = [("result", MethodCallArgument()),
                ("deferred", String(optional=True))]

    errors = {MethodCallError: "METHOD_CALL_ERROR"}


class DeferredResponse(Command):
    """Fire a L{Deferred} associated with an outstanding method call result."""

    arguments = [("uuid", String()),
                 ("result", MethodCallArgument(optional=True)),
                 ("failure", String(optional=True))]
    requiresAnswer = False


class MethodCallServerProtocol(AMP):
    """Expose methods of a local object over AMP.

    The object to be exposed is expected to be the C{object} attribute of our
    protocol factory.

    @cvar methods: The list of exposed object's methods that can be called with
        the protocol. It must be defined by sub-classes.
    """

    methods = []

    @MethodCall.responder
    def receive_method_call(self, method, args, kwargs):
        """Call an object's method with the given arguments.

        If a connected client sends a L{MethodCall} for method C{foo_bar}, then
        the actual method C{foo_bar} of the object associated with the protocol
        will be called with the given C{args} and C{kwargs} and its return
        value delivered back to the client as response to the command.

        @param method: The name of the object's method to call.
        @param args: The arguments to pass to the method.
        @param kwargs: The keywords arguments to pass to the method.
        """
        if not method in self.methods:
            raise MethodCallError("Forbidden method '%s'" % method)

        try:
            result = getattr(self.factory.object, method)(*args, **kwargs)
        except Exception, error:
            raise MethodCallError("Remote exception %s" % str(error))

        # If the method returns a Deferred, register callbacks that will
        # eventually notify the remote peer of its success or failure.
        if isinstance(result, Deferred):

            # If the Deferred was already fired, we can return its result
            if result.called:
                if isinstance(result.result, Failure):
                    failure = str(result.result.value)
                    result.addErrback(lambda error: None) # Stop propagating
                    raise MethodCallError(failure)
                return {"result": result.result}

            uuid = str(uuid4())
            result.addBoth(self.send_deferred_response, uuid)
            return {"result": None, "deferred": uuid}

        if not MethodCallArgument.check(result):
            raise MethodCallError("Non-serializable result")
        return {"result": result}

    def send_deferred_response(self, result, uuid):
        """Send a L{DeferredResponse} for the deferred with given C{uuid}.

        This is called when the result of L{Deferred} returned by an
        object's method becomes available. A L{DeferredResponse} notifying
        such result (either success or failure) is sent to the peer.
        """
        kwargs = {"uuid": uuid}
        if isinstance(result, Failure):
            kwargs["failure"] = str(result.value)
        else:
            kwargs["result"] = result
        self.callRemote(DeferredResponse, **kwargs)


class MethodCallClientProtocol(AMP):
    """Calls methods of a remote object over L{AMP}.

    @note: If the remote method returns a deferred, the associated local
        deferred returned by L{send_method_call} will result in the same
        callback value of the remote deferred.
    @cvar timeout: A timeout for remote methods returning L{Deferred}s, if a
        response for the deferred is not received within this amount of
        seconds, the remote method call will errback with a L{MethodCallError}.
    """
    timeout = 60

    def __init__(self):
        AMP.__init__(self)
        self._pending_responses = {}

    @DeferredResponse.responder
    def receive_deferred_response(self, uuid, result, failure):
        """Receive the deferred L{MethodCall} response.

        @param uuid: The id of the L{MethodCall} we're getting the result of.
        @param result: The result of the associated deferred if successful.
        @param failure: The failure message of the deferred if it failed.
        """
        self.fire_deferred(uuid, result, failure)
        return {}

    def fire_deferred(self, uuid, result, failure):
        """Fire a pending deferred.

        @param uuid: The id of the L{MethodCall} we're getting the result of.
        @param result: The result of the associated deferred if successful.
        @param failure: The failure message of the deferred if it failed.
        """
        deferred, call = self._pending_responses.pop(uuid)
        if not call.called:
            call.cancel()
        if failure is None:
            deferred.callback({"result": result})
        else:
            deferred.errback(MethodCallError(failure))

    def handle_response(self, response):
        """Handle a L{MethodCall} response.

        If the response is tagged as deferred, it will be queued as pending,
        and a L{Deferred} is returned, which will be fired as soon as the
        final response becomes available.
        """
        if response["deferred"]:
            uuid = response["deferred"]
            deferred = Deferred()
            call = self.factory.reactor.callLater(self.timeout,
                                                  self.fire_deferred,
                                                  uuid, None, "timeout")
            self._pending_responses[uuid] = (deferred, call)
            return deferred

        return response

    def send_method_call(self, method, args=[], kwargs={}):
        """Send a L{MethodCall} command with the given arguments.

        @param method: The name of the remote method to invoke.
        @param args: The positional arguments to pass to the remote method.
        @param kwargs: The keyword arguments to pass to the remote method.
        """
        result = self.callRemote(MethodCall,
                                 method=method, args=args, kwargs=kwargs)
        # The result can be C{None} only if the requested command is a
        # DeferredResponse, which has requiresAnswer set to False
        if result is not None:
            return result.addCallback(self.handle_response)


class MethodCallServerFactory(ServerFactory):
    """Factory for building L{MethodCallProtocol}s exposing an object."""

    protocol = MethodCallServerProtocol

    def __init__(self, object):
        """
        @param object: The object exposed by the L{MethodCallProtocol}s
            instances created by this factory.
        """
        self.object = object


class MethodCallClientFactory(ClientFactory):
    """Factory for building L{AMP} connections to L{MethodCall} servers."""

    protocol = MethodCallClientProtocol

    def __init__(self, reactor, notifier):
        """
        @param reactor: The reactor used by the created protocols
            to schedule notifications and timeouts.
        @param notifier: A function that will be called when the factory builds
            a new connected protocol.  It will be passed the new protocol
            instance as argument.
        """
        self.reactor = reactor
        self._notifier = notifier

    def buildProtocol(self, addr):
        protocol = self.protocol()
        protocol.factory = self
        self.reactor.callLater(0, self._notifier, protocol)
        return protocol


class RemoteObject(object):
    """An object able to transparently call methods on a remote object.

    Any method call on a L{RemoteObject} instance will return a L{Deferred}
    resulting in the return value of the same method call performed on
    the remote object exposed by the peer.
    """

    def __init__(self, protocol):
        """
        @param protocol: A reference to a connected L{AMP} protocol instance,
            which will be used to send L{MethodCall} commands.
        """
        self._protocol = protocol

    def __getattr__(self, method):
        """Return a function sending a L{MethodCall} for the given C{method}.

        When the created function is called, it sends the an appropriate
        L{MethodCall} to the remote peer passing it the arguments and
        keyword arguments it was called with, and returning a L{Deferred}
        resulting in the L{MethodCall}'s response value.
        """

        def send_method_call(*args, **kwargs):
            result = self._protocol.send_method_call(method=method,
                                                     args=args,
                                                     kwargs=kwargs)
            return result.addCallback(lambda response: response["result"])

        return send_method_call


class RemoteObjectCreator(object):
    """Connect to remote objects exposed by a L{MethodCallProtocol}."""

    factory = MethodCallClientFactory
    remote = RemoteObject

    def __init__(self, reactor, socket_path):
        """
        @param reactor: A reactor able to connect to Unix sockets.
        @param socket: The path to the socket we want to connect to.
        """
        self._socket_path = socket_path
        self._reactor = reactor
        self._remote = None

    def connect(self):
        """Connect to a remote object exposed by a L{MethodCallProtocol}.

        This method will connect to the socket provided in the constructor
        and return a L{Deferred} resulting in a connected L{RemoteObject}.
        """
        deferred = Deferred()
        factory = self.factory(self._reactor, deferred.callback)
        self._reactor.connectUNIX(self._socket_path, factory)
        deferred.addCallback(self._connection_made)
        return deferred

    def _connection_made(self, protocol):
        """Called when the connection has been established"""
        self._remote = self.remote(protocol)
        return self._remote

    def disconnect(self):
        """Disconnect the L{RemoteObject} that we have created."""
        self._remote._protocol.transport.loseConnection()
