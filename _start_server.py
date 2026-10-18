# -*- coding: utf-8 -*-
"""把 VoiceStudio 常驻服务（127.0.0.1:8756）拉起来并**脱离本进程**。

为什么需要它：直接 `python server.py &` 或在工具里起后台任务，进程会随那条命令/那一轮
结束被回收 —— 实测服务跑着跑着就没了，界面门禁于是整页拿不到数据（全红，n=0 张卡片），
看起来像产品坏了。这里用 DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP，
和 stages/yue2.py 里拉起 YuE2 引擎用的是同一套做法。

用法：
    python _start_server.py          # 已经在跑就什么都不做
    python _start_server.py --force  # 不管在不在跑，再起一个（一般别用）
"""
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8756
# 和 启动VoiceStudio.bat 里用的是同一个解释器（GSV runtime）
PY = (r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版"
      r"\GPT-SoVITS-v5-20261005nvidia50x0"
      r"\GPT-SoVITS-v2pro-20261006nvidia50x0\runtime\python.exe")


def listening(port, host="127.0.0.1", timeout=0.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def main():
    if not os.path.isfile(PY):
        print("STARTSERVER 解释器不在：%s" % PY)
        return 2

    if listening(PORT) and "--force" not in sys.argv:
        print("STARTSERVER 已在运行（:%d 有监听），不动它" % PORT)
        return 0

    log_path = os.path.join(HERE, "_server.log")
    log = open(log_path, "a", encoding="utf-8", errors="replace")
    log.write("\n==== STARTSERVER 拉起 %s ====\n" % time.strftime("%m-%d %H:%M:%S"))
    log.flush()

    subprocess.Popen(
        [PY, "-u", os.path.join(HERE, "server.py")],
        cwd=HERE,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=(subprocess.DETACHED_PROCESS
                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        close_fds=True,
    )

    for _ in range(40):          # 最多等 20 秒
        time.sleep(0.5)
        if listening(PORT):
            print("STARTSERVER 已就绪 http://127.0.0.1:%d/" % PORT)
            return 0
    print("STARTSERVER 起了但 20 秒内没监听，看 %s" % log_path)
    return 1


if __name__ == "__main__":
    sys.exit(main())
