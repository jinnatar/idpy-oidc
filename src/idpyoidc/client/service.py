""" The basic Service class upon which all the specific services are built. """
import copy
import json
import logging
from typing import Callable
from typing import List
from typing import Optional
from typing import Union
from urllib.parse import urlparse

from cryptojwt.exception import IssuerNotFound
from cryptojwt.jwe.jwe import factory as jwe_factory
from cryptojwt.jws.jws import factory as jws_factory
from cryptojwt.jwt import JWT
from idpyoidc.exception import MissingSigningKey

from idpyoidc.client.exception import Unsupported
from idpyoidc.impexp import ImpExp
from idpyoidc.item import DLDict
from idpyoidc.message import Message
from idpyoidc.message.oauth2 import ResponseMessage
from idpyoidc.message.oauth2 import is_error_message
from idpyoidc.util import importer

from ..constant import JOSE_ENCODED
from ..constant import JSON_ENCODED
from ..constant import URL_ENCODED
from .client_auth import client_auth_setup
from .client_auth import method_to_item
from .client_auth import single_authn_setup
from .configure import Configuration
from .exception import ResponseError
from .util import get_http_body
from .util import get_http_url

__author__ = "Roland Hedberg"

from ..context import OidcContext

LOGGER = logging.getLogger(__name__)

SUCCESSFUL = [200, 201, 202, 203, 204, 205, 206]

SPECIAL_ARGS = ["authn_endpoint", "algs"]

REQUEST_INFO = "Doing request with: URL:{}, method:{}, data:{}, https_args:{}"


