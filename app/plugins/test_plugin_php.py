import sys
import os

sys.path.append(os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from php_to_node import PhpToNodePlugin

samples_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'samples')
sample_path = os.path.join(samples_dir, 'legacy_cart.php')

with open(sample_path, 'r') as f:
    content = f.read()

plugin = PhpToNodePlugin()

print(f"Plugin name: {plugin.name}")
print(f"Source type: {plugin.source_type}")
print(f"Target type: {plugin.target_type}")
print()
print("--- Converted Output ---")
print(plugin.convert(content))