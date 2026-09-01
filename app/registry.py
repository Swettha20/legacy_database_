import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), 'plugins'))

from plugins.java_to_python import JavaToPythonPlugin
from plugins.php_to_node import PhpToNodePlugin


class PluginRegistry:
    def __init__(self):
        # Every plugin gets registered here - this is the ONLY place
        # that needs updating when a new plugin is added.
        self._plugins = [
            JavaToPythonPlugin(),
            PhpToNodePlugin(),
        ]

    def get_plugins_for(self, source_type: str):
        """Returns all plugins that can handle the given source type."""
        return [p for p in self._plugins if p.source_type == source_type]

    def all_plugins(self):
        return self._plugins