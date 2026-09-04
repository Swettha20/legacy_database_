import sys
sys.path.append("app")

from registry import PluginRegistry

registry = PluginRegistry()

print("Plugins for 'java':")
for p in registry.get_plugins_for("java"):
    print(" -", p.name)

print("\nPlugins for 'php':")
for p in registry.get_plugins_for("php"):
    print(" -", p.name)

print("\nPlugins for 'sql' (should be EMPTY - no SQL plugin registered yet):")
for p in registry.get_plugins_for("sql"):
    print(" -", p.name)