#####################################################################################
#
#  Copyright (C) Tavendo GmbH
#
#  Unless a separate license agreement exists between you and Tavendo GmbH (e.g. you
#  have purchased a commercial license), the license terms below apply.
#
#  Should you enter into a separate license agreement after having received a copy of
#  this software, then the terms of such license agreement replace the terms below at
#  the time at which such license agreement becomes effective.
#
#  In case a separate license agreement ends, and such agreement ends without being
#  replaced by another separate license agreement, the license terms below apply
#  from the time at which said agreement ends.
#
#  LICENSE TERMS
#
#  This program is free software: you can redistribute it and/or modify it under the
#  terms of the GNU Affero General Public License, version 3, as published by the
#  Free Software Foundation. This program is distributed in the hope that it will be
#  useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
#  See the GNU Affero General Public License Version 3 for more details.
#
#  You should have received a copy of the GNU Affero General Public license along
#  with this program. If not, see <http://www.gnu.org/licenses/agpl-3.0.en.html>.
#
#####################################################################################

import datetime
import json
import hmac
import hashlib
import base64

from crossbar._logging import make_logger
from crossbar._compat import native_string

from netaddr.ip import IPAddress, IPNetwork

from twisted.web import server
from twisted.web.resource import Resource

from autobahn.websocket.utf8validator import Utf8Validator
_validator = Utf8Validator()


_ALLOWED_CONTENT_TYPES = set([b'application/json'])


class _InvalidUnicode(BaseException):
    """
    Invalid Unicode was found.
    """


