#!/usr/bin/env python3
"""
PhoneInventory 全面压力测试 + 功能验证
测试所有 API 端点的正确性、边界条件、并发安全性
"""
import json
import os
import sqlite3
import time
import threading
import urllib.request
import urllib.error
import random
import string
import sys

# ─── Config ───
API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8580")
DEPLOY_DIR = os.environ.get("DEPLOY_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(DEPLOY_DIR, "data", "inventory.db")

# Auth: all endpoints (except login/pin-reset/health) require X-Auth-Token
TEST_EMAIL = os.environ.get("TEST_EMAIL", "anderson@ifixforu.com")
TEST_PIN = os.environ.get("TEST_PIN", "888888")
AUTH_TOKEN = None

PASS = 0
FAIL = 0
WARN = 0
ERRORS = []

def api(method, path, data=None, expect_status=None, token=True):
    """Make an API call and return parsed JSON."""
    url = API_BASE + "/api" + path
    body = json.dumps(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header('Content-Type', 'application/json')
    if token and AUTH_TOKEN:
        req.add_header('X-Auth-Token', AUTH_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            if expect_status and resp.status != expect_status:
                return {'_error': f'Expected {expect_status}, got {resp.status}', '_data': result}
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        try:
            return json.loads(body)
        except:
            return {'error': body, '_status': e.code}
    except Exception as e:
        return {'error': str(e)}


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        msg = f"  ❌ {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


def warn(name, detail=""):
    global WARN
    WARN += 1
    print(f"  ⚠️  {name}" + (f" — {detail}" if detail else ""))


def random_imei():
    """Generate a random 15-digit IMEI for testing."""
    return '99' + ''.join(random.choices('0123456789', k=13))


# ═══════════════════════════════════════
# TEST SUITE 1: Health Check
# ═══════════════════════════════════════
def test_health():
    print("\n📋 TEST 1: Health Check")
    res = api('GET', '/health')
    check("Health endpoint responds", res is not None)
    check("Health status field exists", 'status' in res, str(res))
    check("Health checks field exists", 'checks' in res, str(res))
    if 'checks' in res:
        for k, v in res['checks'].items():
            check(f"Health check [{k}]", v == 'ok', f"value={v}")


# ═══════════════════════════════════════
# TEST SUITE 2: Inventory CRUD
# ═══════════════════════════════════════
def test_inventory():
    print("\n📋 TEST 2: Inventory Operations")

    # GET inventory
    res = api('GET', '/inventory')
    check("GET /inventory returns data", 'inventory' in res)
    check("GET /inventory has total", 'total' in res)
    total = res.get('total', 0)
    check("Inventory count > 0", total > 0, f"total={total}")

    # GET with store filter
    res = api('GET', '/inventory?store=alhambra')
    check("GET /inventory?store filter works", 'inventory' in res)

    # GET with status filter
    res = api('GET', '/inventory?status=available')
    check("GET /inventory?status filter works", 'inventory' in res)
    avail = res.get('total', 0)
    check("Available phones exist", avail > 0, f"available={avail}")

    # Pick a real available phone for further tests
    if res.get('inventory'):
        phone = res['inventory'][0]
        imei = phone.get('imei', '')
        check("Phone has IMEI", len(imei) == 15, f"imei={imei}")
        check("Phone has store", bool(phone.get('store')), f"store={phone.get('store')}")
        check("Phone has status", phone.get('status') == 'available')
        return phone
    return None


# ═══════════════════════════════════════
# TEST SUITE 3: Inventory Update
# ═══════════════════════════════════════
def test_inventory_update():
    print("\n📋 TEST 3: Inventory Update (non-destructive)")

    # Try updating a non-existent IMEI
    res = api('PUT', '/inventory/000000000000000', {'status': 'available'})
    check("Update non-existent returns 404", res.get('error') == 'Record not found')

    # Try updating with invalid IMEI format
    res = api('PUT', '/inventory/000000000000000', {'imei': '12345'})
    check("Update with invalid IMEI rejected", 'error' in res)

    # Status change validation
    res = api('PUT', '/inventory/000000000000000', {'status': 'hacked'})
    # Should return 404 (record not found), not succeed
    check("Invalid status on missing record = 404", res.get('error') == 'Record not found')


# ═══════════════════════════════════════
# TEST SUITE 4: Sales Flow
# ═══════════════════════════════════════
def test_sales():
    print("\n📋 TEST 4: Sales Operations")

    # GET sales
    res = api('GET', '/sales')
    check("GET /sales returns data", 'sales' in res)
    sales = res.get('sales', [])
    check("Sales list is array", isinstance(sales, list))

    # Check field mapping
    if sales:
        s = sales[0]
        check("Sale has phoneName (camelCase)", 'phoneName' in s)
        check("Sale has storeKey (camelCase)", 'storeKey' in s)
        check("Sale has createdAt (camelCase)", 'createdAt' in s)
        check("Sale has paymentMethods (array)", isinstance(s.get('paymentMethods'), list))

    # Sold IMEIS endpoint
    res = api('GET', '/sold-imeis')
    check("GET /sold-imeis returns data", 'imeis' in res)
    check("sold-imeis is array", isinstance(res.get('imeis'), list))

    # Try creating sale without IMEI
    res = api('POST', '/sale', {'phoneName': 'Test'})
    check("Sale without IMEI rejected", 'error' in res)

    # Try creating sale with invalid IMEI
    res = api('POST', '/sale', {'imei': '12345', 'phoneName': 'Test'})
    check("Sale with invalid IMEI rejected", 'error' in res)

    # Try creating sale with non-existent IMEI
    fake_imei = random_imei()
    res = api('POST', '/sale', {
        'id': 'TEST-FAKE-001', 'imei': fake_imei, 'phoneName': 'Test',
        'store': 'Alhambra', 'storeKey': 'alhambra', 'total': 100
    })
    check("Sale with non-existent IMEI rejected", 'error' in res, str(res))

    # Try updating non-existent sale
    res = api('PUT', '/sale/NONEXISTENT-ID', {'status': 'returned'})
    check("Update non-existent sale returns not-found",
          res.get('error') == 'Sale not found', str(res))


# ═══════════════════════════════════════
# TEST SUITE 5: Transfer Flow
# ═══════════════════════════════════════
def test_transfers():
    print("\n📋 TEST 5: Transfer Operations")

    # GET transfers
    res = api('GET', '/transfers')
    check("GET /transfers returns data", 'transfers' in res)
    transfers = res.get('transfers', [])
    check("Transfers is array", isinstance(transfers, list))

    # Check field mapping
    if transfers:
        t = transfers[0]
        check("Transfer has fromStore (camelCase)", 'fromStore' in t)
        check("Transfer has toStore (camelCase)", 'toStore' in t)
        check("Transfer has requestedBy (camelCase)", 'requestedBy' in t)

    # Try invalid status transition on non-existent transfer
    res = api('PUT', '/transfer/NONEXISTENT-ID', {'status': 'approved'})
    check("Update non-existent transfer returns 404",
          res.get('error') == 'Transfer not found', str(res))

    # Try invalid status value
    res = api('PUT', '/transfer/NONEXISTENT-ID', {'status': 'hacked'})
    check("Invalid transfer status rejected", 'error' in res)


# ═══════════════════════════════════════
# TEST SUITE 6: Transfer Status Machine
# ═══════════════════════════════════════
def test_transfer_state_machine():
    print("\n📋 TEST 6: Transfer State Machine (with test data)")

    # Create a test transfer with a fake IMEI
    test_id = 'TEST-SM-' + ''.join(random.choices(string.ascii_uppercase, k=4))
    test_imei = random_imei()

    # Transfer creation requires the IMEI to exist & be available in inventory
    def seed_phone(imei):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO inventory (imei, model, store, status, condition, region) VALUES (?,?,?,?,?,?)",
                (imei, 'Test Phone SM', 'Alhambra', 'available', 'new', 'us'))
            conn.commit()
            conn.close()
        except Exception as e:
            warn("seed inventory for transfer test failed", str(e))
    seed_phone(test_imei)

    res = api('POST', '/transfer', {
        'id': test_id, 'imei': test_imei, 'phoneName': 'Test Phone',
        'fromStore': 'alhambra', 'toStore': 'irvine', 'requestedBy': 'Test'
    })
    check("Create test transfer", res.get('ok'), str(res))

    # Invalid from pending: returned is only reachable from approved/completed
    res = api('PUT', '/transfer/' + test_id, {'status': 'returned'})
    check("pending→returned blocked", 'error' in res, str(res))

    # Valid: pending → approved (legacy two-step admin flow)
    res = api('PUT', '/transfer/' + test_id, {'status': 'approved', 'approvedBy': 'Admin'})
    check("pending→approved works", res.get('ok'), str(res))

    # Try invalid: approved → approved
    res = api('PUT', '/transfer/' + test_id, {'status': 'approved'})
    check("approved→approved blocked", 'error' in res, str(res))

    # Try invalid: approved → cancelled
    res = api('PUT', '/transfer/' + test_id, {'status': 'cancelled'})
    check("approved→cancelled blocked", 'error' in res, str(res))

    # Valid: approved → completed
    res = api('PUT', '/transfer/' + test_id, {'status': 'completed'})
    check("approved→completed works", res.get('ok'), str(res))

    # Try invalid: completed → approved
    res = api('PUT', '/transfer/' + test_id, {'status': 'approved'})
    check("completed→approved blocked", 'error' in res, str(res))

    # Valid: completed → returned
    res = api('PUT', '/transfer/' + test_id, {'status': 'returned'})
    check("completed→returned works", res.get('ok'), str(res))

    # Try further transition from returned
    res = api('PUT', '/transfer/' + test_id, {'status': 'completed'})
    check("returned→completed blocked", 'error' in res, str(res))

    # Clean up: delete the test transfer from DB
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM transfers WHERE id = ?", (test_id,))
        conn.commit()
        conn.close()
        print("  🧹 Test transfer cleaned up")
    except Exception as e:
        warn("Cleanup failed", str(e))

    # Test cancel flow
    test_id2 = 'TEST-SM2-' + ''.join(random.choices(string.ascii_uppercase, k=4))
    test_imei2 = random_imei()
    seed_phone(test_imei2)
    api('POST', '/transfer', {
        'id': test_id2, 'imei': test_imei2, 'phoneName': 'Test Phone 2',
        'fromStore': 'alhambra', 'toStore': 'irvine', 'requestedBy': 'Test'
    })
    res = api('PUT', '/transfer/' + test_id2, {'status': 'cancelled'})
    check("pending→cancelled works", res.get('ok'), str(res))

    # Cancelled → anything should fail
    res = api('PUT', '/transfer/' + test_id2, {'status': 'approved'})
    check("cancelled→approved blocked", 'error' in res, str(res))

    # One-step model: receiving store confirms a pending transfer directly
    test_id3 = 'TEST-SM3-' + ''.join(random.choices(string.ascii_uppercase, k=4))
    test_imei3 = random_imei()
    seed_phone(test_imei3)
    api('POST', '/transfer', {
        'id': test_id3, 'imei': test_imei3, 'phoneName': 'Test Phone 3',
        'fromStore': 'alhambra', 'toStore': 'irvine', 'requestedBy': 'Test'
    })
    res = api('PUT', '/transfer/' + test_id3, {'status': 'completed'})
    check("pending→completed works (one-step confirm)", res.get('ok'), str(res))

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM transfers WHERE id IN (?, ?, ?)", (test_id, test_id2, test_id3))
        conn.execute("DELETE FROM inventory WHERE imei IN (?, ?, ?)", (test_imei, test_imei2, test_imei3))
        conn.commit()
        conn.close()
    except:
        pass


# ═══════════════════════════════════════
# TEST SUITE 7: Auth & PIN System
# ═══════════════════════════════════════
def test_pins():
    print("\n📋 TEST 7: Auth & PIN System")

    # user-pins broadcast is retired
    res = api('GET', '/user-pins')
    check("GET /user-pins is gone (410)", res.get('error') == 'gone', str(res))

    # No token → unauthorized
    res = api('GET', '/inventory', token=False)
    check("No token rejected (401)", res.get('error') == 'unauthorized', str(res))

    # Login: wrong PIN
    res = api('POST', '/login', {'email': TEST_EMAIL, 'pin': '000000'}, token=False)
    check("Login wrong PIN rejected", 'error' in res and not res.get('ok'), str(res))

    # Login: unknown account
    res = api('POST', '/login', {'email': 'nobody@nowhere.com', 'pin': '888888'}, token=False)
    check("Login unknown account rejected", 'error' in res and not res.get('ok'), str(res))

    # Login: valid (token already obtained in main, verify payload shape)
    res = api('POST', '/login', {'email': TEST_EMAIL, 'pin': TEST_PIN}, token=False)
    check("Login returns token", bool(res.get('token')), str(res)[:120])
    check("Login returns account", isinstance(res.get('account'), dict))

    # Change PIN: must be own email (token owner)
    res = api('POST', '/change-pin', {'email': 'jayden@ifixforu.com', 'currentPin': TEST_PIN, 'pin': '123456'})
    check("Change other's PIN rejected", 'error' in res, str(res))

    # Change PIN: wrong current PIN
    res = api('POST', '/change-pin', {'email': TEST_EMAIL, 'currentPin': '000000', 'pin': '123456'})
    check("Change PIN wrong current rejected", 'error' in res, str(res))

    # Change PIN - invalid (not 6 digits)
    res = api('POST', '/change-pin', {'email': TEST_EMAIL, 'currentPin': TEST_PIN, 'pin': '1234'})
    check("PIN too short rejected", 'error' in res)

    res = api('POST', '/change-pin', {'email': TEST_EMAIL, 'currentPin': TEST_PIN, 'pin': 'abcdef'})
    check("PIN non-digit rejected", 'error' in res)

    res = api('POST', '/change-pin', {'email': TEST_EMAIL, 'currentPin': TEST_PIN, 'pin': '888888'})
    check("Default PIN rejected as new PIN", 'error' in res)

    # Change PIN - missing fields
    res = api('POST', '/change-pin', {'email': '', 'currentPin': TEST_PIN, 'pin': '123456'})
    check("PIN without email rejected", 'error' in res)

    res = api('POST', '/change-pin', {'email': TEST_EMAIL, 'currentPin': TEST_PIN, 'pin': ''})
    check("PIN empty rejected", 'error' in res)

    # PIN reset request without SMTP configured → 503 (or ok if configured)
    res = api('POST', '/pin-reset/request', {'email': TEST_EMAIL}, token=False)
    check("PIN reset request responds", res.get('ok') or 'error' in res, str(res))

    # Clean up test PIN
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM user_pins WHERE email = ?", ('stress-test@test.com',))
        conn.commit()
        conn.close()
    except:
        pass


# ═══════════════════════════════════════
# TEST SUITE 8: Stock Requests
# ═══════════════════════════════════════
def test_stock_requests():
    print("\n📋 TEST 8: Stock Requests")

    # GET stock requests
    res = api('GET', '/stock-requests')
    check("GET /stock-requests returns data", res.get('ok'))

    # Create stock request
    res = api('POST', '/stock-requests', {
        'action': 'create',
        'requestedBy': 'StressTest',
        'store': 'Alhambra',
        'model': 'iPhone 16 Pro',
        'items': [{'model': 'iPhone 16 Pro', 'qty': 2}],
        'qty': 2,
        'note': 'Stress test'
    })
    check("Create stock request", res.get('ok'), str(res))
    sr_id = res.get('data', {}).get('id', '')
    check("Stock request has ID", bool(sr_id), f"id={sr_id}")

    if sr_id:
        # Approve
        res = api('POST', '/stock-requests', {
            'action': 'approve', 'id': sr_id, 'by': 'Admin'
        })
        check("Approve stock request", res.get('ok'), str(res))

        # Fulfill
        res = api('POST', '/stock-requests', {
            'action': 'fulfill', 'id': sr_id, 'by': 'Admin'
        })
        check("Fulfill stock request", res.get('ok'), str(res))

        # Clean up
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("DELETE FROM stock_requests WHERE id = ?", (sr_id,))
            conn.commit()
            conn.close()
        except:
            pass

    # Invalid action
    res = api('POST', '/stock-requests', {'action': 'hack'})
    check("Invalid action rejected", 'error' in res)


# ═══════════════════════════════════════
# TEST SUITE 9: Delete Inventory Protection
# ═══════════════════════════════════════
def test_delete_protection():
    print("\n📋 TEST 9: Delete Inventory Protection")

    # Try deleting non-existent
    res = api('DELETE', '/inventory/000000000000000')
    check("Delete non-existent returns 404", res.get('error') == 'Record not found')

    # Find a sold phone with active sale (if any)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT i.imei FROM inventory i
            JOIN sales s ON i.imei = s.imei
            WHERE i.status = 'sold' AND s.status = 'completed'
            LIMIT 1
        """).fetchone()
        if row:
            res = api('DELETE', '/inventory/' + row['imei'])
            check("Delete sold phone with active sale blocked",
                  'error' in res and 'active sale' in res.get('error', ''), str(res))
        else:
            warn("No sold phone with active sale to test delete protection")
        conn.close()
    except Exception as e:
        warn("Delete protection test error", str(e))


# ═══════════════════════════════════════
# TEST SUITE 10: Concurrent API Stress Test
# ═══════════════════════════════════════
def test_concurrent():
    print("\n📋 TEST 10: Concurrent Stress Test (20 threads × 10 requests)")

    results = {'success': 0, 'fail': 0, 'errors': []}
    lock = threading.Lock()

    def worker(thread_id):
        for i in range(10):
            try:
                res = api('GET', '/health')
                with lock:
                    if res and res.get('status') in ('ok', 'degraded'):
                        results['success'] += 1
                    else:
                        results['fail'] += 1
            except Exception as e:
                with lock:
                    results['fail'] += 1
                    results['errors'].append(str(e))

    threads = []
    start = time.time()
    for i in range(20):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - start

    total = results['success'] + results['fail']
    check(f"Concurrent health checks ({total} requests)", results['fail'] == 0,
          f"success={results['success']} fail={results['fail']} in {elapsed:.2f}s")

    rps = total / elapsed if elapsed > 0 else 0
    print(f"  📊 Throughput: {rps:.0f} req/s")


# ═══════════════════════════════════════
# TEST SUITE 11: Concurrent Sales Race Condition
# ═══════════════════════════════════════
def test_concurrent_sales():
    print("\n📋 TEST 11: Double-Sale Race Condition Test")

    # Find an available phone
    res = api('GET', '/inventory?status=available')
    phones = res.get('inventory', [])
    if not phones:
        warn("No available phones to test double-sale")
        return

    # Pick a phone we can safely test with
    test_phone = phones[0]
    test_imei = test_phone['imei']

    results = {'success': 0, 'duplicate': 0, 'errors': []}
    lock = threading.Lock()

    def try_sale(thread_id):
        sale_id = f'RACE-{thread_id}-' + ''.join(random.choices(string.ascii_uppercase, k=4))
        res = api('POST', '/sale', {
            'id': sale_id, 'imei': test_imei, 'phoneName': test_phone.get('model', 'Test'),
            'store': test_phone.get('store', 'Alhambra'), 'storeKey': 'alhambra',
            'total': 100, 'price': 100, 'customer': 'RaceTest', 'paymentMethods': ['Cash']
        })
        with lock:
            if res.get('ok'):
                results['success'] += 1
            elif res.get('duplicate'):
                results['duplicate'] += 1
            else:
                results['errors'].append(str(res))

    # Launch 5 threads all trying to sell the same phone
    threads = []
    for i in range(5):
        t = threading.Thread(target=try_sale, args=(i,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    check("Only 1 sale succeeds", results['success'] == 1,
          f"success={results['success']} duplicate={results['duplicate']} errors={results['errors']}")
    check("Others get duplicate error", results['duplicate'] == 4,
          f"duplicate={results['duplicate']}")

    # Clean up: revert the sale
    try:
        conn = sqlite3.connect(DB_PATH)
        # Delete test sales
        conn.execute("DELETE FROM sales WHERE imei = ? AND customer = 'RaceTest'", (test_imei,))
        # Restore phone status
        conn.execute("UPDATE inventory SET status = 'available' WHERE imei = ?", (test_imei,))
        conn.commit()
        conn.close()
        print("  🧹 Test sale cleaned up, phone restored to available")
    except Exception as e:
        warn("Cleanup failed", str(e))


# ═══════════════════════════════════════
# TEST SUITE 12: Data Consistency Check
# ═══════════════════════════════════════
def test_data_consistency():
    print("\n📋 TEST 12: Data Consistency Check")

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # 1. All completed sales should have sold inventory
        rows = conn.execute("""
            SELECT s.id, s.imei, i.status as inv_status FROM sales s
            LEFT JOIN inventory i ON s.imei = i.imei
            WHERE s.status = 'completed'
        """).fetchall()

        mismatched = [r for r in rows if r['inv_status'] and r['inv_status'] != 'sold']
        orphaned = [r for r in rows if r['inv_status'] is None]
        check("Completed sales → inventory status = sold",
              len(mismatched) == 0,
              f"{len(mismatched)} mismatched: " + str([(r['id'], r['inv_status']) for r in mismatched[:3]]) if mismatched else "")
        if orphaned:
            warn(f"{len(orphaned)} completed sales have no inventory record (possibly deleted)")

        # 2. All returned sales should have available inventory
        rows = conn.execute("""
            SELECT s.id, s.imei, i.status as inv_status FROM sales s
            LEFT JOIN inventory i ON s.imei = i.imei
            WHERE s.status = 'returned' AND i.status IS NOT NULL
        """).fetchall()

        not_available = [r for r in rows if r['inv_status'] != 'available']
        check("Returned sales → inventory status = available",
              len(not_available) == 0,
              f"{len(not_available)} not available: " + str([(r['id'], r['inv_status']) for r in not_available[:3]]) if not_available else "")

        # 3. No duplicate IMEIs in inventory
        rows = conn.execute("""
            SELECT imei, COUNT(*) as cnt FROM inventory
            WHERE imei != '' GROUP BY imei HAVING COUNT(*) > 1
        """).fetchall()
        check("No duplicate IMEIs in inventory", len(rows) == 0,
              f"{len(rows)} duplicates" if rows else "")

        # 4. All IMEIs should be 15 digits
        rows = conn.execute("""
            SELECT COUNT(*) as c FROM inventory
            WHERE imei != '' AND (length(imei) != 15 OR imei GLOB '*[^0-9]*')
        """).fetchone()
        check("All IMEIs are 15 digits", rows['c'] == 0,
              f"{rows['c']} invalid IMEIs")

        # 5. All stores should be canonical names
        valid_stores = {
            'Alhambra', 'Monterey Park', 'San Gabriel', 'Rowland Heights',
            'Arcadia 1', 'Arcadia 2', 'Irvine', 'Rancho Cucamonga',
            'Las Vegas', 'HQ 总仓', ''
        }
        rows = conn.execute("SELECT DISTINCT store FROM inventory WHERE store IS NOT NULL").fetchall()
        non_standard = [r['store'] for r in rows if r['store'] not in valid_stores]
        check("All store names are canonical",
              len(non_standard) == 0,
              f"non-standard: {non_standard}" if non_standard else "")

        # 6. All regions should be valid
        valid_regions = {'us', 'hk', 'cn', 'jp', 'kr', ''}
        rows = conn.execute("SELECT DISTINCT region FROM inventory WHERE region IS NOT NULL").fetchall()
        invalid = [r['region'] for r in rows if r['region'] not in valid_regions]
        check("All regions are valid",
              len(invalid) == 0,
              f"invalid: {invalid}" if invalid else "")

        # 7. Sold-imeis API matches database
        api_res = api('GET', '/sold-imeis')
        api_imeis = set(api_res.get('imeis', []))
        db_rows = conn.execute("SELECT imei FROM sales WHERE status = 'completed'").fetchall()
        db_imeis = set(r['imei'] for r in db_rows)
        check("sold-imeis API matches DB",
              api_imeis == db_imeis,
              f"API has {len(api_imeis)}, DB has {len(db_imeis)}, diff={api_imeis.symmetric_difference(db_imeis)}" if api_imeis != db_imeis else "")

        conn.close()
    except Exception as e:
        check("Data consistency check", False, str(e))


# ═══════════════════════════════════════
# TEST SUITE 13: phones.js Sync Check
# ═══════════════════════════════════════
def test_phones_js():
    print("\n📋 TEST 13: /api/phones Data Sync (replaces public phones.js)")

    try:
        res = api('GET', '/phones')
        phones = res.get('phones')
        check("/api/phones returns list", isinstance(phones, list), str(res)[:120])
        if not isinstance(phones, list):
            return

        # Compare with DB
        conn = sqlite3.connect(DB_PATH)
        db_total = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        conn.close()

        diff = abs(db_total - len(phones))
        check(f"/api/phones sync (API={len(phones)}, DB={db_total})",
              diff <= 2,
              f"difference of {diff} records")

        # Check structure of first phone (build_phones output shape)
        if phones:
            p = phones[0]
            required_fields = ['imei', 'name', 'store', 'status', 'colorHex', 'cond']
            for field in required_fields:
                check(f"/api/phones has '{field}' field", field in p)
    except Exception as e:
        check("/api/phones parse", False, str(e))


# ═══════════════════════════════════════
# TEST SUITE 14: Edge Cases & Error Handling
# ═══════════════════════════════════════
def test_edge_cases():
    print("\n📋 TEST 14: Edge Cases & Error Handling")

    # 404 on unknown paths
    res = api('GET', '/nonexistent')
    check("Unknown GET path returns 404", res.get('error') == 'Not found')

    res = api('POST', '/nonexistent', {})
    check("Unknown POST path returns 404", res.get('error') == 'Not found')

    res = api('PUT', '/nonexistent/123', {})
    check("Unknown PUT path returns 404", res.get('error') == 'Not found')

    res = api('DELETE', '/nonexistent/123')
    check("Unknown DELETE path returns 404", res.get('error') == 'Not found')

    # Empty body
    res = api('POST', '/sale', None)
    check("POST /sale with empty body handled", 'error' in res)

    # Double return
    res = api('PUT', '/sale/NONEXISTENT', {'status': 'returned'})
    check("Return non-existent sale handled", 'error' in res)


# ═══════════════════════════════════════
# MAIN
# ═══════════════════════════════════════
if __name__ == '__main__':
    print("=" * 60)
    print("🔬 PhoneInventory 全面压力测试 + 功能验证")
    print("=" * 60)
    print(f"API: {API_BASE}")
    print(f"DB: {DB_PATH}")

    # Check API is reachable
    try:
        res = api('GET', '/health', token=False)
        if not res or 'status' not in res:
            print("\n❌ API server not reachable! Start it first:")
            print(f"   python3 {DEPLOY_DIR}/scripts/api_server.py")
            sys.exit(1)
    except:
        print("\n❌ API server not reachable! Start it first:")
        print(f"   python3 {DEPLOY_DIR}/scripts/api_server.py")
        sys.exit(1)

    # Obtain auth token (all endpoints require it now; module-level assignment)
    res = api('POST', '/login', {'email': TEST_EMAIL, 'pin': TEST_PIN}, token=False)
    if not res.get('token'):
        print(f"\n❌ Login failed for {TEST_EMAIL} — set TEST_EMAIL/TEST_PIN env vars. Got: {res}")
        sys.exit(1)
    AUTH_TOKEN = res['token']
    print(f"🔑 Authenticated as {TEST_EMAIL}")

    # Run all tests
    test_health()
    test_inventory()
    test_inventory_update()
    test_sales()
    test_transfers()
    test_transfer_state_machine()
    test_pins()
    test_stock_requests()
    test_delete_protection()
    test_concurrent()
    test_concurrent_sales()
    test_data_consistency()
    test_phones_js()
    test_edge_cases()

    # Summary
    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"📊 结果: {PASS}/{total} 通过 | {FAIL} 失败 | {WARN} 警告")
    if ERRORS:
        print(f"\n❌ 失败项目:")
        for err in ERRORS:
            print(err)
    else:
        print("\n✅ 所有测试通过!")
    print("=" * 60)
    sys.exit(1 if FAIL > 0 else 0)
