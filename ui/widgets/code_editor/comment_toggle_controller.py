from PySide6.QtGui import QTextCursor


class CommentToggleController:
    def __init__(self, editor):
        self.editor = editor

    def toggle_comment(self):
        """Ctrl+/ 注释切换：全选行均已注释则取消，否则注释。"""
        cursor = self.editor.textCursor()

        if cursor.hasSelection():
            start = cursor.selectionStart()
            end = max(cursor.selectionEnd() - 1, start)
        else:
            block = cursor.block()
            start = block.position()
            # block.length() 包含行尾换行符；单行切换不应把下一行误算进来
            end = block.position() + len(block.text())

        work = QTextCursor(self.editor.document())
        work.setPosition(start)
        work.movePosition(QTextCursor.StartOfLine)
        line_entries = []
        all_commented = True
        while work.position() <= end:
            block = work.block()
            text = block.text()
            stripped = text.lstrip()
            if stripped and not stripped.startswith("# "):
                all_commented = False
            line_entries.append((block.blockNumber(), text))
            if not work.movePosition(QTextCursor.Down):
                break

        self.editor.blockSignals(True)
        cursor.beginEditBlock()
        try:
            for block_num, text in reversed(line_entries):
                if not text.strip():
                    continue

                indent = len(text) - len(text.lstrip())
                content = text.lstrip()

                if all_commented:
                    if content.startswith("# "):
                        new_text = " " * indent + content[2:]
                    elif content.startswith("#"):
                        new_text = " " * indent + content[1:]
                    else:
                        continue
                else:
                    new_text = " " * indent + "# " + content

                block = self.editor.document().findBlockByNumber(block_num)
                work.setPosition(block.position())
                work.setPosition(block.position() + block.length() - 1, QTextCursor.KeepAnchor)
                work.insertText(new_text)
        finally:
            cursor.endEditBlock()
            self.editor.blockSignals(False)

        self.editor.contentMutated.emit()
