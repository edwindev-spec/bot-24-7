"""Restricted plugin registry for the vendored IronBrew1-only CLI."""
from obfuscators.ironbrew1 import Ironbrew1

PLUGINS = [Ironbrew1()]


def by_name(name):
    normalized = (name or "").strip().lower()
    for plugin in PLUGINS:
        if plugin.name.lower() == normalized:
            return plugin
    raise KeyError("unknown obfuscator: %s" % (name or ""))


def detect(source):
    plugin = PLUGINS[0]
    return plugin, plugin.detect(source)
