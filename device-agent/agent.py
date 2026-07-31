#!/usr/bin/env python3
"""
PhonesInventory 本地读机 Agent (MVP)
------------------------------------
插上 iPhone、手机点“信任”，本 Agent 通过苹果 usbmux/lockdown 协议(不越狱)读取
设备信息(型号/容量/序列号/IMEI/系统版本/激活状态/电池循环次数/健康度)，并在
localhost 暴露一个只读 HTTP 接口，供 PhonesInventory 网页「评估估价」自动填单。

跨平台: Windows / macOS / Linux。Windows 需已安装 Apple Mobile Device Support
(随 iTunes 或单独驱动)。

依赖:  pip install pymobiledevice3
运行:  python3 agent.py           (默认端口 8767，只绑定 127.0.0.1)

安全: 只监听回环地址、只读、不上传任何数据；CORS 只放行 ALLOWED_ORIGINS。
"""
import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8767
# 允许调用本 Agent 的网页来源。生产站 + 本地开发。
ALLOWED_ORIGINS = {
    'https://phonesinventory.com',
    'https://www.phonesinventory.com',
    'http://localhost:8580',
    'http://127.0.0.1:8580',
}

# ProductType → 市场型号名 (常见机型，可继续补充)。查不到时回退 ProductType。
MODEL_NAMES = {
    'iPhone12,1': 'iPhone 11', 'iPhone12,3': 'iPhone 11 Pro', 'iPhone12,5': 'iPhone 11 Pro Max',
    'iPhone12,8': 'iPhone SE (2nd gen)',
    'iPhone13,1': 'iPhone 12 mini', 'iPhone13,2': 'iPhone 12', 'iPhone13,3': 'iPhone 12 Pro', 'iPhone13,4': 'iPhone 12 Pro Max',
    'iPhone14,2': 'iPhone 13 Pro', 'iPhone14,3': 'iPhone 13 Pro Max', 'iPhone14,4': 'iPhone 13 mini', 'iPhone14,5': 'iPhone 13',
    'iPhone14,6': 'iPhone SE (3rd gen)', 'iPhone14,7': 'iPhone 14', 'iPhone14,8': 'iPhone 14 Plus',
    'iPhone15,2': 'iPhone 14 Pro', 'iPhone15,3': 'iPhone 14 Pro Max', 'iPhone15,4': 'iPhone 15', 'iPhone15,5': 'iPhone 15 Plus',
    'iPhone16,1': 'iPhone 15 Pro', 'iPhone16,2': 'iPhone 15 Pro Max',
    'iPhone17,1': 'iPhone 16 Pro', 'iPhone17,2': 'iPhone 16 Pro Max', 'iPhone17,3': 'iPhone 16', 'iPhone17,4': 'iPhone 16 Plus',
}

STORAGE_TIERS = [64, 128, 256, 512, 1024]


def _storage_from_bytes(total):
    """把磁盘总容量(字节)映射到标称容量档 (128GB/256GB...)。标称容量约为可用的 1.07 倍。"""
    if not total:
        return ''
    gb = total / 1_000_000_000.0
    best = min(STORAGE_TIERS, key=lambda t: abs(t - gb * 1.07))
    return ('1TB' if best == 1024 else str(best) + 'GB')


async def _read_device_async():
    from pymobiledevice3.usbmux import list_devices
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.diagnostics import DiagnosticsService

    devices = await list_devices()
    if not devices:
        return {'ok': False, 'code': 'no_device', 'error': '未检测到设备，请插好数据线'}
    udid = getattr(devices[0], 'serial', None) or getattr(devices[0], 'udid', None)

    try:
        lockdown = await create_using_usbmux(serial=udid)
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ('pair', 'trust', 'passcode', 'password')):
            return {'ok': False, 'code': 'need_trust', 'error': '请在手机上点“信任此电脑”后重试'}
        return {'ok': False, 'code': 'lockdown_error', 'error': '连接失败: ' + str(e)}

    async def gv(key, domain=None):
        try:
            return await lockdown.get_value(domain, key)
        except Exception:
            return None

    product_type = (await gv('ProductType')) or ''
    info = {
        'ok': True,
        'udid': udid,
        'productType': product_type,
        'model': MODEL_NAMES.get(product_type, product_type),
        'serial': await gv('SerialNumber'),
        'imei': await gv('InternationalMobileEquipmentIdentity'),
        'imei2': await gv('InternationalMobileEquipmentIdentity2'),
        'ios': await gv('ProductVersion'),
        'deviceName': await gv('DeviceName'),
        'modelNumber': await gv('ModelNumber'),
        'regionInfo': await gv('RegionInfo'),
        'activationState': await gv('ActivationState'),
    }

    try:
        info['storage'] = _storage_from_bytes(await gv('TotalDiskCapacity', 'com.apple.disk_usage'))
    except Exception:
        info['storage'] = ''

    info['batteryCycle'] = None
    info['batteryHealth'] = None
    try:
        diag = DiagnosticsService(lockdown)
        bat = await diag.get_battery() or {}
        if isinstance(bat, dict):
            cyc = bat.get('CycleCount')
            raw_max = bat.get('AppleRawMaxCapacity') or bat.get('MaxCapacity') or bat.get('NominalChargeCapacity')
            design = bat.get('DesignCapacity')
            if cyc is not None:
                info['batteryCycle'] = cyc
            if raw_max and design:
                info['batteryHealth'] = round(raw_max / design * 100)
    except Exception:
        pass

    try:
        res = lockdown.close()
        if asyncio.iscoroutine(res):
            await res
    except Exception:
        pass
    return info


def read_device():
    """读取第一台已连接并信任的 iOS 设备。返回 dict（含错误码）。"""
    try:
        import pymobiledevice3  # noqa: F401
    except Exception:
        return {'ok': False, 'code': 'no_deps',
                'error': '未安装依赖，请先运行: pip install pymobiledevice3'}
    try:
        return asyncio.run(_read_device_async())
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ('pair', 'trust', 'passcode', 'password')):
            return {'ok': False, 'code': 'need_trust', 'error': '请在手机上点“信任此电脑”后重试'}
        return {'ok': False, 'code': 'usbmux_error',
                'error': 'USB 服务不可用 (Windows 需装 Apple Mobile Device Support): ' + str(e)}


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        origin = self.headers.get('Origin', '')
        allow = origin if origin in ALLOWED_ORIGINS else 'null'
        self.send_header('Access-Control-Allow-Origin', allow)
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._cors()
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/health':
            return self._json({'ok': True, 'agent': 'phonesinventory-device-agent', 'version': 1})
        if path == '/device':
            try:
                return self._json(read_device())
            except Exception as e:
                return self._json({'ok': False, 'code': 'error', 'error': str(e)}, 500)
        return self._json({'ok': False, 'error': 'not found'}, 404)

    def log_message(self, fmt, *args):
        print('[agent] ' + (fmt % args))


def main():
    print('PhonesInventory 读机 Agent 启动 → http://127.0.0.1:%d' % PORT)
    print('接口: GET /device (读取设备)  ·  GET /health')
    print('插上 iPhone 并在手机上点“信任此电脑”即可。Ctrl+C 退出。')
    try:
        ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print('\n已退出。')
        sys.exit(0)


if __name__ == '__main__':
    main()
