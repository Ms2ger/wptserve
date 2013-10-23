import sys
import os
import BaseHTTPServer
from SocketServer import ThreadingMixIn
import traceback
import socket
import threading
import re
import types
import logging
import ssl
import imp
import urlparse
import errno

import handlers
from request import Server, Request
from response import Response
import routes
from utils import HTTPException

logger = logging.getLogger("wptserve")
logger.setLevel(logging.DEBUG)

"""HTTP server designed for testing purposes.

The server is designed to provide flexibility in the way that
requests are handled, and to provide control both of exactly
what bytes are put on the wire for the response, and in the
timing of sending those bytes.

The server is based on the stdlib HTTPServer, but with some
notable differences in the way that requests are processed.
Overall processing is handled by a WebTestRequestHandler,
which is a subclass of BaseHTTPRequestHandler. This is responsible
for parsing the incoming request. A RequestRewriter is then
applied and may change the request data if it matches a
supplied rule.

Once the request data had been finalised, Request and Reponse
objects are constructed. These are used by the other parts of the
system to read information about the request and manipulate the
response.

Each request is handled by a particular handler function. The
mapping between Request and the appropriate handler is determined
by a Router. By default handlers are installed to interpret files
under the document root with .py extensions as executable python
files (see handlers.py for the api for such files), .asis files as
bytestreams to be sent literally and all other files to be served
statically.

The handler functions are responsible for either populating the
fields of the response object, which will then be written when the
handler returns, or for directly writing to the output stream.
"""

class Router(object):
    """Object for matching handler functions to requests.

    :param doc_root: Absolute path of the filesystem location from
                     which to serve tests
    :param routes: Initial routes to add; a list of three item tuples
                   (method, path_regexp_string, handler_function), defined
                   as for register()
    """

    def __init__(self, doc_root, routes):
        self.doc_root = doc_root
        self.routes = []
        for route in reversed(routes):
            self.register(*route)

    def register(self, methods, path_regexp, handler):
        """Register a handler for a set of paths.

        :param methods: Set of methods this should match. "*" is a
                        special value indicating that all methods should
                        be matched.

        :param path_regexp: String that can be compiled into a regexp that
                            is matched against the request path.

        :param handler: Function that will be called to process matching
                        requests. This must take two parameters, the request
                        object and the response object.
        """
        if type(methods) in types.StringTypes:
            methods = [methods]
        for method in methods:
            self.routes.append((method, re.compile(path_regexp), handler))

    def get_handler(self, request):
        """Get a handler for a request or None if there is no handler.

        :param request: Request to get a handler for.
        :rtype: Callable or None
        """
        for method, regexp, handler in reversed(self.routes):
            if (request.method == method or
                method == "*" or
                (request.method == "GET" and method == "HEAD")):
                m = regexp.match(request.url_parts.path)
                if m:
                    if not hasattr(handler, "__class__"):
                        name = handler.__name__
                    else:
                        name = handler.__class__.__name__
                    logger.debug("Found handler %s" % name)
                    request.route_match = m
                    return handler
        return None

class RequestRewriter(object):
    def __init__(self, rules):
        """Object for rewriting the request path.

        :param rules: Initial rules to add; a list of three item tuples
                      (method, input_path, output_path), defined as for
                      register()
        """
        self.rules = {}
        for rule in reversed(rules):
            self.register(*rule)

    def register(self, methods, input_path, output_path):
        """Register a rewrite rul.

        :param methods: Set of methods this should match. "*" is a
                        special value indicating that all methods should
                        be matched.

        :param input_path: Path to match for the initial request.

        :param output_path: Path to replace the input path with in
                            the request.
        """
        if type(methods) in types.StringTypes:
            methods = [methods]
        self.rules[input_path] = (methods, output_path)

    def rewrite(self, request_handler):
        """Rewrite the path in a BaseHTTPRequestHandler instance, if
           it matches a rule.

        :param request_handler: BaseHTTPRequestHandler for which to
                                rewrite the request.
        """
        if request_handler.path in self.rules:
            methods, destination = self.rules[request_handler.path]
            if "*" in methods or request_handler.command in methods:
                logger.debug("Rewriting request path %s to %s" % (request_handler.path, destination))
                request_handler.path = destination

