#!/usr/bin/env python3
"""Builds Kodi-compatible addon zips into _site/."""
import os
import zipfile
import xml.etree.ElementTree as ET


def make_zip(addon_dir, out_path, skip_exts={'.pyc'}):
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as z:
        # Directory entry must come first — Kodi rejects zips without it
        z.mkdir(addon_dir)
        for root, dirs, files in os.walk(addon_dir):
            dirs[:] = [d for d in sorted(dirs) if d != '__pycache__']
            for name in sorted(files):
                if os.path.splitext(name)[1] in skip_exts:
                    continue
                z.write(os.path.join(root, name))


svc_ver  = ET.parse('service.scrob/addon.xml').getroot().get('version')
repo_ver = ET.parse('repository.scrob/addon.xml').getroot().get('version')

os.makedirs('_site/service.scrob',    exist_ok=True)
os.makedirs('_site/repository.scrob', exist_ok=True)

make_zip('service.scrob',    f'_site/service.scrob/service.scrob-{svc_ver}.zip')
make_zip('repository.scrob', f'_site/repository.scrob/repository.scrob-{repo_ver}.zip')

print(f'service.scrob-{svc_ver}.zip')
print(f'repository.scrob-{repo_ver}.zip')
