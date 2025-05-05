import os
import uuid
import jinja2
from lektor.environment.config import Config
from lektor.pluginsystem import PluginController
from lektor.utils import tojson_filter, format_lat_long
from lektor.context import (
    url_to, site_proxy, config_proxy, get_asset_url, get_locale, get_ctx
)
from lektor.markdown import Markdown
from lektor.packages import load_packages
from babel import dates
from jinja2.loaders import split_template_path

# Assume: project = some preloaded Project object
root_path = os.path.abspath(project.tree)

# Setup theme/template paths
theme_paths = [
    os.path.join(root_path, "themes", theme)
    for theme in project.themes
]

template_paths = [
    os.path.join(path, "templates")
    for path in [root_path] + theme_paths
]

# Initialize Jinja environment manually
jinja_env = jinja2.Environment(
    autoescape=lambda name: name and name.endswith((".html", ".xml")),
    extensions=["jinja2.ext.do"],
    loader=jinja2.FileSystemLoader(template_paths)
)

# Register filters
def latlongformat(latlong, secs=True):
    lat, lon = latlong
    return format_lat_long(lat=lat, long=lon, secs=secs)

jinja_env.filters.update({
    "tojson": tojson_filter,
    "latformat": lambda x, secs=True: format_lat_long(lat=x, secs=secs),
    "longformat": lambda x, secs=True: format_lat_long(long=x, secs=secs),
    "latlongformat": latlongformat,
    "url": url_to,
    "asseturl": get_asset_url,
    "markdown": lambda source, **kw: Markdown(source, get_ctx().source, field_options=kw),
    "dateformat": lambda arg, fmt="medium": dates.format_date(arg, fmt, locale=get_locale("en_US")),
    "datetimeformat": lambda arg, fmt="medium": dates.format_datetime(arg, fmt, locale=get_locale("en_US")),
    "timeformat": lambda arg, fmt="medium": dates.format_time(arg, fmt, locale=get_locale("en_US")),
})

# Register globals
jinja_env.globals.update({
    "url_to": url_to,
    "site": site_proxy,
    "config": config_proxy,
    "get_random_id": lambda: uuid.uuid4().hex,
})

# Load plugins manually
plugin_controller = PluginController(env=None)  # No facade to pass; must pass None or manage context yourself
load_packages(env=None)  # You’ll also need to mock env where required in some plugin cases
plugin_controller.emit("process-template-context", context={}, template=None)

# Manually load config
config = Config(project.project_file)

# Render a template manually
template = jinja_env.get_or_select_template("index.html")
rendered = template.render({
    "site": site_proxy,
    "config": config_proxy,
    "this": None,
    "alt": "en"
})
