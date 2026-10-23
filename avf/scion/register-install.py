#!/usr/bin/env python3
"""Add the iOS 16.5 (AVF) install to Scion's install list (shared_prefs/installs.xml).

  register-install.py IN.xml OUT.xml VM_DIR

The list is JSON in <string name="list"> (Installs.kt, InstallMigration.encode). Every existing
entry is kept byte for byte as JSON; a new one is appended (id = next_id), next_id is bumped and
the selected install ("current") is left alone. If an entry already uses VM_DIR, nothing changes.
"""
import json, os, sys
import xml.etree.ElementTree as ET

src, dst, vm_dir = sys.argv[1:4]
tree = ET.parse(src)
root = tree.getroot()
by_name = {e.get('name'): e for e in root}
lst = by_name['list']
installs = json.loads(lst.text)
if any(i.get('internal_dir') == vm_dir for i in installs):
    print('   already registered:', [i['id'] for i in installs if i.get('internal_dir') == vm_dir])
    tree.write(dst, encoding='utf-8', xml_declaration=True)
    sys.exit(0)

first = installs[0]
next_el = by_name.get('next_id')
next_id = int(next_el.get('value')) if next_el is not None else 0
next_id = max(next_id, max(int(i['id']) for i in installs if i['id'].isdigit()) + 1)
iid = str(next_id)
settings = dict(first['settings'])          # same keys and value types as install 1's
settings.update({'backend': 'avf', 'security': 'bringup', 'ram_gb': 4, 'smp': 1, 'extra_args': ''})
shared_base = os.path.dirname(first['shared_dir'].rstrip('/'))
if os.path.basename(shared_base) != 'installs':
    shared_base = os.path.join(shared_base, 'installs')
installs.append({
    'id': iid, 'name': 'iOS 16.5 (AVF)',
    'internal_dir': vm_dir, 'shared_dir': os.path.join(shared_base, iid),
    'internal': True, 'legacy': False, 'settings': settings,
})
lst.text = json.dumps(installs, separators=(',', ':'))
if next_el is None:
    next_el = ET.SubElement(root, 'int', {'name': 'next_id'})
next_el.set('value', str(next_id + 1))
tree.write(dst, encoding='utf-8', xml_declaration=True)
print(f'   install {iid} "iOS 16.5 (AVF)" -> {vm_dir}; install 1 unchanged; next_id {next_id + 1}')
