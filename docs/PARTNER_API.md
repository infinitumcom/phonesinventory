# PhonesInventory — Partner Inventory API / 对外库存 API 对接文档

> **v1** · Read-only inventory feed for approved trade partners.
> 只读库存数据接口，供已审核通过的同行合作方拉取我们的在售库存。

---

## 1. Overview / 概览

This API lets an approved partner pull our **available inventory** at **wholesale (dealer) prices**, so you can list, price-match, or source stock programmatically.

本接口让已通过审核的友商以**同行价（批发价）**拉取我们的**在售库存**，用于程序化上架、比价或调货采购。

- **Read-only / 只读** — you can query stock; you cannot modify anything. 只能查询，无法写入或修改任何数据。
- **Our cost is never exposed / 绝不暴露我们的成本价** — you only see the dealer price. 你只能看到同行售价。
- **IMEI/serial is masked by default / IMEI/序列号默认掩码** — full IMEI is granted per key only when agreed. 完整 IMEI 仅在双方约定后按密钥单独开通。

---

## 2. Base URL / 接口地址

```
https://phonesinventory.com/api/v1
```

All endpoints are under this prefix. 所有端点均在此前缀下。

---

## 3. Authentication / 鉴权

Every request (except `/docs`) must include your API key in the request header:
除 `/docs` 外，每个请求都必须在请求头中带上你的 API 密钥：

```
X-Api-Key: pi_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

- Keys are issued by our platform admin and look like `pi_live_` + 40 hex chars.
  密钥由我方平台管理员发放，格式为 `pi_live_` 开头 + 40 位十六进制字符。
- **The full key is shown only once at issue time — store it securely.**
  **完整密钥仅在发放时显示一次，请妥善保存。**
- Treat the key like a password. If leaked, ask us to revoke and reissue.
  请像对待密码一样保管密钥。若泄露，请联系我们吊销并重新签发。

### Restrictions per key / 每把密钥的限制
| Control / 控制项 | Description / 说明 |
|---|---|
| **Scope / 权限范围** | Currently `inventory:read`. 目前为只读库存权限。 |
| **Daily quota / 每日配额** | Requests per day; exceeding returns HTTP `429`. 每日请求上限，超出返回 `429`。 |
| **IP allowlist / IP 白名单** | Optional. If set, requests from other IPs are rejected. 可选；一旦设置，其他 IP 的请求会被拒绝。 |
| **Full IMEI / 完整 IMEI** | Off by default (masked). 默认关闭（掩码）。 |

---

## 4. Endpoints / 端点

### 4.1 `GET /ping` — Auth test / 鉴权测试

Verify your key works. 用于验证密钥是否可用。

```bash
curl -H "X-Api-Key: pi_live_xxx" https://phonesinventory.com/api/v1/ping
```

**Response / 返回:**
```json
{ "ok": true, "partner": "Your Company Name", "scopes": "inventory:read" }
```

---

### 4.2 `GET /inventory` — Available stock (paged) / 在售库存（分页）

Returns individual available units. 返回逐台在售库存。

**Query parameters / 查询参数（全部可选）:**

| Param / 参数 | Type / 类型 | Notes / 说明 |
|---|---|---|
| `category` | string | `phone` / `ipad` / `computer` / `watch` / `other` |
| `brand` | string | e.g. `Apple`, `Samsung`（不区分大小写） |
| `region` | string | `us` / `cn` (国行) / `hk`（不区分大小写） |
| `condition` | string | `new` / `used`（不区分大小写） |
| `model` | string | Fuzzy match / 模糊匹配，如 `iPhone 15` |
| `store` | string | Filter by store — **only if your key has store access**. 按门店筛选，**仅当你的密钥开通门店可见时生效**。 |
| `page` | int | Default `1` / 默认 1 |
| `limit` | int | Default `100`, max `500` / 默认 100，最大 500 |

```bash
curl -H "X-Api-Key: pi_live_xxx" \
  "https://phonesinventory.com/api/v1/inventory?category=phone&brand=Apple&condition=new&page=1&limit=100"
