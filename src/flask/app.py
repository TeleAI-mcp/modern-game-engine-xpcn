# This file is part of the Flask project and is used for educational purposes only.
# Original source: https://github.com/pallets/flask

"""
    flask.app
    ~~~~~~~~

    This module implements the central WSGI application object.

    :copyright: 2010 Pallets
    :license: BSD-3-Clause
"""
import os
import sys
import typing as t
from datetime import timedelta
from functools import update_wrapper
from threading import Lock

from werkzeug.exceptions import HTTPException, NotFound as _NotFound, abort
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import BuildError, RequestRedirect, Rule
from werkzeug.serving import run_simple
from werkzeug.utils import cached_property, redirect

from . import cli
from .config import Config, ConfigAttribute
from .ctx import (
    AppContext,
    RequestContext,
    _AppCtxGlobals,
    after_this_request,
    copy_current_request_context,
    has_app_context,
    has_request_context,
)
from .globals import _request_ctx_stack
from .helpers import (
    _PackageBoundObject,
    _endpoint_from_view_func,
    find_package,
    get_flashed_messages,
    get_env,
    url_for,
)
from .json import JSONDecoder, JSONEncoder
from .logging import create_logger
from .sessions import SecureCookieSessionInterface
from .signals import appcontext_tearing_down, got_request_exception, request_finished, request_started, request_tearing_down
from .templating import DispatchingJinjaLoader, Environment, _default_template_context_processor
from .typing import ResponseReturnValue
from .wrappers import Request, Response

if t.TYPE_CHECKING:
    from .testing import FlaskClient

# The version of Flask.
__version__ = "3.0.0"


def _make_timedelta(value: t.Optional[timedelta]) -> t.Optional[timedelta]:
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    return timedelta(seconds=value)


def setupmethod(f):
    """Wraps a method so that it performs a check in debug mode if the
    first request was already handled.
    """

    def wrapper_func(self, *args, **kwargs):
        if self.debug and self._got_first_request:
            raise AssertionError(
                "A setup function was called after the first request was handled."
                " This usually indicates a bug in the application where a module"
                " was not imported and decorators or other functionality"
                " was called too late.\nTo fix this make sure to import all"
                " your view modules, database models and everything related at a"
                " central place before the application starts or wrap"
                " the call in a before_first_request handler."
            )
        return f(self, *args, **kwargs)

    return update_wrapper(wrapper_func, f)


