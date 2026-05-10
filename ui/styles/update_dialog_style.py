UPDATE_DIALOG_QSS = """
QDialog {
    background-color: #f5f5f7;
}

QLabel {
    font-family: "Segoe UI", "Microsoft YaHei";
    color: #333333;
}

QLabel#DialogTitle {
    font-size: 18px;
    font-weight: 800;
    color: #1d1d1f;
}

QLabel#DialogHint {
    font-size: 12px;
    color: #6b6b6f;
}

QWidget#SummaryCard {
    background-color: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 10px;
}

QLabel#SummaryKey {
    font-size: 12px;
    color: #6b6b6f;
}

QLabel#SummaryValue {
    font-size: 13px;
    color: #1d1d1f;
    font-weight: 700;
}

QLabel#NotesTitle {
    font-size: 13px;
    font-weight: 700;
    color: #1d1d1f;
}

QPlainTextEdit#NotesBox {
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 10px;
    background-color: #ffffff;
    selection-background-color: #cce5ff;
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
