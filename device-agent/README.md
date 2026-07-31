# PhonesInventory 本地读机 Agent (MVP)

插上 iPhone、手机点“信任此电脑”，网页「二手回收 → 评估估价」里点「📲 读取设备」即可
自动填入 型号 / 容量 / 序列号 / IMEI / 系统版本 / 激活状态 / **电池循环次数 / 健康度**。

不越狱，走苹果官方 usbmux/lockdown 协议，只读、只监听本机 127.0.0.1、不上传数据。

## 门店用（Windows · 免安装 exe，推荐）

门店电脑无需装 Python，只需要一个 exe：

1. **先在门店电脑装一次 Apple Mobile Device Support**（装 iTunes 即自带，或单独装 Apple 的 USB 驱动）——这是 iPhone USB 通信必需
2. 拿到 `PhonesInventory-DeviceAgent.exe`（见下方“怎么拿到 exe”），拷到门店电脑
3. 双击运行，保持黑窗口开着
4. iPhone 插线 → 手机点“信任此电脑” → 网页点「📲 读取设备」

### 怎么拿到这个 exe
本项目已配好云端自动打包（`.github/workflows/build-agent.yml`），在 GitHub 上一键出 exe：

- 打开仓库 → **Actions** 标签 → 左侧 **Build Device Agent (Windows)** → 右侧 **Run workflow** → 跑完后在该次运行页面底部 **Artifacts** 下载 `PhonesInventory-DeviceAgent-windows`（解压得到 exe）
- 或本地在任意 Windows 机器上双击 `build-windows.bat` 自己打（需装 Python 3），产出 `dist\PhonesInventory-DeviceAgent.exe`

> exe 是 Windows 免安装单文件，但仍依赖上面第 1 步的 Apple USB 驱动。

## 开发者运行（源码，Mac/Windows）
```
pip install -r requirements.txt
python3 agent.py       # Windows: python agent.py
```
Mac 也可双击 `run-mac.command`。启动后监听 `http://127.0.0.1:8767`，保持窗口开着。

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