class Service(ImpExp):
    """The basic Service class."""

    msg_type = Message
    response_cls = Message
    error_msg = ResponseMessage
    endpoint_name = ""
    endpoint = ""
    service_name = ""
    synchronous = True
    default_authn_method = ""
    http_method = "GET"
    request_body_type = "urlencoded"
    response_body_type = "json"

    parameter = {
        "default_authn_method": None,
        "endpoint": "",
        "error_msg": object,
        "http_method": None,
        "msg_type": object,
        "request_body_type": None,
        "response_body_type": None,
        "response_cls": object,
    }

    init_args = ["upstream_get"]

    _include = {}
    _supports = {}
    _callback_path = {}

    def __init__(
        self, upstream_get: Callable, conf: Optional[Union[dict, Configuration]] = None, **kwargs
    ):
        ImpExp.__init__(self)

        self.upstream_get = upstream_get
        self.default_request_args = {}
        self.client_authn_methods = {}

        if conf:
            LOGGER.debug(f"Service config: {conf}")
            self.conf = conf
            for param in [
                "msg_type",
                "response_cls",
                "error_msg",
                "http_method",
                "request_body_type",
                "response_body_type",
                "default_authn_method",
            ]:
                if param in conf:
                    setattr(self, param, conf[param])

            _default_request_args = conf.get("request_args", {})
            if _default_request_args:
                self.default_request_args = _default_request_args
                del conf["request_args"]

            _client_authn_methods = conf.get("client_authn_methods", None)
            if _client_authn_methods:
                self.client_authn_methods = client_auth_setup(method_to_item(_client_authn_methods))

            if self.default_authn_method:
                if self.default_authn_method not in self.client_authn_methods:
                    self.client_authn_methods[self.default_authn_method] = single_authn_setup(
                        self.default_authn_method, None
                    )

        else:
            self.conf = {}
            if self.default_authn_method:
                self.client_authn_methods[self.default_authn_method] = single_authn_setup(
                    self.default_authn_method, None
                )

        # pull in all the modifiers
        self.pre_construct = []
        self.post_construct = []
        self.construct_extra_headers = []
        self.post_parse_process = []

    def gather_request_args(self, **kwargs):
        """
        Go through the attributes that the message class can contain and
        add values if they are missing but exists in the client info or
        when there are default values.

        :param kwargs: Initial set of attributes.
        :return: Possibly augmented set of attributes
        """
        ar_args = kwargs.copy()

        _context = self.upstream_get("context")
        _use = _context.collect_usage()
        if not _use:
            _use = _context.map_preferred_to_registered()

        if "request_args" in self.conf:
            ar_args.update(self.conf["request_args"])

        # Go through the list of claims defined for the message class.
        # There are a couple of places where information can be found.
        # Access them in the order of priority
        # 1. A keyword argument
        # 2. configured set of default attribute values
        # 3. default attribute values defined in the OIDC standard document
        for prop in self.msg_type.c_param:
            if prop in ar_args:
                continue

            val = _use.get(prop)
            if not val:
                # val = request_claim(_context, prop)
                # if not val:
                val = self.default_request_args.get(prop)

            if val:
                ar_args[prop] = val

        for key, val in self.default_request_args.items():
            if key not in ar_args:
                ar_args[key] = val

        return ar_args

    def method_args(self, context, **kwargs):
        """
        Collect the set of arguments that should be used by a set of methods

        :param context: Which service we're working for
        :param kwargs: A set of keyword arguments that are added at run-time.
        :return: A set of keyword arguments
        """
        try:
            _args = self.conf[context].copy()
        except KeyError:
            _args = kwargs
        else:
            _args.update(kwargs)
        return _args

    def do_pre_construct(self, request_args, **kwargs):
        """
        Will run the pre_construct methods one by one in the order given.

        :param request_args: Request arguments
        :param kwargs: Extra key word arguments
        :return: A tuple of request_args and post_args. post_args are to be
            used by the post_construct methods.
        """

        _args = self.method_args("pre_construct", **kwargs)
        post_args = {}
        for meth in self.pre_construct:
            request_args, _post_args = meth(
                request_args, service=self, post_args=post_args, **_args
            )
            # Not necessarily independent
            # post_args.update(_post_args)

        return request_args, post_args

    def do_post_construct(self, request_args, **kwargs):
        """
        Will run the post_construct methods one at the time in order.

        :param request_args: Request arguments
        :param kwargs: Arguments used by the post_construct method
        :return: Possible modified set of request arguments.
        """
        _args = self.method_args("post_construct", **kwargs)

        for meth in self.post_construct:
            request_args = meth(request_args, service=self, **_args)

        return request_args

    def update_service_context(self, resp: Message, key: Optional[str] = "", **kwargs):
        """
        A method run after the response has been parsed and verified.

        :param resp: The response as a :py:class:`idpyoidc.Message` instance
        :param key: The key under which the response should be stored
        :param kwargs: Extra key word arguments
        """
        pass

    def construct(self, request_args: Optional[dict] = None, **kwargs):
        """
        Instantiate the request as a message class instance with
        attribute values gathered in a pre_construct method or in the
        gather_request_args method.

        :param request_args:
        :param kwargs: extra keyword arguments
        :return: message class instance
        """
        if request_args is None:
            request_args = {}

        # run the pre_construct methods. Will return a possibly new
        # set of request arguments but also a set of arguments to
        # be used by the post_construct methods.
        request_args, post_args = self.do_pre_construct(request_args, **kwargs)

        # If 'state' appears among the keyword argument and is not
        # expected to appear in the request, remove it.
        if "state" in self.msg_type.c_param and "state" in kwargs:
            # Don't overwrite something put there by the constructor
            if "state" not in request_args:
                request_args["state"] = kwargs["state"]

        # logger.debug("request_args: %s" % sanitize(request_args))
        _args = self.gather_request_args(**request_args)

        # logger.debug("kwargs: %s" % sanitize(kwargs))
        # initiate the request as in an instance of the self.msg_type
        # message type
        request = self.msg_type(**_args)

        _behaviour_args = kwargs.get("behaviour_args")
        if _behaviour_args:
            post_args.update(_behaviour_args)

        return self.do_post_construct(request, **post_args)

    def init_authentication_method(self, request, authn_method, http_args=None, **kwargs):
        """
        Will run the proper client authentication method.
        Each such method will place the necessary information in the necessary
        place. A method may modify the request.

        :param request: The request, a Message class instance
        :param authn_method: Client authentication method
        :param http_args: HTTP header arguments
        :param kwargs: Extra keyword arguments
        :return: Extended set of HTTP header arguments
        """
        if http_args is None:
            http_args = {}

        if authn_method:
            LOGGER.debug("Client authn method: %s", authn_method)
            if self.client_authn_methods and authn_method in self.client_authn_methods:
                _func = self.client_authn_methods[authn_method]
            else:
                _context = self.upstream_get("context")
                try:
                    _func = _context.client_authn_methods[authn_method]
                except KeyError:  # not one of the common
                    LOGGER.error(f"Unknown client authentication method: {authn_method}")
                    raise Unsupported(f"Unknown client authentication method: {authn_method}")

            return _func.construct(request=request, service=self, http_args=http_args, **kwargs)

        return http_args

    def construct_request(self, request_args=None, **kwargs):
        """
        The method where everything is setup for sending the request.
        The request information is gathered and the where and how of sending the
        request is decided.

        :param request_args: Initial request arguments as a dictionary
        :param kwargs: Extra keyword arguments
        :return: A dictionary with the keys 'url' and possibly 'body', 'kwargs',
            'request' and 'ht_args'.
        """
        if request_args is None:
            request_args = {}

        return self.construct(request_args, **kwargs)

    def get_endpoint(self):
        """
        Find the service endpoint

        :return: The service endpoint (a URL)
        """
        if self.endpoint:
            return self.endpoint

        return self.upstream_get("context").provider_info[self.endpoint_name]

    def get_authn_header(
        self, request: Union[dict, Message], authn_method: Optional[str] = "", **kwargs
    ) -> dict:
        """
        Construct an authorization specification to be sent in the
        HTTP header.

        :param request: The service request
        :param authn_method: Which authentication/authorization method to use
        :param kwargs: Extra keyword arguments
        :return: A set of keyword arguments to be sent in the HTTP header.
        """
        headers = {}
        # If I should deal with client authentication
        if authn_method:
            h_arg = self.init_authentication_method(request, authn_method, **kwargs)
            try:
                headers = h_arg["headers"]
            except KeyError:
                pass

        return headers

    def get_authn_method(self) -> str:
        """
        Find the method that the client should use to authenticate against a
        service.

        :return: The authn/authz method
        """
        return self.default_authn_method

    def get_headers(
        self,
        request: Union[dict, Message],
        http_method: str,
        authn_method: Optional[str] = "",
        **kwargs,
    ) -> dict:
        """

        :param request:
        :param authn_method:
        :param kwargs:
        :return:
        """
        if not authn_method:
            authn_method = self.get_authn_method()

        _headers = self.get_authn_header(
            request, authn_method=authn_method, authn_endpoint=self.endpoint_name, **kwargs
        )

        _authz = _headers.get("Authorization")
        if _authz:
            if _authz.startswith("Bearer") or _authz.startswith("DPoP"):
                kwargs["token"] = _authz.split(" ")[1]

        for meth in self.construct_extra_headers:
            _headers = meth(
                self.upstream_get("context"),
                headers=_headers,
                request=request,
                authn_method=authn_method,
                service_endpoint=self.endpoint_name,
                http_method=http_method,
                **kwargs,
            )

        return _headers

    def get_request_parameters(
        self, request_args=None, method="", request_body_type="", authn_method="", **kwargs
    ) -> dict:
        """
        Builds the request message and constructs the HTTP headers.

        This is the starting point for a pipeline that will:

        - construct the request message
        - add/remove information to/from the request message in the way a
            specific client authentication method requires.
        - gather a set of HTTP headers like Content-type and Authorization.
        - serialize the request message into the necessary format (JSON,
            urlencoded, signed JWT)

        :param request_body_type: Which serialization to use for the HTTP body
        :param method: HTTP method used.
        :param authn_method: Client authentication method
        :param request_args: Message arguments
        :param kwargs: extra keyword arguments
        :return: Dictionary with the necessary information for the HTTP
            request
        """
        if not method:
            method = self.http_method
        if not authn_method:
            authn_method = self.get_authn_method()
        if not request_body_type:
            request_body_type = self.request_body_type

        request = self.construct_request(request_args=request_args, **kwargs)

        LOGGER.debug("Request: %s", request)
        _info = {"method": method, "request": request}

        _args = kwargs.copy()
        _context = self.upstream_get("context")
        if _context.issuer:
            _args["iss"] = _context.issuer

        # Client authentication by usage of the Authorization HTTP header
        # or by modifying the request object
        _headers = self.get_headers(request, http_method=method, authn_method=authn_method, **_args)

        # Find out where to send this request
        try:
            endpoint_url = kwargs["endpoint"]
        except KeyError:
            endpoint_url = self.get_endpoint()

        _info["url"] = get_http_url(endpoint_url, request, method=method)

        # If there is to be a body part
        if method == "POST":
            # How should it be serialized
            if request_body_type == "urlencoded":
                content_type = URL_ENCODED
            elif request_body_type in ["jws", "jwe", "jose"]:
                content_type = JOSE_ENCODED
            else:  # request_body_type == 'json'
                content_type = JSON_ENCODED

            _info["body"] = get_http_body(request, content_type)

            _headers.update({"Content-Type": content_type})

        if _headers:
            _info["headers"] = _headers

        return _info

    # ------------------ response handling -----------------------

    @staticmethod
    def get_urlinfo(info):
        """
        Pick out the fragment or query part from a URL.

        :param info: A URL possibly containing a query or a fragment part
        :return: the query/fragment part
        """
        # If info is a whole URL pick out the query or fragment part
        if "?" in info or "#" in info:
            parts = urlparse(info)
            # either query of fragment
            if parts.query:
                info = parts.query
            else:
                info = parts.fragment
        return info

    def post_parse_response(self, response, **kwargs):
        """
        This method does post-processing of the service response.
        Each service have their own version of this method.

        :param response: The service response
        :param kwargs: A set of keyword arguments
        :return: The possibly modified response
        """
        return response

    def gather_verify_arguments(
        self, response: Optional[Union[dict, Message]] = None, behaviour_args: Optional[dict] = None
    ):
        """
        Need to add some information before running verify()

        :return: dictionary with arguments to the verify call
        """

        _context = self.upstream_get("context")
        kwargs = {
            "iss": _context.issuer,
            "keyjar": self.upstream_get("attribute", "keyjar"),
            "verify": True,
            "client_id": _context.get_client_id(),
        }

        if self.service_name == "provider_info":
            if _context.issuer.startswith("http://"):
                kwargs["allow_http"] = True

        return kwargs

    def _do_jwt(self, info):
        _context = self.upstream_get("context")
        args = {"allowed_sign_algs": _context.get_sign_alg(self.service_name)}
        enc_algs = _context.get_enc_alg_enc(self.service_name)
        args["allowed_enc_algs"] = enc_algs["alg"]
        args["allowed_enc_encs"] = enc_algs["enc"]

        _jwt = JWT(key_jar=self.upstream_get("attribute", "keyjar"), **args)
        _jwt.iss = _context.get_client_id()
        return _jwt.unpack(info)

    def _do_response(self, info, sformat, **kwargs):
        _context = self.upstream_get("context")

        if isinstance(info,  list): # Don't have support for sformat=list
            return info

        try:
            resp = self.response_cls().deserialize(info, sformat, iss=_context.issuer, **kwargs)
        except Exception as err:
            LOGGER.error("Error while deserializing: %s (1 pass)", err)
            resp = None
            if sformat == "json":
                # Could be JWS or JWE but wrongly tagged
                # Adding issuer is just a fail-safe. If one thing was wrong then two can be.
                try:
                    resp = self.response_cls().deserialize(
                        info, "jwt", iss=_context.issuer, **kwargs
                    )
                except Exception as err:
                    LOGGER.error("Error while deserializing: %s", err)
                    raise

            if resp is None:
                raise ValueError(f"Incorrect message type: {sformat}")
        return resp

    def parse_response(
        self,
        info,
        sformat: Optional[str] = "",
        state: Optional[str] = "",
        behaviour_args: Optional[dict] = None,
        **kwargs,
    ) :
        """
        This the start of a pipeline that will:

            1 Deserializes a response into it's response message class.
              Or :py:class:`idpyoidc.message.oauth2.ErrorResponse` if it's an error
              message
            2 verifies the correctness of the response by running the
              verify method belonging to the message class used.
            3 runs the do_post_parse_response method iff the response was not
              an error response.

        :param behaviour_args:
        :param info: The response, can be either in a JSON or an urlencoded format
        :param sformat: Which serialization that was used
        :param state: The state
        :param kwargs: Extra key word arguments
        :return: The parsed and to some extent verified response
        """

        if not sformat:
            sformat = self.response_body_type

        LOGGER.debug("response format: %s", sformat)

        resp = None
        _jws = _jwe = None
        if sformat == "jose":  # can be jwe, jws or json
            # the checks for JWS and JWE will be replaced with functions from cryptojwt
            _jws = info
            try:
                if jws_factory(info):
                    info = self._do_jwt(info)
            except:
                try:
                    if jwe_factory(info):
                        info = self._do_jwt(info)
                except:
                    LOGGER.debug("jwe detected")
            if info and isinstance(info, str):
                info = json.loads(info)
            sformat = "dict"
        elif sformat == "jwe":
            _keyjar = self.upstream_get("attribute", "keyjar")
            _client_id = self.upstream_get("attribute", "client_id")
            _jwe = info
            resp = self.response_cls().from_jwe(info, keys=_keyjar.get_issuer_keys(_client_id))
        # If format is urlencoded 'info' may be a URL
        # in which case I have to get at the query/fragment part
        elif sformat == "urlencoded":
            info = self.get_urlinfo(info)
        elif sformat in ["jwt", "jws"]:
            _jws = info
            info = self._do_jwt(info)
            sformat = "dict"
        elif sformat == "json":
            info = json.loads(info)
            sformat = "dict"

        LOGGER.debug("response_cls: %s", self.response_cls.__name__)

        if resp is None:
            if self.response_cls == list and info == []:
                return info
            elif not info:
                LOGGER.error("Missing or faulty response")
                raise ResponseError("Missing or faulty response")

            if sformat == "text":
                resp = info
            else:
                resp = self._do_response(info, sformat, **kwargs)
                if isinstance(resp, Message):
                    LOGGER.debug(f'Initial response parsing => "{resp.to_dict()}"')
                else:
                    LOGGER.debug(f'Initial response parsing => "{resp}"')

        # is this an error message
        if sformat == "text":
            pass
        elif is_error_message(resp):
            LOGGER.debug("Error response: %s", resp)
        elif isinstance(resp, Message):
            vargs = self.gather_verify_arguments(response=resp, behaviour_args=behaviour_args)
            LOGGER.debug("Verify response with %s", vargs)
            try:
                # verify the message. If something is wrong an exception is thrown
                resp.verify(**vargs)
            except MissingSigningKey as err:
                LOGGER.error(f"Could not find an appropriate key: {err}")
                if vargs["iss"] not in vargs["keyjar"].owners():
                    LOGGER.debug(f"Issuer {vargs['iss']} not found in keyjar")
                raise
            except Exception as err:
                LOGGER.error("Got exception while verifying response: %s", err)
                raise

            if _jws:
                resp._jws = _jws
            elif _jwe:
                resp._jwe = _jwe

            resp = self.post_parse_response(resp, state=state)

        if not resp:
            LOGGER.error("Missing or faulty response")
            raise ResponseError("Missing or faulty response")

        return resp

    def supports(self):
        res = {}
        for key, val in self._supports.items():
            if isinstance(val, Callable):
                res[key] = val()
            else:
                res[key] = val
        return res

    def extends(self, info):
        for claim, val in self._include.items():
            if claim in info:
                info[claim].extend(val)
            else:
                info[claim] = copy.copy(val)
        return info

    def get_callback_path(self, callback):
        return self._callback_path.get(callback)

    @staticmethod
    def get_uri(base_url, path, hex):
        return f"{base_url}/{path}/{hex}"

    def construct_uris(
        self,
        base_url: str,
        hex: bytes,
        context: OidcContext,
        targets: Optional[List[str]] = None,
        response_types: Optional[list] = None,
    ):
        if not targets:
            targets = self._callback_path.keys()

        if not targets:
            return {}

        _callback_uris = context.get_preference("callback_uris", {})
        for uri in targets:
            if uri in _callback_uris:
                pass
            else:
                _path = self._callback_path.get(uri)
                if isinstance(_path, str):
                    _callback_uris[uri] = self.get_uri(base_url, _path, hex)
                else:
                    _callback_uris[uri] = [self.get_uri(base_url, _var, hex) for _var in _path]

        return _callback_uris

    def supported(self, claim):
        return claim in self._supports

    def callback_uris(self):
        return list(self._callback_path.keys())


def init_services(service_definitions, upstream_get):
    """
    Initiates a set of services

    :param service_definitions: A dictionary containing service definitions
    :param upstream_get: A function that returns different things from the base entity.
    :return: A dictionary, with service name as key and the service instance as
        value.
    """
    service = DLDict()
    for service_name, service_configuration in service_definitions.items():
        try:
            kwargs = {"conf": service_configuration["kwargs"]}
        except KeyError:
            kwargs = {}

        kwargs.update({"upstream_get": upstream_get})

        if isinstance(service_configuration["class"], str):
            _cls = importer(service_configuration["class"])
            _srv = _cls(**kwargs)
        else:
            _srv = service_configuration["class"](**kwargs)

        service[_srv.service_name] = _srv

    return service
