"""Preserve the Windows task entry; interactive work uses Tauri."""
from pathlib import Path
import subprocess
import sys
import scheduler_v2

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--run':
        scheduler_v2.run(sys.argv[2])
    elif len(sys.argv) == 1:
        executable = Path(__file__).resolve().parent / 'bin' / 'JenkinsScheduler.exe'
        if not executable.is_file():
            raise SystemExit('请从桌面程序启动，或先按README构建隔离候选包。')
        subprocess.Popen([str(executable)], cwd=executable.parent.parent)
    else:
        raise SystemExit('Usage: app.py [--run RESERVATION_ID]')
