import sys
import os
import traceback

# 将项目根目录添加到 python path，确保可以正确导入模块
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

# --exec-script <path>: exe 内部脚本执行模式，跳过 GUI
if len(sys.argv) >= 3 and sys.argv[1] == "--exec-script":
    script_path = sys.argv[2]
    try:
        code = open(script_path, encoding="utf-8").read()
        exec(compile(code, script_path, "exec"))
    except SystemExit as e:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
    sys.exit(0)

from PySide6.QtWidgets import QApplication
from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
