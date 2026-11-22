#!/usr/bin/env bash
# autoCode 精简版 —— Ubuntu 22.04/24.04 一键安装
#
# 用法：
#   sudo bash install-linux.sh <安装包.tar.gz> [对外IP或域名]
#
# 例：
#   sudo bash install-linux.sh autocode-slim2-linux-20261120.tar.gz <服务器IP>
#
# 第二个参数决定「用户浏览器里输入什么能访问」：
#   传了 => 绑定 0.0.0.0，白名单只放这个地址（适合先用 IP 直连）
#   不传 => 只绑 127.0.0.1，留给前置 HTTPS 反向代理（Caddy/nginx）
#
# 装完后还需要在腾讯云控制台「防火墙」里放行对应端口，脚本改不了安全组。

set -euo pipefail

PKG="${1:?用法: sudo bash install-linux.sh <安装包.tar.gz> [对外IP或域名]}"
PUBLIC_HOST="${2:-}"
APP_DIR=/opt/autocode
ETC_DIR=/etc/autocode
SVC_USER=autocode
SVC_NAME=autocode-slim

if [ "$(id -u)" -ne 0 ]; then
    echo "[失败] 请用 sudo 运行"
    exit 1
fi
if [ ! -f "$PKG" ]; then
    echo "[失败] 找不到安装包：$PKG"
    exit 1
fi

echo "== 1/6 系统依赖 =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# gcc/g++ 是必须的：对抗数据要真编译、真运行学生代码
apt-get install -y -qq python3-venv python3-pip gcc g++ curl >/dev/null
echo "   python: $(python3 -V)"

# bubblewrap：学生代码的沙箱（只读根 + 断网 + 独立 pid），缺它对抗链路会直接失败
apt-get install -y -qq bubblewrap >/dev/null 2>&1 || true
if command -v bwrap >/dev/null 2>&1; then
    echo "   bwrap: $(bwrap --version)"
else
    echo "   [警告] bubblewrap 安装失败，请在 $ETC_DIR/slim2.env 里去掉 AUTOCODE_SANDBOX=bwrap"
fi

# Ubuntu 24.04 默认禁止非特权用户命名空间，bwrap 会报
# "setting up uid map: Permission denied"。沙箱本身要靠它隔离，故显式放开。
if [ -d /etc/sysctl.d ]; then
    echo "kernel.apparmor_restrict_unprivileged_userns=0" > /etc/sysctl.d/99-autocode-userns.conf
    sysctl --system >/dev/null 2>&1 || true
    echo "   userns: $(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo unknown)"
fi

echo "== 2/6 用户与目录 =="
id "$SVC_USER" >/dev/null 2>&1 || useradd -r -m -d "$APP_DIR" -s /bin/bash "$SVC_USER"
mkdir -p "$APP_DIR" "$ETC_DIR"
tar xzf "$PKG" -C "$APP_DIR"
mkdir -p "$APP_DIR/data" "$APP_DIR/logs/slim2"
chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"

echo "== 3/6 虚拟环境 =="
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    sudo -u "$SVC_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements-server.txt"
echo "   依赖已装"

echo "== 4/6 环境变量 =="
if [ ! -f "$ETC_DIR/slim2.env" ]; then
    cp "$APP_DIR/deploy/slim2.env.example" "$ETC_DIR/slim2.env"
fi
if [ -n "$PUBLIC_HOST" ]; then
    sed -i "s|^AUTOCODE_SLIM2_HOST=.*|AUTOCODE_SLIM2_HOST=0.0.0.0|" "$ETC_DIR/slim2.env"
    sed -i "s|^AUTOCODE_SLIM2_ALLOWED_HOSTS=.*|AUTOCODE_SLIM2_ALLOWED_HOSTS=${PUBLIC_HOST},127.0.0.1,localhost|" "$ETC_DIR/slim2.env"
    echo "   绑定 0.0.0.0，白名单：${PUBLIC_HOST}"
else
    echo "   只绑 127.0.0.1（请在前面放 HTTPS 反向代理）"
fi
chmod 600 "$ETC_DIR/slim2.env"
chown root:root "$ETC_DIR/slim2.env"

if grep -q '^DEEPSEEK_API_KEY=sk-replace-me' "$ETC_DIR/slim2.env"; then
    echo "   [注意] DEEPSEEK_API_KEY 还是占位值，登录后生成数据会失败"
    echo "          编辑 $ETC_DIR/slim2.env 填入真实 Key，然后 systemctl restart $SVC_NAME"
fi

echo "== 5/6 服务 =="
cp "$APP_DIR/deploy/autocode-slim.service" "/etc/systemd/system/${SVC_NAME}.service"
systemctl daemon-reload
systemctl enable "$SVC_NAME" >/dev/null 2>&1 || true
systemctl restart "$SVC_NAME"
sleep 2

echo "== 6/6 自检 =="
PORT=$(grep -E '^AUTOCODE_SLIM2_PORT=' "$ETC_DIR/slim2.env" | cut -d= -f2 | tr -d '[:space:]')
PORT=${PORT:-8767}
if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
    echo "   服务正常（127.0.0.1:${PORT}/healthz）"
else
    echo "   [失败] 健康检查不过，最近日志："
    journalctl -u "$SVC_NAME" -n 30 --no-pager || true
    exit 1
fi

echo
echo "================ 安装完成 ================"
if [ -f "$APP_DIR/data/slim2-initial-password.txt" ]; then
    echo "初始密码（首次登录后删除该文件）："
    sed -n '1,2p' "$APP_DIR/data/slim2-initial-password.txt"
else
    echo "密码文件：$APP_DIR/data/slim2-initial-password.txt"
    echo "（若不存在，说明用了已有的 credentials，改密：$APP_DIR/.venv/bin/python -m autocode.slim2.passwd）"
fi
if [ -n "$PUBLIC_HOST" ]; then
    echo "访问地址：http://${PUBLIC_HOST}:${PORT}/"
    echo "还没完：请在腾讯云控制台→防火墙，放行 TCP ${PORT}"
else
    echo "后端地址：http://127.0.0.1:${PORT}/（需前置反向代理才能对外）"
fi
echo "日志：journalctl -u ${SVC_NAME} -f    数据：$APP_DIR/data/slim2.db"