class _CommonResource(Resource):
    """
    Shared components between PublisherResource and CallerResource.
    """
    isLeaf = True
    decode_as_json = True

    def __init__(self, options, session):
        """
        Ctor.

        :param options: Options for path service from configuration.
        :type options: dict
        :param session: Instance of `ApplicationSession` to be used for forwarding events.
        :type session: obj
        """
        Resource.__init__(self)
        self._options = options
        self._session = session
        self.log = make_logger()

        self._key = None
        if 'key' in options:
            self._key = options['key'].encode('utf8')

        self._secret = None
        if 'secret' in options:
            self._secret = options['secret'].encode('utf8')

        self._post_body_limit = int(options.get('post_body_limit', 0))
        self._timestamp_delta_limit = int(options.get('timestamp_delta_limit', 300))

        self._require_ip = None
        if 'require_ip' in options:
            self._require_ip = [IPNetwork(net) for net in options['require_ip']]

        self._require_tls = options.get('require_tls', None)

    def _deny_request(self, request, code, reason, **kwargs):
        """
        Called when client request is denied.
        """
        if "log_category" not in kwargs.keys():
            kwargs["log_category"] = "AR" + str(code)

        self.log.debug("[request denied] - {code} / " + reason,
                       code=code, **kwargs)

        request.setResponseCode(code)
        return reason.format(**kwargs).encode('utf8') + b"\n"

    def _fail_request(self, request, code, reason, body=None, **kwargs):
        """
        Called when client request fails.
        """
        if "log_category" not in kwargs.keys():
            kwargs["log_category"] = "AR" + str(code)

        self.log.failure(None, log_failure=kwargs["log_failure"])
        self.log.debug("[request failure] - {code} / " + reason,
                       code=code, **kwargs)

        request.setResponseCode(code)
        if body:
            request.write(body)
        else:
            request.write(reason.format(**kwargs).encode('utf8') + b"\n")

    def _complete_request(self, request, code, body, reason="", **kwargs):
        """
        Called when client request is complete.
        """
        if "log_category" not in kwargs.keys():
            kwargs["log_category"] = "AR" + str(code)

        self.log.debug("[request succeeded] - {code} / " + reason,
                       code=code, reason=reason, **kwargs)
        request.setResponseCode(code)
        request.write(body)

    def _set_common_headers(self, request):
        """
        Set common HTTP response headers.
        """
        origin = request.getHeader(b'origin')
        if origin is None or origin == b'null':
            origin = b'*'
        request.setHeader(b'access-control-allow-origin', origin)
        request.setHeader(b'access-control-allow-credentials', b'true')
        request.setHeader(b'cache-control', b'no-store,no-cache,must-revalidate,max-age=0')

        headers = request.getHeader(b'access-control-request-headers')
        if headers is not None:
            request.setHeader(b'access-control-allow-headers', headers)

    def render(self, request):
        self.log.debug("[render] method={request.method} path={request.path} args={request.args}",
                       request=request)

        try:
            if request.method not in (b"POST", b"PUT", b"OPTIONS"):
                return self._deny_request(request, 405, u"HTTP/{0} not allowed (only HTTP/POST or HTTP/PUT)".format(native_string(request.method)))
            else:
                self._set_common_headers(request)

                if request.method == b"OPTIONS":
                    # http://greenbytes.de/tech/webdav/rfc2616.html#rfc.section.14.7
                    request.setHeader(b'allow', b'POST,PUT,OPTIONS')

                    # https://www.w3.org/TR/cors/#access-control-allow-methods-response-header
                    request.setHeader(b'access-control-allow-methods', b'POST,PUT,OPTIONS')

                    request.setResponseCode(200)
                    return b''
                else:
                    return self._render_request(request)
        except Exception as e:
            self.log.failure("Unhandled server error. {exc}", exc=e)
            return self._deny_request(request, 500, "Unhandled server error.", exc=e)

    def _render_request(self, request):
        """
        Receives an HTTP/POST|PUT request, and then calls the Publisher/Caller
        processor.
        """
        # read HTTP/POST|PUT body
        body = request.content.read()

        args = {native_string(x): y[0] for x, y in request.args.items()}
        headers = request.requestHeaders

        # check content type + charset encoding
        #
        content_type_header = headers.getRawHeaders(b"content-type", [])

        if len(content_type_header) > 0:
            content_type_elements = [
                x.strip().lower()
                for x in content_type_header[0].split(b";")
            ]
        else:
            content_type_elements = []

        if self.decode_as_json:
            # if the client sent a content type, it MUST be one of _ALLOWED_CONTENT_TYPES
            # (but we allow missing content type .. will catch later during JSON
            # parsing anyway)
            if len(content_type_elements) > 0 and \
               content_type_elements[0] not in _ALLOWED_CONTENT_TYPES:
                return self._deny_request(
                    request, 400,
                    u"bad content type: if a content type is present, it MUST be one of '{}', not '{}'".format(list(_ALLOWED_CONTENT_TYPES), content_type_elements[0]),
                    log_category="AR452"
                )

        encoding_parts = {}

        if len(content_type_elements) > 1:
            try:
                for item in content_type_elements:
                    if b"=" not in item:
                        # Don't bother looking at things "like application/json"
                        continue

                    # Parsing things like:
                    # charset=utf-8
                    _ = native_string(item).split("=")
                    assert len(_) == 2

                    # We don't want duplicates
                    key = _[0].strip().lower()
                    assert key not in encoding_parts
                    encoding_parts[key] = _[1].strip().lower()
            except:
                return self._deny_request(request, 400,
                                          u"mangled Content-Type header",
                                          log_category="AR450")

        charset_encoding = encoding_parts.get("charset", "utf-8")

        if charset_encoding not in ["utf-8", 'utf8']:
            return self._deny_request(
                request, 400,
                (u"'{charset_encoding}' is not an accepted charset encoding, "
                 u"must be utf-8"),
                log_category="AR450",
                charset_encoding=charset_encoding)

        # enforce "post_body_limit"
        #
        body_length = len(body)
        content_length_header = headers.getRawHeaders(b"content-length", [])

        if len(content_length_header) == 1:
            content_length = int(content_length_header[0])
        elif len(content_length_header) > 1:
            return self._deny_request(
                request, 400,
                u"Multiple Content-Length headers are not allowed")
        else:
            content_length = body_length

        if body_length != content_length:
            # Prevent the body length from being different to the given
            # Content-Length. This is so that clients can't lie and bypass
            # length restrictions by giving an incorrect header with a large
            # body.
            return self._deny_request(request, 400, u"HTTP/POST|PUT body length ({0}) is different to Content-Length ({1})".format(body_length, content_length))

        if self._post_body_limit and content_length > self._post_body_limit:
            return self._deny_request(
                request, 413,
                u"HTTP/POST|PUT body length ({0}) exceeds maximum ({1})".format(content_length, self._post_body_limit)
            )

        #
        # parse/check HTTP/POST|PUT query parameters
        #

        # key
        #
        if 'key' in args:
            key_str = args["key"]
        else:
            if self._secret:
                return self._deny_request(
                    request, 400, u"signed request required, but mandatory 'key' field missing",
                    log_category="AR461")

        # timestamp
        #
        if 'timestamp' in args:
            timestamp_str = args["timestamp"]
            try:
                ts = datetime.datetime.strptime(native_string(timestamp_str), "%Y-%m-%dT%H:%M:%S.%fZ")
                delta = abs((ts - datetime.datetime.utcnow()).total_seconds())
                if self._timestamp_delta_limit and delta > self._timestamp_delta_limit:
                    return self._deny_request(
                        request, 400, u"request expired (delta {0} seconds)".format(delta),
                        log_category="AR462")
            except ValueError as e:
                return self._deny_request(
                    request, 400,
                    u"invalid timestamp '{0}' (must be UTC/ISO-8601, e.g. '2011-10-14T16:59:51.123Z')".format(native_string(timestamp_str)),
                    log_category="AR462")
        else:
            if self._secret:
                return self._deny_request(
                    request, 400, u"signed request required, but mandatory 'timestamp' field missing",
                    log_category="AR461")

        # seq
        #
        if 'seq' in args:
            seq_str = args["seq"]
            try:
                # FIXME: check sequence
                seq = int(seq_str)  # noqa
            except:
                return self._deny_request(
                    request, 400, u"invalid sequence number '{0}' (must be an integer)".format(native_string(seq_str)),
                    log_category="AR462")
        else:
            if self._secret:
                return self._deny_request(
                    request, 400, u"signed request required, but mandatory 'seq' field missing",
                    log_category="AR461")

        # nonce
        #
        if 'nonce' in args:
            nonce_str = args["nonce"]
            try:
                # FIXME: check nonce
                nonce = int(nonce_str)  # noqa
            except:
                return self._deny_request(
                    request, 400, u"invalid nonce '{0}' (must be an integer)".format(native_string(nonce_str)),
                    log_category="AR462")
        else:
            if self._secret:
                return self._deny_request(
                    request, 400, u"signed request required, but mandatory 'nonce' field missing",
                    log_category="AR461")

        # signature
        #
        if 'signature' in args:
            signature_str = args["signature"]
        else:
            if self._secret:
                return self._deny_request(
                    request, 400, u"signed request required, but mandatory 'signature' field missing",
                    log_category="AR461")

        # do more checks if signed requests are required
        #
        if self._secret:

            if key_str != self._key:
                return self._deny_request(
                    request, 401, u"unknown key '{0}' in signed request".format(native_string(key_str)),
                    log_category="AR460")

            # Compute signature: HMAC[SHA256]_{secret} (key | timestamp | seq | nonce | body) => signature
            hm = hmac.new(self._secret, None, hashlib.sha256)
            hm.update(key_str)
            hm.update(timestamp_str)
            hm.update(seq_str)
            hm.update(nonce_str)
            hm.update(body)
            signature_recomputed = base64.urlsafe_b64encode(hm.digest())

            if signature_str != signature_recomputed:
                return self._deny_request(request, 401, u"invalid request signature",
                                          log_category="AR459")
            else:
                self.log.debug("REST request signature valid.",
                               log_category="AR203")

        # user_agent = headers.get("user-agent", "unknown")
        client_ip = request.getClientIP()
        is_secure = request.isSecure()

        # enforce client IP address
        #
        if self._require_ip:
            ip = IPAddress(native_string(client_ip))
            allowed = False
            for net in self._require_ip:
                if ip in net:
                    allowed = True
                    break
            if not allowed:
                return self._deny_request(request, 400, u"request denied based on IP address")

        # enforce TLS
        #
        if self._require_tls:
            if not is_secure:
                return self._deny_request(request, 400, u"request denied because not using TLS")

        # FIXME: authorize request
        authorized = True

        if not authorized:
            return self._deny_request(request, 401, u"not authorized")

        _validator.reset()
        validation_result = _validator.validate(body)

        # validate() returns a 4-tuple, of which item 0 is whether it
        # is valid
        if not validation_result[0]:
            return self._deny_request(
                request, 400,
                u"invalid request event - HTTP/POST|PUT body was invalid UTF-8",
                log_category="AR451")

        event = body.decode('utf8')

        if self.decode_as_json:
            try:
                event = json.loads(event)
            except Exception as e:
                return self._deny_request(
                    request, 400,
                    (u"invalid request event - HTTP/POST|PUT body must be "
                     u"valid JSON: {exc}"), exc=e, log_category="AR453")

            if not isinstance(event, dict):
                return self._deny_request(
                    request, 400,
                    (u"invalid request event - HTTP/POST|PUT body must be "
                     u"a JSON dict"), log_category="AR454")

        d = self._process(request, event)

        if isinstance(d, bytes):
            # If it's bytes, return it directly
            return d
        else:
            # If it's a Deferred, let it run.
            d.addCallback(lambda _: request.finish())

        return server.NOT_DONE_YET

    def _process(self, request, event):
        raise NotImplementedError()
