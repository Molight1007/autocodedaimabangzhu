MESSAGE_BOX_QSS = """
QDialog {
    background-color: #ffffff;
    border-radius: 12px;
}

QWidget#Container {
    background-color: #ffffff;
    border-radius: 12px;
}

QLabel#IconLabel {
    background-color: transparent;
}

QLabel#TitleLabel {
    font-size: 18px;
    font-weight: 800;
    color: #1d1d1f;
    background-color: transparent;
}

QLabel#ContentLabel {
    font-size: 14px;
    color: #333333;
    line-height: 1.5;
    background-color: transparent;
}

QTextBrowser#ContentBrowser {
    font-size: 14px;
    color: #333333;
    line-height: 1.5;
    background-color: transparent;
    border: none;
    padding: 0px;
}

QPushButton {
    background-color: #ffffff;
    color: #333333;
    border: 1px solid #d1d1d6;
    border-radius: 8px;
    font-weight: 700;
    padding: 8px 20px;
    min-height: 32px;
    min-width: 80px;
}

QPushButton:hover {
    border-color: #007AFF;
    color: #007AFF;
    background-color: #f0f7ff;
}

QPushButton:pressed {
    background-color: #e0efff;
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

QPushButton#PrimaryBtn:pressed {
    background-color: #0052a3;
}

QPushButton#DangerBtn {
    background-color: #ff3b30;
    color: white;
    border: none;
    font-weight: 800;
}

QPushButton#DangerBtn:hover {
    background-color: #e6352b;
    color: white;
}

QPushButton#DangerBtn:pressed {
    background-color: #cc2f26;
}

QPushButton#WarningBtn {
    background-color: #ff9500;
    color: white;
    border: none;
    font-weight: 800;
}

QPushButton#WarningBtn:hover {
    background-color: #e68600;
    color: white;
}

QPushButton#WarningBtn:pressed {
    background-color: #cc7700;
}

QPushButton#SuccessBtn {
    background-color: #34c759;
    color: white;
    border: none;
    font-weight: 800;
}

QPushButton#SuccessBtn:hover {
    background-color: #2eb350;
    color: white;
}

QPushButton#SuccessBtn:pressed {
    background-color: #299c47;
}
"""
