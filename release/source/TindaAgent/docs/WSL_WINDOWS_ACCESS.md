# WSL -> Windows 浏览器访问说明

当 `start.sh` 显示服务已启动，但 Windows 浏览器访问 `http://127.0.0.1:<port>` 报 `ERR_CONNECTION_REFUSED`，通常是 WSL localhost 转发失效。

## 快速判断

1. 在 WSL 内检查服务：

```bash
ss -ltnp | rg ':8000\\b'
curl -I http://127.0.0.1:8000/
```

2. 在 Windows 侧检查 `127.0.0.1`：

```bat
curl -v http://127.0.0.1:8000/
```

如果 WSL 内通、Windows 本地回环不通，就是转发问题。

## 立即可用方案

直接用 WSL IP 访问（`start.sh` 会打印该地址）：

```text
http://<WSL_IP>:<port>
```

例如：`http://172.19.84.102:8000/`

## 根治方案（恢复 Windows 访问 127.0.0.1）

1. 在 Windows 用户目录创建或编辑 `%UserProfile%\\.wslconfig`：

```ini
[wsl2]
localhostForwarding=true
```

2. 关闭所有 WSL 实例并重启：

```bat
wsl --shutdown
```

3. 重新启动 Ubuntu 与服务，再访问：

```text
http://127.0.0.1:8000/
```

## 补充

1. `0.0.0.0` 是监听地址，不是浏览器访问地址。
2. `ping http://...` 是错误用法；检查 HTTP 应该用 `curl` 或浏览器。
