import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from plugin_base import ModernizerPlugin


class PhpToNodePlugin(ModernizerPlugin):

    @property
    def source_type(self) -> str:
        return "php"

    @property
    def target_type(self) -> str:
        return "node"

    @property
    def name(self) -> str:
        return "PHP → Node.js (stub)"

    def convert(self, content: str) -> str:
        return f"// TODO: AI conversion not wired yet\n/* Original PHP code:\n{content}\n*/"