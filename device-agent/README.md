# PhonesInventory 本地读机 Agent (MVP)

插上 iPhone、手机点“信任此电脑”，网页「二手回收 → 评估估价」里点「📲 读取设备」即可
自动填入 型号 / 容量 / 序列号 / IMEI / 系统版本 / 激活状态 / **电池循环次数 / 健康度**。

不越狱，走苹果官方 usbmux/lockdown 协议，只读、只监听本机 127.0.0.1、不上传数据。

## 运行

### macOS
1. 装 Python 3（`brew install python` 或官网）
2. 双击 `run-mac.command`（首次自动装依赖）
   - 若提示“无法打开”，右键→打开，或终端 `chmod +x run-mac.command`

### Windows
1. 装 Python 3（勾选 Add to PATH）
2. 装 **Apple Mobile Device Support**（安装 iTunes 即带，或单独驱动）
3. 双击 `run-windows.bat`

### 手动
```
pip install -r requirements.txt
python3 agent.py
```

启动后监听 `http://127.0.0.1:8767`，保持这个窗口开着即可。

## 接口
- `GET /health` → Agent 是否在运行
- `GET /device` → 读取当前连接的设备信息（JSON）

返回的 `code` 含义：`no_deps`(缺依赖) / `no_device`(没插机) / `need_trust`(手机没点信任) / `usbmux_error`(Windows 缺驱动)。

## 常见问题
- **点了没反应 / 提示 Agent 未运行**：确认这个黑窗口开着，且网页地址在 `agent.py` 的 `ALLOWED_ORIGINS` 里。
- **一直提示 need_trust**：手机解锁状态下重插数据线，点“信任”，输入锁屏密码。
- **电池循环次数为空**：个别 iOS 版本诊断字段受限；型号/系统/IMEI 一般都能读到，电池可用 GSX 或系统设置兜底。
- **激活日期/保修/销售地区**：设备本身不含，这些走 PhonesInventory 已有的 GSX(IMEI) 查询补充。

## 打包成免安装 exe/app（可选，之后做）
用 PyInstaller：`pip install pyinstaller && pyinstaller -F agent.py`，产出单文件给员工双击，无需装 Python。
