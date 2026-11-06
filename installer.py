"""Per-user program installation. User data is never copied or rolled back."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile

import configuration as c
import engine as e
from runtime_layout import data_directory
import scheduler_v2 as s


def program_files(root):
    manifest = c.read_json(root / 'release-manifest.json')
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        raise ValueError('安装清单缺失，请使用完整安装包。')
    names = list(manifest['files'])
    for name in names:
        p = PurePosixPath(name)
        if (not isinstance(name, str) or '\\' in name or ':' in name or p.is_absolute()
                or '..' in p.parts or str(p) != name
                or p.parts[0] in {'data', '.git', 'webview2'}
                or name in {'maintenance.json', 'install-location.json', 'desktop.lock'}
                or not (root / name).resolve().is_relative_to(root.resolve())):
            raise ValueError('安装清单包含无效程序路径。')
    return names


def guard_reservations(data):
    for record in s.records(data):
        status = record['status']
        if status == 'pending':
            when = datetime.fromisoformat(record['scheduled_at'])
            if e.now() > when + timedelta(seconds=e.GRACE):
                continue
        elif status not in {'running', 'invalid'}:
            continue
        raise ValueError('有待执行、执行中或损坏的预约，请先在程序中取消或核对后再安装/卸载。')


def apply(payload, target, data, *, uninstall=False):
    payload, target, data = (Path(p).resolve() for p in (payload, target, data))
    if (target == Path(target.anchor) or target == payload or target.is_relative_to(payload)
            or payload.is_relative_to(target) or data.is_relative_to(target) or target.is_relative_to(data)):
        raise ValueError('程序和数据目录无效。')
    existing = (target / 'release-manifest.json').exists()
    if existing:
        if not (target / 'install-location.json').exists() or data_directory(target) != data:
            raise ValueError('必须保留原安装的数据位置。')
    elif target.exists() and any(p.is_file() and p != target / 'desktop.lock' for p in target.rglob('*')):
        raise ValueError('目标目录不是本程序的安装目录，请选择空目录。')
    if uninstall and not existing:
        raise ValueError('未找到有效安装，未删除任何文件。')
    if not uninstall:
        s.runtime_ready(payload)
        new_names = program_files(payload)
        if not (payload / 'runtime/python.exe').is_file():
            raise ValueError('安装包缺少内置 Python。')
    else:
        new_names = []
    old_names = program_files(target) if existing else []
    target.mkdir(parents=True, exist_ok=True)
    # Native host opens desktop.lock with exclusive Windows sharing. If it is
    # running, even opening this lock fails before any program files change.
    with c.file_lock(target / 'desktop.lock'), c.file_lock(data / 'deployment.lock'), c.planning_lock(data):
        guard_reservations(data)
        names = sorted(set(old_names + new_names)) + ['install-location.json', 'release-manifest.json']
        # Reject junctions/symlinks before either installing or removing a file.
        for name in names:
            path = target / name
            if not path.resolve().is_relative_to(target) or path.is_symlink():
                raise ValueError('程序路径存在链接，无法安全安装。')
        c.atomic_json(target / 'maintenance.json', {'operation': 'uninstall' if uninstall else 'install'})
        backup = Path(tempfile.mkdtemp(prefix='jenkins-program-backup-', dir=target.parent))
        completed = False
        try:
            present = []
            try:
                for name in names:
                    path = target / name
                    if path.is_file():
                        saved = backup / name
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, saved)
                        present.append(name)
            except BaseException:
                (target / 'maintenance.json').unlink(missing_ok=True)
                completed = True
                raise
            try:
                for name in names:
                    destination = target / name
                    if name in new_names:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        temporary = destination.with_name(destination.name + '.installing')
                        try:
                            shutil.copyfile(payload / name, temporary)
                            os.replace(temporary, destination)
                        finally:
                            temporary.unlink(missing_ok=True)
                    elif name not in {'install-location.json', 'release-manifest.json'}:
                        destination.unlink(missing_ok=True)
                if uninstall:
                    (target / 'release-manifest.json').unlink()
                    (target / 'install-location.json').unlink()
                    # Delete only bytecode derived from the removed source files.
                    # Leaving it behind would prevent a later clean reinstall.
                    for name in old_names:
                        source = target / name
                        if source.suffix == '.py':
                            cache = source.parent / '__pycache__'
                            for cached in cache.glob(source.stem + '.cpython-*.pyc'):
                                if cached.resolve().is_relative_to(target) and not cached.is_symlink():
                                    cached.unlink()
                            if cache.is_dir() and not any(cache.iterdir()):
                                cache.rmdir()
                else:
                    c.atomic_json(target / 'install-location.json', {'schema_version': 1, 'data_directory': str(data)})
                    c.atomic_json(target / 'release-manifest.json', c.read_json(payload / 'release-manifest.json'))
                (target / 'maintenance.json').unlink()
                completed = True
            except BaseException:
                # Never restore settings, tokens, or reservation consumption state.
                for name in names:
                    destination = target / name
                    if name in present:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup / name, destination)
                    elif destination.is_file():
                        destination.unlink()
                (target / 'maintenance.json').unlink(missing_ok=True)
                completed = True
                raise
        finally:
            # If rollback itself fails, leave both the maintenance marker and
            # program backup for recovery. A complete reinstall can repair it.
            if completed:
                shutil.rmtree(backup)
    return {'version': s.VERSION, 'target': str(target), 'data': str(data), 'uninstalled': uninstall}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['install', 'uninstall'])
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--data', type=Path)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    try:
        target = args.target.resolve()
        data = args.data or data_directory(target)
        result = apply(Path(__file__).resolve().parent, target, data, uninstall=args.operation == 'uninstall')
        args.result.write_text('OK', encoding='utf-16')
        print(json.dumps(result, ensure_ascii=True))
    except Exception as error:
        message = str(error) if isinstance(error, (ValueError, c.ConfigError)) else '请关闭预约程序和相关进程后重试；检查目录权限和磁盘空间。'
        args.result.write_text(message, encoding='utf-16')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
