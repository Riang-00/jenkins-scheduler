"""Build the per-user NSIS installer from a freshly built native executable."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import uuid
import zipfile

from package_candidate import build_candidate, sha256_file, SOURCE_ROOT

PYTHON_VERSION = '3.13.17'
PYTHON_SHA256 = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'
NSIS_SHA256 = 'c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1'


def download(name, url, digest=None):
    target = SOURCE_ROOT / '.staging/downloads' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with urllib.request.urlopen(url, timeout=60) as response:
            content = response.read()
        if digest and hashlib.sha256(content).hexdigest() != digest:
            raise ValueError('下载文件校验失败：' + name)
        target.write_bytes(content)
    if digest and sha256_file(target) != digest:
        raise ValueError('缓存文件校验失败：' + name)
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--revision')
    args = parser.parse_args()
    pyzip = download(f'python-{PYTHON_VERSION}-embed-amd64.zip',
                     f'https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip', PYTHON_SHA256)
    nsiszip = download('nsis-3.11.zip', 'https://prdownloads.sourceforge.net/nsis/nsis-3.11.zip?download', NSIS_SHA256)
    webview = download('MicrosoftEdgeWebview2Setup.exe', 'https://go.microsoft.com/fwlink/p/?LinkId=2124703')
    # The Evergreen bootstrapper changes over time; authenticate its publisher.
    signature = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command',
        'Import-Module "$PSHOME/Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1" -ErrorAction Stop; '
        "$s=Get-AuthenticodeSignature -LiteralPath '" + str(webview).replace("'", "''") +
        "'; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { exit 1 }"], check=True)
    del signature
    build = SOURCE_ROOT / '.staging' / ('release-' + uuid.uuid4().hex)
    runtime = build / 'runtime'
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(pyzip) as archive:
        archive.extractall(runtime)
    (runtime / 'python313._pth').write_text('python313.zip\n.\n..\n', encoding='ascii')
    compiler = nsiszip.parent / 'nsis-3.11/makensis.exe'
    if not compiler.exists():
        with zipfile.ZipFile(nsiszip) as archive:
            archive.extractall(nsiszip.parent)
    payload = build_candidate(SOURCE_ROOT, args.exe, build / 'payload', revision=args.revision, runtime=runtime)
    output = SOURCE_ROOT / 'artifacts/JenkinsScheduler-2.1.1-windows-x64-setup.exe'
    output.parent.mkdir(exist_ok=True)
    compiler_temp = build / 'compiler-temp'
    compiler_temp.mkdir()
    subprocess.run([str(compiler), '/V2', '/INPUTCHARSET', 'UTF8', '/DPAYLOAD=' + str(payload), '/DOUTPUT=' + str(output),
                    '/DWEBVIEW=' + str(webview), str(SOURCE_ROOT / 'tools/installer.nsi')], check=True,
                   env={**os.environ, 'TEMP': str(compiler_temp), 'TMP': str(compiler_temp)})
    checksum = output.with_name('SHA256SUMS.txt')
    checksum.write_text(sha256_file(output) + '  ' + output.name + '\n', encoding='ascii')
    result = {'installer': str(output), 'sha256': sha256_file(output), 'payload': str(payload),
              'python': PYTHON_VERSION, 'source_revision': args.revision}
    (SOURCE_ROOT / 'artifacts/build-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
