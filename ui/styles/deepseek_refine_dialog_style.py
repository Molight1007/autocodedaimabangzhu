DEEPSEEK_REFINE_DIALOG_QSS = """
QDialog {
    background-color: #f5f5f7;
}

QLabel#DialogTitle {
    font-size: 16px;
    font-weight: 800;
    color: #1d1d1f;
}

QLabel#DialogHint {
    font-size: 12px;
    color: #6b6b6f;
}

QWidget#Toolbar {
    background-color: white;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
}

QLabel#ModelBadge {
    background-color: #f3f4f6;
    border: 1px solid #e5e5ea;
    border-radius: 6px;
    padding: 6px 10px;
    color: #333333;
    font-weight: 700;
}

QLabel#StatusBadge {
    background-color: #f3f4f6;
    border: 1px solid #e5e5ea;
    border-radius: 6px;
    padding: 6px 10px;
    color: #6b6b6f;
    font-weight: 700;
}

QComboBox {
    border: 1px solid #d1d1d6;
    border-radius: 6px;
    padding: 4px 10px;
    background-color: #ffffff;
    min-height: 28px;
}

QLabel#PaneTitle {
    color: #1d1d1f;
    font-weight: 800;
}

QWidget#PaneCard {
    background-color: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 10px;
}

QPlainTextEdit {
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 10px;
    background-color: #ffffff;
    selection-background-color: #cce5ff;
}

QPlainTextEdit:focus {
    border: 1px solid #007AFF;
}

QPushButton {
    background-color: #ffffff;
    color: #333333;
    border: 1px solid #d1d1d6;
    border-radius: 6px;
    font-weight: 700;
    padding: 6px 12px;
    min-height: 28px;
}

QPushButton:hover {
    border-color: #007AFF;
    color: #007AFF;
}

QPushButton#PrimaryBtn {
    background-color: #007AFF;
    color: white;
    border: none;
    font-weight: 800;
}

QPushButton#PrimaryBtn:hover {
    background-color: #0062cc;
    color: white;
}

QDialogButtonBox QPushButton {
    min-width: 92px;
}
"""
