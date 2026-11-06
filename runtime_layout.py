"""An installer-pinned data path shared by the desktop and scheduled workers."""
import json
from pathlib import Path


def data_directory(root):
    root = Path(root).resolve()
    marker = root / 'install-location.json'
    if not marker.exists():
        return root / 'data'
    value = json.loads(marker.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or not isinstance(value.get('data_directory'), str)
            or not Path(value['data_directory']).is_absolute()):
        raise ValueError('安装位置文件无效，请重新安装到原目录。')
    return Path(value['data_directory']).resolve()
