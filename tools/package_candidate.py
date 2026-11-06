"""Assemble a new isolated candidate from explicit source and native allowlists."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import uuid

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))
import configuration as c
import scheduler_v2 as scheduler

SOURCE_FILES = tuple(sorted((scheduler.REQUIRED_FILES - {'bin/JenkinsScheduler.exe'}) | {
    'README.md', '启动预约部署.cmd', 'docs/RELEASE.md', 'desktop/UPSTREAM.md',
    'installer.py', 'docs/THIRD_PARTY_NOTICES.txt',
}))


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_path(source, output):
    destination = (Path(output) if output is not None else source / '.staging' / ('candidate-' + uuid.uuid4().hex)).resolve()
    allowed = False
    for name in ('.staging', 'artifacts'):
        parent = (source / name).resolve()
        if parent.is_relative_to(source) and destination != parent and destination.is_relative_to(parent):
            allowed = True
    if not allowed:
        raise ValueError('候选输出必须是源码 .staging/ 或 artifacts/ 下的新目录，不能是实际安装目录。')
    if destination.exists():
        raise FileExistsError('候选输出目录已存在，不会覆盖。')
    return destination


def build_candidate(source, executable=None, output=None, *, revision=None, runtime=None):
    source = Path(source).resolve()
    if revision is not None and not re.fullmatch(r'[0-9a-f]{7,64}', revision):
        raise ValueError('revision 必须是已核对的十六进制源码版本，或省略并记录工作文件快照。')
    executable = Path(executable or source / 'desktop/src-tauri/target/release/tauri-native.exe').resolve()
    if not executable.is_file() or executable.suffix.lower() != '.exe':
        raise FileNotFoundError('没有可用的新构建 EXE；不能使用实际安装目录的旧程序。')
    with executable.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('构建输出不是 Windows EXE。')
    destination = _candidate_path(source, output)
    inputs = {name: source / name for name in SOURCE_FILES}
    for path in inputs.values():
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source):
            raise FileNotFoundError('源码白名单文件缺失或越界：' + str(path.relative_to(source)))
    inputs['bin/JenkinsScheduler.exe'] = executable
    if runtime is not None:
        runtime = Path(runtime).resolve()
        if not (runtime / 'python.exe').is_file() or not (runtime / 'python313._pth').is_file():
            raise ValueError('内置 Python 文件不完整。')
        for path in runtime.iterdir():
            if path.is_file() and not path.is_symlink():
                inputs['runtime/' + path.name] = path
    before = {name: sha256_file(path) for name, path in inputs.items()}
    destination.mkdir(parents=True, exist_ok=False)
    # A failed copy intentionally leaves an unsealed directory without a manifest;
    # it can never pass runtime_ready, and no pre-existing directory is removed.
    for name, path in inputs.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if sha256_file(target) != before[name]:
            raise ValueError('打包期间文件变化，候选没有封口；请使用新输出目录重试。')
    if any(sha256_file(path) != before[name] for name, path in inputs.items()):
        raise ValueError('打包期间源码变化，候选没有封口；请完成源码修改后重试。')
    tree_hash = hashlib.sha256(json.dumps(before, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    manifest = {'protocol': scheduler.PROTOCOL, 'version': scheduler.VERSION,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'source_revision': revision,
                'source_state': 'revision-supplied' if revision else 'working-tree-snapshot',
                'source_tree_sha256': tree_hash, 'files': before}
    c.atomic_json(destination / 'release-manifest.json', manifest)
    scheduler.runtime_ready(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description='创建新的隔离候选目录；不会安装或读取真实运行数据。')
    parser.add_argument('--exe', type=Path, help='新构建 EXE 的绝对路径')
    parser.add_argument('--output', type=Path, help='源码 .staging/ 或 artifacts/ 下不存在的输出目录')
    parser.add_argument('--revision', help='调用者已核对的源码 SHA；脚本不执行 Git 命令')
    options = parser.parse_args()
    if options.exe is not None and not options.exe.is_absolute():
        parser.error('--exe 必须是绝对路径。')
    try:
        candidate = build_candidate(SOURCE_ROOT, options.exe, options.output, revision=options.revision)
    except (OSError, ValueError, c.ConfigError) as error:
        parser.exit(1, '候选创建失败：' + str(error) + '\n')
    print(json.dumps({'candidate': str(candidate), 'protocol': scheduler.PROTOCOL, 'version': scheduler.VERSION,
                      'manifest_sha256': sha256_file(candidate / 'release-manifest.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
