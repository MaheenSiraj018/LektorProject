from _future_ import annotations

import fnmatch
import os
import uuid
from functools import update_wrapper
from typing import TYPE_CHECKING

import babel.dates
import jinja2
from jinja2.loaders import split_template_path

from lektor.constants import PRIMARY_ALT
from lektor.context import config_proxy
from lektor.context import get_asset_url
from lektor.context import get_ctx
from lektor.context import get_locale
from lektor.context import site_proxy
from lektor.context import url_to
from lektor.environment.config import Config
from lektor.environment.config import DEFAULT_CONFIG  # noqa - reexport
from lektor.environment.config import ServerInfo  # noqa - reexport
from lektor.environment.config import update_config_from_ini  # noqa - reexport
from lektor.environment.expressions import Expression  # noqa - reexport
from lektor.environment.expressions import FormatExpression  # noqa - reexport
from lektor.markdown import Markdown
from lektor.packages import load_packages
from lektor.pluginsystem import initialize_plugins
from lektor.pluginsystem import PluginController
from lektor.publisher import builtin_publishers
from lektor.utils import format_lat_long
from lektor.utils import tojson_filter

if TYPE_CHECKING:
    from typing import Literal
    from lektor.assets import Asset
    from lektor.build_programs import BuildProgram
    from lektor.sourceobj import SourceObject


def _prevent_inlining(wrapped):
    @jinja2.pass_context
    def wrapper(_jinja_ctx, *args, **kwargs):
        return wrapped(*args, **kwargs)

    return update_wrapper(wrapper, wrapped)


def _dates_filter(name, wrapped):
    @_prevent_inlining
    def wrapper(arg, format="medium", **kwargs):
        if isinstance(arg, jinja2.Undefined):
            return str(arg)

        if not isinstance(format, str):
            raise TypeError(
                f"The 'format' parameter to '{name}' should be a str, not {format!r}"
            )

        locale = kwargs.get("locale")
        if locale is None:
            kwargs["locale"] = get_locale("en_US")

        try:
            return wrapped(arg, format, **kwargs)
        except (TypeError, ValueError):
            raise
        except Exception as exc:
            raise TypeError(
                f"While evaluating filter '{name}', an unexpected exception was raised. "
                "This is likely caused by an input or parameter of an unsupported type."
            ) from exc

    return update_wrapper(wrapper, wrapped)


@_prevent_inlining
def _markdown_filter(
    source: str,
    *,
    resolve_links: Literal["always", "never", "when-possible", None] = None,
    **kw: str,
) -> Markdown:
    ctx = get_ctx()
    source_obj = ctx.source if ctx is not None else None
    return Markdown(
        source, source_obj, field_options={**kw, "resolve_links": resolve_links}
    )


IGNORED_FILES = ["thumbs.db", "desktop.ini", "Icon\\r"]
SPECIAL_ARTIFACTS = [".htaccess", ".htpasswd"]
EXCLUDED_ASSETS = ["_", "."]
INCLUDED_ASSETS = []


def any_fnmatch(filename, patterns):
    for pat in patterns:
        if fnmatch.fnmatch(filename, pat):
            return True
    return False


class CustomJinjaEnvironment(jinja2.Environment):
    def _load_template(self, name, globals):
        ctx = get_ctx()

        try:
            rv = jinja2.Environment._load_template(self, name, globals)
            if ctx is not None:
                filename = rv.filename
                ctx.record_dependency(filename)
            return rv
        except jinja2.TemplateSyntaxError as e:
            if ctx is not None:
                ctx.record_dependency(e.filename)
            raise
        except jinja2.TemplateNotFound as e:
            if ctx is not None:
                for template_name in e.templates:
                    pieces = split_template_path(template_name)
                    for base in self.loader.searchpath:
                        ctx.record_dependency(os.path.join(base, *pieces))
            raise


@jinja2.pass_context
def lookup_from_bag(jinja_ctx, *args):
    pieces = ".".join(x for x in args if x)
    site = jinja_ctx.get("site", default=site_proxy)
    return site.databags.lookup(pieces)


