import sys
sys.path.insert(0, 'e:/Python/release/source')
from TindaAgent.Process.Versioning.manager import VersionManager

# Test 1: Basic instantiation
vm = VersionManager()
print('Test 1: VersionManager instantiation OK')

# Test 2: get_current
current = vm.get_current()
ver = current.get('version')
print(f'Test 2: get_current OK - version: {ver}')

# Test 3: list_local_versions (empty)
local = vm.list_local_versions()
count = len(local)
print(f'Test 3: list_local_versions OK - count: {count}')

# Test 4: check_target_compat
compat = vm.check_target_compat('1.0.0')
ok_val = compat.get('ok')
print(f'Test 4: check_target_compat OK - ok: {ok_val}')

# Test 5: verify_manifest with empty keys
result = vm.verify_manifest({'app': 'TindaAgent', 'version': '1.0.0'}, b'fake')
print(f'Test 5: verify_manifest OK - ok: {result.ok}, error: {result.error}')

print('All basic tests passed!')
