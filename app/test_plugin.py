import sys
sys.path.append("app")
sys.path.append("app/plugins")

from plugins.java_to_python import JavaToPythonPlugin

plugin = JavaToPythonPlugin()
print("Plugin name:", plugin.name)
print("Source type:", plugin.source_type)
print("Target type:", plugin.target_type)

with open("samples/LegacyOrder.java", "r") as f:
    java_code = f.read()

result = plugin.convert(java_code)
print("\n--- Converted Output ---")
print(result)