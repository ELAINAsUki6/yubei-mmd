# 上线手册

域名只是入口，还得把它指到一台一直开着的机器。两条路，按你的情况选。

---

## 当前这个站：ybslmmd.cn

- 域名：**ybslmmd.cn**（.cn 域名，必须完成实名认证，否则解析会被暂停）
- 电脑：常年不关机 → 走**方案 A**
- 备案：用 Cloudflare 隧道 + 家里宽带，**不需要备案**

### 现在就做（域名注册期间）

1. **等实名认证通过**。.cn 域名注册后要在注册商那边做实名认证（上传身份证），
   通常几小时到 1 个工作日。没认证的域名会被 `serverHold`，此时改 NS 也没用。
2. 在注册商后台确认域名状态是「正常 / 已实名」，不是「待实名」「serverHold」。

### 认证通过后，三步走

**第一步：域名托管到 Cloudflare**

1. 注册 [Cloudflare](https://dash.cloudflare.com/) → Add a site → 填 `ybslmmd.cn` → Free
2. 记下它给的两条 NS（长得像 `ada.ns.cloudflare.com`）
3. 去域名注册商后台 → 域名管理 → **修改 DNS 服务器** → 换成那两条
4. 等 Cloudflare 显示 **Active**（几分钟到几小时）

**第二步：一键接隧道**

```powershell
cd C:\Users\ELAINA\Documents\deepseek-harness\default-workspace\mmd-hub
.\deploy\setup-tunnel.ps1 -Domain ybslmmd.cn
```

脚本会：装 cloudflared → 登录 → 建隧道 → 写配置 → 绑 DNS。
中途会弹浏览器，登录 Cloudflare 并选中 `ybslmmd.cn` 授权。

**第三步：跑起来**

```powershell
# 窗口 1
双击 启动服务.bat

# 窗口 2
cloudflared tunnel run mmdhub
```

浏览器打开 `https://ybslmmd.cn` 就能看到。

> 想用子域名（比如 `mmd.ybslmmd.cn`）就把脚本参数改成
> `-Hostname mmd.ybslmmd.cn`。

### 开机自启

```powershell
# 隧道
cloudflared service install

# 网站本体：任务计划程序，登录时启动
schtasks /create /tn "MMDHub" /sc onlogon /rl limited ^
  /tr "\"C:\Users\ELAINA\Documents\deepseek-harness\default-workspace\mmd-hub\deploy\service-run.bat\""
```

`service-run.bat` 会把日志写到 `data\server.log`，出问题先看它。

### 速度预期

Cloudflare 免费版在中国大陆访问会绕到境外节点，**可能不快，个别时段会卡**。
自己小圈子用没问题；要国内访问顺畅，看下面的「方案 B-2：香港轻量服务器」。

---

## 方案 A：家里这台电脑 + Cloudflare Tunnel（通用说明）

**适合**：先试水、不想租服务器。
**代价**：电脑关机 / 睡眠，网站就下线。
**好处**：免费、自动 HTTPS、不用公网 IP、不用在路由器上开端口（大内网也能用）。

### 1. 把域名的 NS 交给 Cloudflare

1. 注册 [Cloudflare](https://dash.cloudflare.com/)（免费）
2. Add a site → 输入你的域名 → 选 Free
3. 它会给你两条 NS，例如
   `ada.ns.cloudflare.com` / `bob.ns.cloudflare.com`
4. 回你买域名的后台（阿里云/腾讯云/Namecheap…），找到「DNS 服务器 / 自定义 DNS」，
   把原来的 NS 改成这两条
5. 等生效（几分钟到几小时），Cloudflare 面板会显示 Active

> 改 NS 之后，原来的 DNS 解析记录全部失效，全都改在 Cloudflare 里管。

### 2. 装 cloudflared

```powershell
winget install --id Cloudflare.cloudflared
```

装完新开一个终端，`cloudflared --version` 能出版本号就行。

### 3. 建隧道

```powershell
cloudflared tunnel login          # 弹浏览器，选你的域名授权
cloudflared tunnel create mmdhub  # 打印 Tunnel ID，并把凭据存到 %USERPROFILE%\.cloudflared\
```

### 4. 写配置

把 `deploy\cloudflared-config.yml` 复制到 `%USERPROFILE%\.cloudflared\config.yml`，
把 `<TUNNEL-ID>` 和域名换成你自己的。

### 5. 绑定域名

```powershell
cloudflared tunnel route dns mmdhub mmd.你的域名.com
```

这会在 Cloudflare 里自动加一条 CNAME。

### 6. 先手动测一次

```powershell
# 窗口 1：跑网站
双击 启动服务.bat

# 窗口 2：跑隧道
cloudflared tunnel run mmdhub
```

浏览器打开 `https://mmd.你的域名.com` —— 能看到页面就通了。

### 7. 设成开机自启

```powershell
cloudflared service install
```

网站本体（server.py）也设成开机自启，用任务计划程序：

- 触发器：登录时
- 操作：启动程序 `python`，参数 `-u server.py 8000`，起始于 `...\mmd-hub`
- 勾「不管用户是否登录都运行」需要填密码

---

## 方案 B：云服务器 + Docker + nginx + Let's Encrypt

**适合**：想长期稳定在线。
**代价**：20–40 元/月。
**好处**：不用开着自家电脑，HTTPS 自主可控。

### 1. 买机器

阿里云/腾讯云轻量应用服务器，2 核 2G 足够。系统选 **Ubuntu 22.04**。
安全组放行 **80 / 443**。

### 2. DNS

在你域名的 DNS 后台加一条 **A 记录**：

```
主机记录  @        记录值  服务器公网 IP
主机记录  www      记录值  服务器公网 IP
```

### 3. 装 Docker

```bash
curl -fsSL https://get.docker.com | sh
```

### 4. 传文件上去

```bash
# 在本地 Windows 上执行（把 IP 换成你的）
scp -r .\mmd-hub root@你的服务器IP:/opt/
```

或者用 WinSCP / FileZilla 拖上去。

### 5. 起服务

```bash
cd /opt/mmd-hub
mkdir -p data
# 首次启动会生成口令
docker compose up -d --build
docker compose logs | grep 口令      # 记下管理员口令
```

容器只监听 `127.0.0.1:8000`，外面靠 nginx。

### 6. nginx + 证书

```bash
apt update && apt install -y nginx certbot python3-certbot-nginx
```

把 `deploy/nginx.conf` 里的域名改好，复制到 `/etc/nginx/conf.d/mmdhub.conf`，
然后：

```bash
certbot --nginx -d 你的域名.com -d www.你的域名.com
```

certbot 会自动签证书、写好 ssl 配置、设好续期。

```bash
nginx -t && systemctl reload nginx
```

访问 `https://你的域名.com`。

---

## 上线后要做的三件事

1. **改口令**：把 `data/admin.token` 删掉，重启服务，会生成新的。
   浏览器里记住的旧口令也要清（F12 → Application → Local Storage → 删 `mmd-hub:admintoken`）。
2. **备份**：`data/published.json` 是全部家当，定期拷走。
   ```bash
   # 服务器上加个每日备份
   crontab -e
   0 3 * * * tar czf /root/mmdhub-$(date +\%F).tgz -C /opt/mmd-hub data
   ```
3. **确认 `/data/` 打不开**：浏览器访问 `https://你的域名.com/data/published.json`
   应该是 404。能打开就说明 nginx 那条拦截没生效。

---

## 常见问题

**打不开 / 502**
- 服务器上 `curl 127.0.0.1:8000/api/data` 通不通？
- 不通就是 server.py 没起来；通了就是 nginx 配置问题。
- `journalctl -u nginx -n 50` 或 `docker compose logs` 看错误。

**证书签发失败**
- 80 端口没放行，或者域名还没解析到这台机器。
- `dig 你的域名.com` 确认指向公网 IP。

**提交一直提示“太频繁”**
- 反向代理没传真实 IP，所有请求都被当成 127.0.0.1。
- 检查 nginx 的 `X-Real-IP` / `X-Forwarded-For` 有没有设。

**Cloudflare 隧道断了**
- `cloudflared service status` 看一眼；重装看 `cloudflared service install` 的日志。