class Environment:
    def _init_(self, project, load_plugins=True, extra_flags=None):
        self.project = project
        self.root_path = os.path.abspath(project.tree)

        self.theme_paths = [
            os.path.join(self.root_path, "themes", theme)
            for theme in self.project.themes
        ]

        if not self.theme_paths:
            try:
                for fname in os.listdir(os.path.join(self.root_path, "themes")):
                    f = os.path.join(self.root_path, "themes", fname)
                    if os.path.isdir(f):
                        self.theme_paths.append(f)
            except OSError:
                pass

        template_paths = [
            os.path.join(path, "templates")
            for path in [self.root_path] + self.theme_paths
        ]

        self.jinja_env = CustomJinjaEnvironment(
            autoescape=self.select_jinja_autoescape,
            extensions=["jinja2.ext.do"],
            loader=jinja2.FileSystemLoader(template_paths),
        )

        from lektor.db import F, get_alts

        def latlongformat(latlong, secs=True):
            lat, lon = latlong
            return format_lat_long(lat=lat, long=lon, secs=secs)

        self.jinja_env.filters.update(
            tojson=tojson_filter,
            latformat=lambda x, secs=True: format_lat_long(lat=x, secs=secs),
            longformat=lambda x, secs=True: format_lat_long(long=x, secs=secs),
            latlongformat=latlongformat,
            url=_prevent_inlining(url_to),
            asseturl=_prevent_inlining(get_asset_url),
            markdown=_markdown_filter,
        )
        self.jinja_env.globals.update(
            F=F,
            url_to=url_to,
            site=site_proxy,
            config=config_proxy,
            bag=lookup_from_bag,
            get_alts=get_alts,
            get_random_id=lambda: uuid.uuid4().hex,
        )
        self.jinja_env.filters.update(
            dateformat=_dates_filter("dateformat", babel.dates.format_date),
            datetimeformat=_dates_filter("datetimeformat", babel.dates.format_datetime),
            timeformat=_dates_filter("timeformat", babel.dates.format_time),
        )

        from lektor.types import builtin_types

        self.types = builtin_types.copy()
        self.publishers = builtin_publishers.copy()
        self.plugin_controller = PluginController(self, extra_flags)
        self.plugins = {}
        self.plugin_ids_by_class = {}
        self.build_programs = []
        self.special_file_assets = {}
        self.special_file_suffixes = {}
        self.custom_url_resolvers = []
        self.custom_generators = []
        self.virtual_sources = {}

        if load_plugins:
            self.load_plugins()

        from lektor.db import siblings_resolver
        self.virtualpathresolver("siblings")(siblings_resolver)

    @property
    def asset_path(self):
        return os.path.join(self.root_path, "assets")

    @property
    def temp_path(self):
        return os.path.join(self.root_path, "temp")

    def load_plugins(self):
        load_packages(self)
        initialize_plugins(self)

    def load_config(self):
        return Config(self.project.project_file)

    def new_pad(self):
        from lektor.db import Database
        return Database(self).new_pad()

    def is_uninteresting_source_name(self, filename: str) -> bool:
        fn = filename.lower()
        if fn in SPECIAL_ARTIFACTS:
            return False

        proj = self.project
        if any_fnmatch(filename, INCLUDED_ASSETS + proj.included_assets):
            return False
        return any_fnmatch(filename, EXCLUDED_ASSETS + proj.excluded_assets)

    @staticmethod
    def is_ignored_artifact(asset_name):
        fn = asset_name.lower()
        if fn in SPECIAL_ARTIFACTS:
            return False
        return fn[:1] == "." or fn in IGNORED_FILES

    def render_template(self, name, pad=None, this=None, values=None, alt=None):
        ctx = self.make_default_tmpl_values(pad, this, values, alt, template=name)
        return self.jinja_env.get_or_select_template(name).render(ctx)

    def make_default_tmpl_values(
        self, pad=None, this=None, values=None, alt=None, template=None
    ):
        values = dict(values or ())

        if alt is None:
            if this is not None:
                alt = getattr(this, "alt", None)
                if not isinstance(alt, str):
                    alt = None
            if alt is None:
                alt = PRIMARY_ALT

        if pad is None:
            ctx = get_ctx()
            if ctx is not None:
                pad = ctx.pad
        if pad is not None:
            values["site"] = pad
        if this is not None:
            values["this"] = this
        if alt is not None:
            values["alt"] = alt
        self.plugin_controller.emit(
            "process-template-context", context=values, template=template
        )
        return values

    @staticmethod
    def select_jinja_autoescape(filename):
        if filename is None:
            return False
        return filename.endswith((".html", ".htm", ".xml", ".xhtml"))

    def resolve_custom_url_path(self, obj, url_path):
        for resolver in self.custom_url_resolvers:
            rv = resolver(obj, url_path)
            if rv is not None:
                return rv
        return None

    def add_build_program(
        self, cls: type[SourceObject], program: type[BuildProgram]
    ) -> None:
        self.build_programs.append((cls, program))

    def add_asset_type(
        self, asset_cls: type[Asset], build_program: type[BuildProgram]
    ) -> None:
        self.build_programs.append((asset_cls, build_program))
        self.special_file_assets[asset_cls.source_extension] = asset_cls
        if asset_cls.artifact_extension:
            cext = asset_cls.source_extension + asset_cls.artifact_extension
            self.special_file_suffixes[cext] = asset_cls.source_extension

    def add_publisher(self, scheme, publisher):
        if scheme in self.publishers:
            raise RuntimeError('Scheme "%s" is already registered.' % scheme)
        self.publishers[scheme] = publisher

    def add_type(self, type):
        name = type.name
        if name in self.types:
            raise RuntimeError('Type "%s" is already registered.' % name)
        self.types[name] = type

    def virtualpathresolver(self, prefix):
        def decorator(func):
            if prefix in self.virtual_sources:
                raise RuntimeError('Prefix "%s" is already registered.' % prefix)
            self.virtual_sources[prefix] = func
            return func
        return decorator

    def urlresolver(self, func):
        self.custom_url_resolvers.append(func)
        return func

    def generator(self, func):
        self.custom_generators.append(func)
        return func

    def describe_project_structure(self) -> str:
        lines = []
        lines.append(f"Root Path: {self.root_path}")
        lines.append(f"Assets Path: {self.asset_path}")
        lines.append(f"Temp Path: {self.temp_path}")
        lines.append("Theme Paths:")
        for path in self.theme_paths:
            lines.append(f"  - {path}")
        lines.append("Template Search Paths:")
        for path in self.jinja_env.loader.searchpath:
            lines.append(f"  - {path}")
        return "\\n".join(lines)
