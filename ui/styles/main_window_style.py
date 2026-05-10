MAIN_WINDOW_QSS = """
QMainWindow {
    background-color: #f5f5f7;
}

QLabel {
    font-family: "Segoe UI", "Microsoft YaHei";
    color: #333333;
}

QLabel#HeaderTitle {
    font-size: 22px;
    font-weight: 700;
    color: #1d1d1f;
}

QLabel#HeaderHint {
    font-size: 12px;
    color: #6b6b6f;
}

QGroupBox {
    background-color: white;
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    margin-top: 0px;
    font-weight: bold;
    font-size: 14px;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 10px;
    background-color: white;
    color: #333333;
}

QLineEdit {
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 6px;
    background-color: #fcfcfc;
}

QLineEdit:focus {
    border: 1px solid #007AFF;
    background-color: white;
}

QComboBox {
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 4px;
    background-color: #fcfcfc;
}

QCheckBox {
    font-size: 13px;
    color: #666666;
}

QScrollArea {
    background-color: #f9f9f9;
    border: 1px solid #e0e0e0;
    border-radius: 6px;
}

QSplitter::handle {
    background: transparent;
}

QSplitter::handle:horizontal {
    width: 10px;
}
"""

