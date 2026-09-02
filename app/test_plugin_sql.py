import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), 'plugins'))
sys.path.append(os.path.dirname(__file__))

from sql_to_postgres import SqlToPostgresPlugin

samples_dir = os.path.join(os.path.dirname(__file__), '..', 'samples')
sample_path = os.path.join(samples_dir, 'schema.sql')

with open(sample_path, 'r') as f:
    content = f.read()

plugin = SqlToPostgresPlugin()

print(f"Plugin name: {plugin.name}")
print(f"Source type: {plugin.source_type}")
print(f"Target type: {plugin.target_type}")
print()
print("--- Converted Output ---")
print(plugin.convert(content))