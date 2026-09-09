import os
import sys

import pytest

from kairn import process
from kairn.extract import adapters


def test_utf8_output_and_windows_environment():
    env = adapters.get("claude").env(dict(os.environ))
    if os.name == "nt":
        assert env["SYSTEMROOT"] == os.environ["SYSTEMROOT"]
        assert env["USERPROFILE"] == os.environ["USERPROFILE"]
    result = process.run([sys.executable, "-c", "import sys; sys.stdout.buffer.write('日本語'.encode('utf-8'))"],
                         env=env, capture_output=True, text=True, encoding="utf-8", check=True)
    assert result.stdout == "日本語"


@pytest.mark.skipif(os.name != "nt", reason="Windows PATHEXT resolution")
def test_windows_batch_command_with_spaces(tmp_path):
    folder = tmp_path / "test tools"
    folder.mkdir()
    batch = folder / "kairn-test.cmd"
    batch.write_text('@echo %1\n', encoding="utf-8")
    env = dict(os.environ, PATH=str(folder))
    result = process.run(["kairn-test", "hello"], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "hello"
