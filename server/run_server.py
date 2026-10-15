import os
import sys
import multiprocessing
import socket

# 针对打包环境的路径修正
if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))
    sys.path.insert(0, sys._MEIPASS)

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from main import app
from database import DB_HOST, DB_PORT, DB_NAME
import uvicorn

def get_machine_ips(port=8000):
    urls = []
    hostname = socket.gethostname()
    try:
        _, _, ip_list = socket.gethostbyname_ex(hostname)
        for ip in ip_list:
            if ip and not ip.startswith('127.') and not ip.startswith('169.254.'):
                desc = '局域网/内网'
                if ip.startswith('100.'):
                    desc = 'Tailscale / 异地专线'
                elif ip.startswith('172.16.') or ip.startswith('172.19.'):
                    desc = 'VPN / 虚拟网卡'
                elif ip.startswith('192.168.'):
                    desc = '局域网 (Wi-Fi/以太网)'
                elif ip.startswith('10.'):
                    desc = '企业内网 / VPN'
                urls.append((desc, ip, f'http://{ip}:{port}'))
    except Exception:
        pass
    urls.append(('本机本地回环', '127.0.0.1', f'http://127.0.0.1:{port}'))
    return hostname, urls

if __name__ == "__main__":
    multiprocessing.freeze_support()
    hostname, net_urls = get_machine_ips(8000)

    print("=================================================================")
    print("   Codex Auth Hub 集中式认证与额度调度服务端 (FastAPI + PG)")
    print("=================================================================")
    print(f"[*] 宿主机器主机名: {hostname}")
    print(f"[*] 关联 PostgreSQL 数据库: {DB_HOST}:{DB_PORT}/{DB_NAME}")
    print("-----------------------------------------------------------------")
    print("[*] 动态检测到本机可用网络接口与访问地址:")
    for desc, ip, url in net_urls:
        print(f"    - [{desc:<18}]  {url}")
    print("-----------------------------------------------------------------")
    print("提示:")
    print("  1. 局域网同网段机器：请连接上方的 [局域网] 地址")
    print("  2. VPN / 异地隔离机：请连接上方的 [Tailscale] 或 [VPN] 地址")
    print("  3. 客户端首次运行只需输入上方对应的地址即可自动记住")
    print("=================================================================\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

