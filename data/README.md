# data 目录说明

这个目录里的文件是**运行时自动生成**的，不应该提交到 git：

| 文件 | 内容 |
|---|---|
| `admin.token` | 管理员静态口令（首次启动生成） |
| `totp.secret` | 动态口令密钥（手机验证器用） |
| `secret.key` | 用户登录票的签名密钥 |
| `users.json` | 注册用户（密码哈希） |
| `favs.json` | 每个用户的收藏 |
| `baidu.token` | 百度推送 token |
| `indexnow.key` | 搜索引擎验证密钥 |
| `published.json` | 线上数据（分区 / 角色 / 配布地址） |
| `pending.json` / `rejected.json` | 审核队列 |

把仓库 clone 下来之后，直接 `python server.py 8000`，这些会自动生成。