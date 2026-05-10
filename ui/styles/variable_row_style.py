VARIABLE_ROW_QSS = """
QFrame#VariableRow {
    border: 1px solid #dcdcdc;
    border-radius: 6px;
    background-color: white;
}

QFrame#VariableRow:hover {
    border: 1px solid #b0b0b0;
}

QWidget#HeaderWidget {
    background-color: #f7f7f9;
}

QLabel#VarTitle {
    font-weight: 800;
    color: #1d1d1f;
}

QLabel#VarSummary {
    color: #6b6b6f;
    font-size: 12px;
}

QToolButton {
    border: none;
    background-color: transparent;
    padding: 0px;
}

QToolButton:hover {
    background-color: rgba(0, 0, 0, 0.04);
    border-radius: 10px;
}

QLabel {
    color: #333333;
    font-family: "Segoe UI", "Microsoft YaHei";
}

QLineEdit {
    border: 1px solid #e0e0e0;
    border-radius: 3px;
    padding: 3px 5px;
    background-color: #fcfcfc;
}

QLineEdit:focus {
    border: 1px solid #007AFF;
    background-color: white;
}

QLineEdit[codePreview="true"] {
    background-color: #f3f4f6;
}

QPlainTextEdit {
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 6px;
    background-color: #fcfcfc;
    selection-background-color: #cce5ff;
}

QPlainTextEdit:focus {
    border: 1px solid #007AFF;
    background-color: white;
}

QComboBox {
    border: 1px solid #e0e0e0;
    border-radius: 3px;
    padding: 3px 5px;
    background-color: #fcfcfc;
    min-width: 60px;
}

QComboBox:focus {
    border: 1px solid #007AFF;
}

QComboBox::drop-down {
    border: none;
    width: 20px;
}

QPushButton#DeleteBtn {
    color: #999999;
    background-color: transparent;
    border: none;
    font-size: 18px;
    font-weight: bold;
    padding-bottom: 3px;
}

QPushButton#DeleteBtn:hover {
    color: #ff4d4f;
    background-color: rgba(255, 77, 79, 0.1);
    border-radius: 12px;
}
"""
