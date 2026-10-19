# -*- coding: utf-8 -*-
"""服务与门禁之间的**接口契约版本**（唯一来源）。

为什么单独一个文件，而不是在 server.py 里写个常量：
    门禁（tools/svc_probe.py、_run_regression.sh）和前端都会读同一个数。
    写在两处，早晚会出现「服务报 2、门禁期望 1」这种偏差 —— 而这类偏差
    长得跟"产品坏了"一模一样，最难查。
    2026-10-19 就踩过一次：train_rounds_ui 红了，根因不是代码，是**跑在
    昨天的旧进程上**，新接口 404。版本守卫就是为这件事加的。

什么时候要改 API_VERSION：
    * 只加**向后兼容**的东西（多一个字段、多一个端点）→ 不用改。
    * 改了已有接口的语义、删了字段、挪了路径 → API_VERSION += 1。
    * 新门禁依赖新接口 → 把 API_MIN 抬到那一版。
      `_run_regression.sh` 每关跑之前先打 /api/health；服务版本 < API_MIN
      就**直接拒绝跑**并提示重启，而不是让 20 道 UI 门禁集体假红。
"""

API_VERSION = 2
API_MIN = 2
SERVICE = "voicestudio"
