#!/usr/bin/env bash
# 二游 MMD 模型导航 —— 服务器一键初始化
#
# 用法（在服务器上，把整个 mmd-hub 文件夹传上去之后）：
#   sudo bash deploy/server-setup.sh ybslmmd.cn
#
# 它做这些事：
#   1. 装 python3 / nginx / certbot
#   2. 把网站注册成 systemd 服务（开机自启、挂了自动重启）
#   3. 配好 nginx 反向代理（80 端口）
#   4. 提示你签 HTTPS 证书
#
# 只支持 Debian / Ubuntu。

set -e

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
    echo "用法: sudo bash deploy/server-setup.sh 你的域名"
    echo "例如: sudo bash deploy/server-setup.sh ybslmmd.cn"
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行"
    exit 1
fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -f "$APP_DIR/server.py" ]; then
    echo "找不到 $APP_DIR/server.py —— 请把整个 mmd-hub 文件夹传上来再跑这个脚本"
    exit 1
fi

echo "=============================================="
echo " 网站目录: $APP_DIR"
echo " 域名    : $DOMAIN"
echo "=============================================="

# ---------- 1. 系统包 ----------
echo
echo "[1/4] 安装依赖（python3 / nginx / certbot）…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 nginx certbot python3-certbot-nginx >/dev/null
echo "     ok"

# ---------- 2. systemd 服务 ----------
echo "[2/4] 注册 systemd 服务…"
mkdir -p "$APP_DIR/data"
# 让 www-data 能读写 data 目录（存数据、生成密钥）
chmod 755 "$APP_DIR"
chmod -R a+rX "$APP_DIR"
chown -R www-data:www-data "$APP_DIR/data"

cat > /etc/systemd/system/mmdhub.service <<EOF
[Unit]
Description=二游 MMD 模型导航
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 -u server.py 8000 127.0.0.1
Restart=always
RestartSec=3
User=www-data
Group=www-data
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mmdhub >/dev/null 2>&1
systemctl restart mmdhub
sleep 2
if systemctl is-active --quiet mmdhub; then
    echo "     服务已启动"
else
    echo "     服务启动失败，看日志：journalctl -u mmdhub -n 30 --no-pager"
    exit 1
fi

# ---------- 3. nginx ----------
echo "[3/4] 配置 nginx 反代…"
cat > /etc/nginx/sites-available/mmdhub <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    client_max_body_size 256k;

    proxy_set_header Host              \$host;
    proxy_set_header X-Real-IP         \$remote_addr;
    proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    # 数据文件和隐藏文件不对外
    location ~ ^/data/ { return 404; }
    location ~ /\\.     { return 404; }

    location = /api/submit {
        limit_req zone=mmd_submit burst=5 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
EOF

# 限流 zone 要写在 http 块里
if ! grep -q "mmd_submit" /etc/nginx/nginx.conf; then
    sed -i "/http {/a\\    limit_req_zone \$binary_remote_addr zone=mmd_submit:10m rate=10r/m;" /etc/nginx/nginx.conf
fi

ln -sf /etc/nginx/sites-available/mmdhub /etc/nginx/sites-enabled/mmdhub
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
echo "     nginx 已生效"

# ---------- 4. 自检 ----------
echo "[4/4] 自检…"
if curl -fsS --max-time 5 "http://127.0.0.1:8000/api/data" >/dev/null; then
    echo "     后端 8000 正常"
else
    echo "     后端没响应，看：journalctl -u mmdhub -n 30 --no-pager"
    exit 1
fi

IP=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo "你的公网IP")
echo
echo "=============================================="
echo " 完成！"
echo
echo " 现在可以试： http://$IP/"
echo " （域名还没解析过来的话，先用 IP 看效果）"
echo
echo " 下一步：签 HTTPS 证书（把域名解析到这台机器之后跑）"
echo "   certbot --nginx -d $DOMAIN"
echo
echo " 管理员动态口令（第一次启动时生成的）："
echo "   grep -A3 '动态口令' /var/log/syslog | tail -20"
echo "   或者： systemctl restart mmdhub && journalctl -u mmdhub -n 30 --no-pager"
echo
echo " 数据目录： $APP_DIR/data"
echo "=============================================="