```

**Response / 返回:**
```json
{
  "total": 342,
  "page": 1,
  "limit": 100,
  "items": [
    {
      "id": 1521,
      "brand": "Apple",
      "model": "iPhone 15 Pro Max",
      "storage": "256GB",
      "color": "Natural Titanium",
      "condition": "new",
      "batteryHealth": "",
      "region": "us",
      "category": "phone",
      "price": 985,
      "imei": "356789****1234",
      "store": "Las Vegas"
    }
  ]
}
```

**Field notes / 字段说明:**
- `price` — dealer/wholesale price in USD. 同行价（美元）。
- `imei` — masked (`prefix****last4`) unless your key has full-IMEI access. 掩码显示，除非你的密钥已开通完整 IMEI。
- `store` — **only present if your key has store access.** Tells you which store holds the unit. 仅当你的密钥开通门店可见时返回，标明这台机器在哪个门店。
- `total` — total matching units (for pagination). 匹配总数，用于翻页。

---

### 4.3 `GET /inventory/summary` — Quantities by model / 按型号汇总数量

Grouped counts + lowest price per group — good for a quick availability overview.
按型号/容量/成色/地区分组的数量与最低价，用于快速掌握可供情况。

```bash
curl -H "X-Api-Key: pi_live_xxx" \
  https://phonesinventory.com/api/v1/inventory/summary
```

**Response / 返回:**
```json
{
  "totalAvailable": 1240,
  "groups": [
    {
      "category": "phone",
      "brand": "Apple",
      "model": "iPhone 15 Pro Max",
      "storage": "256GB",
      "condition": "new",
      "region": "us",
      "qty": 12,
      "min_price": 985,
      "store": "Las Vegas"
    }
  ]
}
```

> If your key has **store access**, `summary` is broken down **per store** (each group also carries a `store` field), so you can see exactly what each store is holding. 若你的密钥开通**门店可见**，`summary` 会**按门店拆分**（每组附带 `store` 字段），可直接看到每个门店各有什么库存。

---

### 4.4 `GET /docs` — Machine-readable docs (public) / 机读文档（公开）

Returns this API's structure as JSON. **No key required** — useful for self-onboarding.
以 JSON 返回接口结构，**无需密钥**，方便自助对接。

```bash
curl https://phonesinventory.com/api/v1/docs
```

---

## 5. Errors / 错误码

Errors return a JSON body `{ "error": "..." }` with an HTTP status code:
错误以 JSON `{ "error": "..." }` 返回，并附对应 HTTP 状态码：

| Status / 状态码 | Meaning / 含义 |
|---|---|
| `401` | Missing or invalid/revoked key. 缺少密钥，或密钥无效/已吊销。 |
| `403` | Insufficient scope, or IP not on allowlist. 权限不足，或 IP 不在白名单。 |
| `429` | Daily quota exceeded. 超出每日配额。 |
| `500` | Server error — retry later or contact us. 服务器错误，请稍后重试或联系我们。 |

---

## 6. Usage guidance / 使用建议

- **Cache results.** Sync on a schedule (e.g. every 15–30 min), not on every page view — respect your daily quota.
  **请缓存结果。** 建议定时同步（如每 15–30 分钟一次），不要每次页面访问都请求，以免耗尽每日配额。
- **Prefer `/summary` for browsing**, `/inventory` for detail/purchase.
  **浏览可供量用 `/summary`**，需要逐台明细/下单时用 `/inventory`。
- **Availability changes constantly.** A unit shown available may sell before you order — always re-confirm at order time.
  **库存实时变动。** 显示在售的机器可能在你下单前已售出，下单前请再次确认。
- Settlement is **offline / 线下结算**. This API is data-only; ordering & payment are handled through our agreed channel. 本接口仅提供数据，下单与结算走双方约定渠道。

---

## 7. Getting a key / 如何获取密钥

Contact us to be approved as a partner. Once approved, we issue your `pi_live_...` key with your agreed daily quota, optional IP allowlist, and (if agreed) full-IMEI access.

请联系我们完成合作方审核。通过后，我们会为你签发 `pi_live_...` 密钥，并按约定设置每日配额、可选 IP 白名单，以及（如约定）完整 IMEI 权限。

**Contact / 联系:** anderson@ifixforu.com

---

*PhonesInventory Partner API · v1 · Last updated 2026-08-05*
