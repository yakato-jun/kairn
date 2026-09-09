"""外部 CLI の実行。Windows でも PATH / PATHEXT からコマンドを解決する。"""
from __future__ import annotations

import os
import shutil
import subprocess


def command(argv, env=None):
    if os.name != "nt":
        return argv
    env = os.environ if env is None else env
    executable = shutil.which(argv[0], path=env.get("PATH", ""))
    return [executable or argv[0], *argv[1:]]


def run(argv, **kwargs):
    return subprocess.run(command(argv, kwargs.get("env")), **kwargs)


def popen(argv, **kwargs):
    return subprocess.Popen(command(argv, kwargs.get("env")), **kwargs)