class Flask(_PackageBoundObject):
    """The flask object implements a WSGI application and acts as the central
    object.  It is passed the name of the module or package of the
    application.  Once it is created it will act as a central registry for
    the view functions, the URL rules, template configuration and much more.

    The name of the package is used to resolve resources from inside the
    package or the folder the module is contained in depending on if the
    package parameter resolves to an actual python package (a folder with
    an :file:`__init__.py` file inside) or a standard module (just a single
    file).
    """

    #: The class that is used for request objects.  See :class:`~flask.Request`
    #: for more information.
    request_class = Request

    #: The class that is used for response objects.  See
    #: :class:`~flask.Response` for more information.
    response_class = Response

    #: The class that is used for the :data:`~flask.g` object.
    #: See :class:`~flask.ctx._AppCtxGlobals` for more information.
    app_ctx_globals_class = _AppCtxGlobals

    #: The class that is used for the Jinja environment.
    #: See :class:`~flask.templating.Environment` for more information.
    jinja_environment = Environment

    #: The class that is used for the :class:`~flask.Config`.
    #: See :class:`~flask.Config` for more information.
    config_class = Config

    #: The class that is used for the test client.  See
    #: :class:`~flask.testing.FlaskClient` for more information.
    test_client_class: t.Optional[t.Type["FlaskClient"]] = None

    #: The class that is used for the session interface.
    #: See :class:`~flask.sessions.SecureCookieSessionInterface` for more information.
    session_interface = SecureCookieSessionInterface()

    def __init__(
        self,
        import_name: str,
        static_url_path: t.Optional[str] = None,
        static_folder: t.Optional[t.Union[str, os.PathLike]] = "static",
        static_host: t.Optional[str] = None,
        host_matching: bool = False,
        subdomain_matching: bool = False,
        template_folder: t.Optional[str] = "templates",
        instance_path: t.Optional[str] = None,
        instance_relative_config: bool = False,
        root_path: t.Optional[str] = None,
    ):
        _PackageBoundObject.__init__(
            self,
            import_name,
            template_folder=template_folder,
            root_path=root_path,
        )

        if static_folder is not None:
            self.static_folder = static_folder  # type: ignore

        if static_url_path is not None:
            self.static_url_path = static_url_path

        if instance_path is None:
            instance_path = self.auto_find_instance_path()
        elif not os.path.isabs(instance_path):
            raise ValueError(
                "If an instance path is provided it must be an absolute path."
            )

        self.instance_path = instance_path

        self.config = self.make_config()
        self.config["ENV"] = get_env()
        self.config["DEBUG"] = get_debug_flag()
        self.config["SECRET_KEY"] = None
        self.config["SESSION_COOKIE_NAME"] = "session"
        self.config["SESSION_COOKIE_DOMAIN"] = None
        self.config["SESSION_COOKIE_PATH"] = None
        self.config["SESSION_COOKIE_HTTPONLY"] = True
        self.config["SESSION_COOKIE_SECURE"] = False
        self.config["SESSION_COOKIE_SAMESITE"] = None
        self.config["SESSION_REFRESH_EACH_REQUEST"] = True
        self.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=31)
        self.config["USE_X_SENDFILE"] = False
        self.config["SEND_FILE_MAX_AGE_DEFAULT"] = None
        self.config["PREFERRED_URL_SCHEME"] = "http"
        self.config["JSON_AS_ASCII"] = True
        self.config["JSON_SORT_KEYS"] = True
        self.config["JSONIFY_PRETTYPRINT_REGULAR"] = True
        self.config["JSONIFY_MIMETYPE"] = "application/json"
        self.config["TEMPLATES_AUTO_RELOAD"] = None
        self.config["EXPLAIN_TEMPLATE_LOADING"] = False
        self.config["MAX_CONTENT_LENGTH"] = None
        self.config["APPLICATION_ROOT"] = "/"
        self.config["SESSION_COOKIE_NAME"] = "session"

        self._view_functions: t.Dict[str, t.Callable] = {}
        self._error_handlers: t.Dict[
            t.Optional[t.Type[BaseException]], t.Callable
        ] = {}
        self._url_builders: t.Dict[str, t.Callable] = {}
        self._before_request_funcs: t.Dict[
            t.Optional[str], t.List[t.Callable]
        ] = {}
        self._before_first_request_funcs: t.List[t.Callable] = []
        self._after_request_funcs: t.Dict[
            t.Optional[str], t.List[t.Callable]
        ] = {}
        self._teardown_request_funcs: t.Dict[
            t.Optional[str], t.List[t.Callable]
        ] = {}
        self._teardown_appcontext_funcs: t.List[t.Callable] = []
        self._url_default_functions: t.Dict[
            t.Optional[str], t.List[t.Callable]
        ] = {}
        self._url_value_preprocessors: t.Dict[
            t.Optional[str], t.List[t.Callable]
        ] = {}
        self._blueprints: t.Dict[str, "Blueprint"] = {}
        self.extensions: t.Dict[str, t.Any] = {}
        self.url_map = self.url_map_class()
        self.url_map.host_matching = host_matching
        self.subdomain_matching = subdomain_matching
        self._got_first_request = False
        self._lock = Lock()

        # Apply the test client class.
        if self.test_client_class is not None:
            self.test_client_class = self.test_client_class

        # Register CLI commands.
        self.cli = cli.FlaskGroup()
        self.cli.add_command(cli.run_command)
        self.cli.add_command(cli.shell_command)
        self.cli.add_command(cli.routes_command)

    def _get_exc_class_and_code(self, exc_class: t.Type[BaseException]) -> t.Tuple[t.Type[BaseException], int]:
        """Get the exception class and code for a given exception class."""
        if issubclass(exc_class, HTTPException):
            return exc_class, exc_class.code  # type: ignore
        return exc_class, 500

    def make_config(self) -> Config:
        """Create the configuration object."""
        return self.config_class(self.root_path, self.default_config)

    @cached_property
    def auto_find_instance_path(self) -> str:
        """Finds the instance path."""
        prefix, package_path = find_package(self.import_name)

        if prefix is None:
            return os.path.abspath(package_path)

        return os.path.abspath(os.path.join(prefix, "var", self.name + "-instance"))

    @property
    def name(self) -> str:
        """The name of the application."""
        if self.import_name == "__main__":
            return getattr(sys.modules["__main__"], "__file__", self.import_name)
        return self.import_name

    @property
    def propagate_exceptions(self) -> bool:
        """Return the value of the ``PROPAGATE_EXCEPTIONS`` config value."""
        rv = self.config.get("PROPAGATE_EXCEPTIONS")
        if rv is not None:
            return rv
        return self.testing or self.debug

    @property
    def debug(self) -> bool:
        """Return the value of the ``DEBUG`` config value."""
        return self.config.get("DEBUG", False)

    @property
    def testing(self) -> bool:
        """Return the value of the ``TESTING`` config value."""
        return self.config.get("TESTING", False)

    @property
    def logger(self):
        """A :class:`logging.Logger` object for this application."""
        if self._logger is None:
            self._logger = create_logger(self)
        return self._logger

    @setupmethod
    def add_url_rule(
        self,
        rule: str,
        endpoint: t.Optional[str] = None,
        view_func: t.Optional[t.Callable] = None,
        provide_automatic_options: t.Optional[bool] = None,
        **options: t.Any,
    ) -> None:
        """Connects a URL rule."""
        if endpoint is None:
            endpoint = _endpoint_from_view_func(view_func)
        options["endpoint"] = endpoint
        methods = options.pop("methods", None)

        if methods is None:
            methods = getattr(view_func, "methods", None) or ("GET",)

        if isinstance(methods, str):
            raise TypeError(
                "Allowed methods must be a list or tuple. "
                f"Got {methods!r} instead."
            )

        methods = {item.upper() for item in methods}

        required_methods = set(getattr(view_func, "required_methods", ()))

        if provide_automatic_options is None:
            provide_automatic_options = getattr(
                view_func, "provide_automatic_options", None
            )

        if provide_automatic_options is None:
            if "OPTIONS" not in methods:
                provide_automatic_options = True
                required_methods.add("OPTIONS")
        else:
            provide_automatic_options = False

        methods.update(required_methods)

        rule_obj = Rule(rule, methods=methods, **options)
        rule_obj.provide_automatic_options = provide_automatic_options
        self.url_map.add(rule_obj)

        if view_func is not None:
            old_func = self._view_functions.get(endpoint)
            if old_func is not None and old_func != view_func:
                raise AssertionError(
                    "View function mapping is overwriting an existing"
                    f" endpoint function: {endpoint}")
            self._view_functions[endpoint] = view_func

    def route(self, rule: str, **options: t.Any) -> t.Callable:
        """A decorator that is used to register a view function for a
        given URL rule.
        """

        def decorator(f: t.Callable) -> t.Callable:
            endpoint = options.pop("endpoint", None)
            self.add_url_rule(rule, endpoint, f, **options)
            return f

        return decorator

    @setupmethod
    def endpoint(self, endpoint: str) -> t.Callable:
        """A decorator to register a function as an endpoint."""

        def decorator(f: t.Callable) -> t.Callable:
            self._view_functions[endpoint] = f
            return f

        return decorator

    @setupmethod
    def errorhandler(
        self, code_or_exception: t.Union[int, t.Type[Exception]]
    ) -> t.Callable:
        """Register a function to handle errors."""

        def decorator(f: t.Callable) -> t.Callable:
            self._register_error_handler(code_or_exception, f)
            return f

        return decorator

    def _register_error_handler(
        self,
        code_or_exception: t.Union[int, t.Type[Exception]],
        f: t.Callable,
    ) -> None:
        if isinstance(code_or_exception, int):
            code_or_exception = _make_abort_exception(code_or_exception)
        self._error_handlers[code_or_exception] = f

    def register_error_handler(
        self,
        code_or_exception: t.Union[int, t.Type[Exception]],
        f: t.Callable,
    ) -> None:
        """Alternative error attach function."""
        self._register_error_handler(code_or_exception, f)

    @setupmethod
    def before_request(self, f: t.Callable) -> t.Callable:
        """Register a function to run before each request."""
        self._before_request_funcs.setdefault(None, []).append(f)
        return f

    @setupmethod
    def before_first_request(self, f: t.Callable) -> t.Callable:
        """Register a function to run before the first request."""
        self._before_first_request_funcs.append(f)
        return f

    @setupmethod
    def after_request(self, f: t.Callable) -> t.Callable:
        """Register a function to run after each request."""
        self._after_request_funcs.setdefault(None, []).append(f)
        return f

    @setupmethod
    def teardown_request(self, f: t.Callable) -> t.Callable:
        """Register a function to run after each request."""
        self._teardown_request_funcs.setdefault(None, []).append(f)
        return f

    @setupmethod
    def teardown_appcontext(self, f: t.Callable) -> t.Callable:
        """Register a function to run after each request."""
        self._teardown_appcontext_funcs.append(f)
        return f

    @setupmethod
    def context_processor(self, f: t.Callable) -> t.Callable:
        """Register a template context processor function."""
        self._template_context_processors[None].append(f)
        return f

    @setupmethod
    def url_defaults(self, f: t.Callable) -> t.Callable:
        """Register a URL defaults processor."""
        self._url_default_functions.setdefault(None, []).append(f)
        return f

    @setupmethod
    def url_value_preprocessor(self, f: t.Callable) -> t.Callable:
        """Register a URL value preprocessor."""
        self._url_value_preprocessors.setdefault(None, []).append(f)
        return f

    def inject_url_defaults(self, endpoint: str, values: t.Dict) -> None:
        """Injects URL defaults."""
        for funcs in self._url_default_functions.values():
            for func in funcs:
                func(endpoint, values)

    def handle_url_build_error(
        self, error: BuildError, endpoint: str, values: t.Dict
    ) -> str:
        """Handle URL build errors."""
        for builder in self._url_builders.values():
            try:
                rv = builder(endpoint, values)
                if rv is not None:
                    return rv
            except Exception:
                pass
        raise error

    def preprocess_request(self) -> t.Optional[ResponseReturnValue]:
        """Preprocess the request."""
        for func in self._before_request_funcs.get(None, ()):  # type: ignore
            rv = func()
            if rv is not None:
                return rv
        return None

    def process_response(self, response: Response) -> Response:
        """Process the response."""
        for func in self._after_request_funcs.get(None, ()):  # type: ignore
            response = func(response)
        return response

    def do_teardown_request(
        self, exc: t.Optional[BaseException] = None
    ) -> None:
        """Teardown the request."""
        for func in self._teardown_request_funcs.get(None, ()):  # type: ignore
            func(exc)

    def do_teardown_appcontext(
        self, exc: t.Optional[BaseException] = None
    ) -> None:
        """Teardown the app context."""
        for func in self._teardown_appcontext_funcs:
            func(exc)

    def app_context(self) -> AppContext:
        """Create an app context."""
        return AppContext(self)

    def request_context(self, environ: t.Dict) -> RequestContext:
        """Create a request context."""
        return RequestContext(self, environ)

    def test_request_context(self, **kwargs: t.Any) -> RequestContext:
        """Create a test request context."""
        return self.request_context(self.test_client().environ_base(**kwargs))

    def wsgi_app(self, environ: t.Dict, start_response: t.Callable) -> t.Any:
        """The actual WSGI application."""
        ctx = self.request_context(environ)
        error = None
        try:
            try:
                ctx.push()
                response = self.full_dispatch_request()
            except Exception as e:
                error = e
                response = self.handle_exception(e)
            return response(environ, start_response)
        finally:
            if self.should_ignore_error(error):
                error = None
            ctx.auto_pop(error)

    def __call__(self, environ: t.Dict, start_response: t.Callable) -> t.Any:
        """The WSGI application."""
        return self.wsgi_app(environ, start_response)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name!r}>"