#TODO: support SSL
class WebTestServer(ThreadingMixIn, BaseHTTPServer.HTTPServer):
    allow_reuse_address = True
    acceptable_errors = (errno.EPIPE, errno.ECONNABORTED)

    def __init__(self, server_address, RequestHandlerClass, router, rewriter, config=None,
                 use_ssl=False, certificate=None, **kwargs):
        """Server for HTTP(s) Requests

        :param server_address: tuple of (server_name, port)

        :param RequestHandlerClass: BaseHTTPRequestHandler-like class to use for
                                    handling requests.

        :param router: Router instance to use for matching requests to handler
                       functions

        :param rewriter: RequestRewriter-like instance to use for preprocessing
                         requests before they are routed

        :param config: Dictionary holding environment configuration settings for
                       handlers to read, or None to use the default values.

        :param use_ssl: Boolean indicating whether the server should use SSL

        :certificate: Certificate to use if SSL is enabled.
        """
        self.router = router
        self.rewriter = rewriter

        self.scheme = "https" if use_ssl else "http"

        if config is not None:
            Server.config = config
        else:
            logger.debug("Using default configuration")
            Server.config = {"host":server_address[0],
                             "domains":{"": server_address[0]},
                             "ports":{"http":[server_address[1]]}}

        #super doesn't work here because BaseHTTPServer.HTTPServer is old-style
        BaseHTTPServer.HTTPServer.__init__(self, server_address, RequestHandlerClass, **kwargs)

        if use_ssl:
            self.socket = ssl.wrap_socket(self.socket,
                                          certfile=certificate,
                                          server_side=True)

    def handle_error(self, request, client_address):
        error = sys.exc_value

        if ((isinstance(error, socket.error) and
             isinstance(error.args, tuple) and
             error.args[0] in self.acceptable_errors)
            or
            (isinstance(error, IOError) and
             error.errno in self.acceptable_errors)):
            pass  # remote hang up before the result is sent
        else:
            logging.error(error)


class WebTestRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    """RequestHandler for WebTestHttpd"""

    def handle_one_request(self):
        try:
            self.close_connection = False
            request_line_is_valid = self.get_request_line()

            if self.close_connection:
                return

            request_is_valid = self.parse_request()
            if not request_is_valid:
                #parse_request() actually sends its own error responses
                return

            self.server.rewriter.rewrite(self)

            request = Request(self)
            response = Response(self, request)

            if not request_line_is_valid:
                response.set_error(414)
                response.write()
                return

            logger.debug("%s %s" % (request.method, request.request_path))
            handler = self.server.router.get_handler(request)
            if handler is None:
                response.set_error(404)
            else:
                try:
                    handler(request, response)
                except HTTPException as e:
                    response.set_error(e.code, e.message)
                except Exception as e:
                    if e.message:
                        err = e.message
                    else:
                        err = traceback.format_exc()
                    response.set_error(500, err)
            logger.info("%i %s %s %i" % (response.status[0], request.method, request.request_path, request.raw_input.length))
            if not response.writer.content_written:
                response.write()

        except socket.timeout, e:
            self.log_error("Request timed out: %r", e)
            self.close_connection = 1
            return

        except HTTPException as e:
            if e.code == 500:
                logger.error(e.message)

    def get_request_line(self):
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
                self.requestline = ''
                self.request_version = ''
                self.command = ''
                return False
        if not self.raw_requestline:
            self.close_connection = 1
        return True

class WebTestHttpd(object):
    """
    :param host: Host from which to serve (default: 127.0.0.1)
    :param port: Port from which to serve (default: 8000)
    :param server_cls: Class to use for the server (default depends on ssl vs non-ssl)
    :param handler_cls: Class to use for the RequestHandler
    :param use_ssl: Use a SSL server if no explicit server_cls is supplied
    :param certificate: Certificate file to use if ssl is enabled
    :param router_cls: Router class to use when matching URLs to handlers
    :param routes: Document root for serving files
    :param routes: List of routes with which to initalize the router
    :param rewriter_cls: Class to use for request rewriter
    :param rewrites: List of rewrites with which to initalize the rewriter_cls

    HTTP server designed for testing scenarios.

    Takes a router class which provides one method get_handler which takes a Request
    and returns a handler function."
    """
    def __init__(self, host="127.0.0.1", port=8000,
                 server_cls=None, handler_cls=WebTestRequestHandler,
                 use_ssl=False, certificate=None, router_cls=Router,
                 doc_root=os.curdir, routes=routes.routes,
                 rewriter_cls=RequestRewriter, rewrites=None):

        self.host = host

        self.router = router_cls(doc_root, routes)
        self.rewriter = rewriter_cls(rewrites if rewrites is not None else [])

        self.use_ssl = use_ssl

        if server_cls is None:
            server_cls = WebTestServer

        if use_ssl:
            assert certificate is not None and os.path.exists(certificate)

        self.httpd = server_cls((host, port),
                                handler_cls,
                                self.router,
                                self.rewriter,
                                use_ssl=use_ssl,
                                certificate=certificate)
        self.started = False

        _host, self.port = self.httpd.socket.getsockname()

    def start(self, block=False):
        """Start the server.

        :param block: True to run the server on the current thread, blocking,
                      False to run on a seperate thread."""
        logger.info("Starting http server on %s:%s" % (self.host, self.port))
        self.started = True
        if block:
            self.httpd.serve_forever()
        else:
            self.server_thread = threading.Thread(target=self.httpd.serve_forever)
            self.server_thread.setDaemon(True) # don't hang on exit
            self.server_thread.start()

    def stop(self):
        """
        Stops the server.

        If the server is not running, this method has no effect.
        """
        if self.started:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
                self.server_thread.join()
                self.server_thread = None
                logger.info("Stopped http server on %s:%s" % (self.host, self.port))
            except AttributeError:
                pass
            self.started = False
        self.httpd = None

    def get_url(self, path="/", query=None, fragment=None):
        if not self.started:
            return None

        return urlparse.urlunsplit(("http" if not self.use_ssl else "https",
                                    "%s:%s" % (self.host, self.port),
                                    path, query, fragment))
