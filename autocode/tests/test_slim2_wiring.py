"""精简版的接线守卫：并入 auth 之后，config 提供的属性必须齐全。

背景：`auth.py` 原本属于 slim v1，并入 slim2 时它引用的 `config.new_password`
在 slim2/config.py 里不存在。Windows 上因为凭证已存在、从不走生成分支，
所以自测和浏览器冒烟都发现不了；只有在全新服务器上首次启动才炸。
这个测试用来保证同样的错误不会再溜过去。
"""

import re
import unittest
from pathlib import Path

from autocode.slim2 import config


class TestConfigWiring(unittest.TestCase):
    def test_auth_needs_are_satisfied(self) -> None:
        source = Path("autocode/slim2/auth.py").read_text(encoding="utf-8")
        used = sorted(set(re.findall(r"config\.([A-Za-z_]+)", source)))
        missing = [name for name in used if not hasattr(config, name)]
        self.assertEqual(missing, [], f"auth.py 用到但 config 没有：{missing}")

    def test_new_password_shape(self) -> None:
        value = config.new_password()
        self.assertEqual(len(value), 16)
        # 易混淆字符必须排除，否则手抄到手机容易错
        for bad in "0O1lI":
            self.assertNotIn(bad, value)
        self.assertNotEqual(value, config.new_password())

    def test_credentials_path_is_under_data(self) -> None:
        self.assertEqual(config.credentials_path().name, "slim2-credentials.json")
        self.assertEqual(config.initial_password_path().name, "slim2-initial-password.txt")


if __name__ == "__main__":
    unittest.main()
