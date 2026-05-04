#!/usr/bin/env python3
"""Regenerates addons.xml and addons.xml.md5 from the addon.xml files in each add-on directory."""
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

ADDON_DIRS = ['service.scrob', 'repository.scrob']


def build():
    root = ET.Element('addons')
    for d in ADDON_DIRS:
        xml_path = Path(d) / 'addon.xml'
        if not xml_path.exists():
            print(f'  WARNING: {xml_path} not found, skipping')
            continue
        addon_el = ET.parse(xml_path).getroot()
        root.append(addon_el)
        print(f'  + {addon_el.get("id")} {addon_el.get("version")}')

    try:
        ET.indent(root, space='  ')
    except AttributeError:
        pass  # Python < 3.9

    content = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode')

    Path('addons.xml').write_text(content, encoding='utf-8')
    md5 = hashlib.md5(content.encode('utf-8')).hexdigest()
    Path('addons.xml.md5').write_text(md5)

    print(f'addons.xml written ({len(ADDON_DIRS)} add-ons)')
    print(f'addons.xml.md5: {md5}')


if __name__ == '__main__':
    build()
