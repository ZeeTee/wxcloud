# AI 大模型行业日报 · 归档服务

微信公众号文章正文里的**外链在手机端不可点击**(微信会剥离非白名单域名的 `href`),
唯一能跳出去的位置是文章底部的「阅读原文」。本服务就是那个落地页:
按天永久归档每日《AI 大模型行业日报》,读者点「阅读原文」进来后,
**可以点到每一条新闻的原文链接**。

基于微信云托管官方 Django 模板二开。设计文档见 `ai-news-daily` 仓库的
`docs/read-more-platform.md`。

---

## 1. 它只做四件事

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/report` | **唯一写接口**。密钥 + IP 白名单 + 体积 + 内容校验 |
| `GET` | `/daily/<YYYY-MM-DD>` | 当天日报,**原样返回**入库的 HTML |
| `GET` | `/healthz` | 存活探针,返回 `200 ok` |
| `GET` | `/` | 服务说明页(静态,不含数据库内容) |

**没有列表页、没有分页、没有搜索、没有 RSS。** 阅读原文只指向单篇,
详情页又不放返回链接,列表页没有任何入口 —— 做了也只是个没人能到达的页面。
要查往期,直接构造日期 URL(`/daily/2026-09-20`)即可。

### 读路径是零加工的

```python
def daily(request, date):
    report = Report.objects.filter(pk=date).first()
    if report is None:
        return render(request, "not_found.html", {"date": date}, status=404)
    return HttpResponse(report.html, content_type="text/html; charset=utf-8")
```

没有模板拼装、没有 HTML 解析、没有字符串改写。**库里存的就是浏览器拿到的**,
与公众号草稿正文逐字节相同。好处是排障极简单:网页上显示不对,
那一定是渲染器的问题,跟本服务无关。

也因此,写接口的鉴权是**唯一防线** —— 一旦它能被匿名调用,攻击者就能往这个
域名下塞任意页面(伪造微信登录页那种)。详见第 4 节。

---

## 2. 部署

### 2.1 必填环境变量

在云托管控制台「服务设置 → 环境变量」里配置:

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `MYSQL_ADDRESS` / `MYSQL_USERNAME` / `MYSQL_PASSWORD` | ✅ | 云托管内 MySQL 自动注入 |
| `REPORT_API_KEY` | ✅ | 写接口密钥。**未配置时写接口返回 503 而不是放行** |
| `REPORT_ALLOWED_IPS` | ✅ | 允许写入的来源 IP,逗号分隔,支持 CIDR。**未配置同样 503** |
| `REPORT_PUBLIC_BASE_URL` | ⭐ 强烈建议 | 形如 `https://xxx.ap-shanghai.run.tcloudbase.com` |
| `REPORT_IP_HEADERS` | 可选 | 只信某个转发头(见 4.3) |
| `DJANGO_DEBUG` | 可选 | 默认 `0`。**不要设成 1** |
| `DJANGO_SECRET_KEY` | 可选 | 不配则每次启动随机生成(本服务不用 session/cookie,无需跨重启稳定) |
| `DJANGO_ALLOWED_HOSTS` | 可选 | 默认 `*` |
| `REPORT_MAX_BODY_BYTES` | 可选 | 默认 `262144`(256KB) |
| `REPORT_RATE_LIMIT` | 可选 | 默认 `30`(每来源 IP 每分钟) |

> `REPORT_API_KEY` 与 `REPORT_ALLOWED_IPS` 都是**必需**的,这是刻意的 fail-closed:
> 配置缺失时宁可写不进去,也不能放任意人写进来。忘了配的表现是上传一直返回 503,
> 错误码 `api_key_not_configured` / `allowlist_not_configured`,很容易定位。

⚠️ **`REPORT_PUBLIC_BASE_URL` 一定要配**:写接口返回的 `url` 会被写进公众号草稿的
「阅读原文」,而网关转发过来的 `Host` 头不一定是公开域名 —— 靠它拼出来的可能是死链。

### 2.2 建表

**不需要手工建表。** 容器启动时会自动执行
`python manage.py migrate --noinput --fake-initial`。

`container.config.json` 里的 `executeSQLs` 只负责保证库存在,**刻意不在那里写
`CREATE TABLE`** —— 那样 Django 迁移会因为「表已存在」而失败,直接把容器启动打断。

### 2.3 端口

容器监听 `80`,必须与控制台「服务设置」里的端口一致,否则部署失败。

---

## 3. 接口契约

统一响应信封(与工具站保持一致):

```jsonc
// 成功
{ "ok": true,  "data": { ... } }
// 失败
{ "ok": false, "error": { "code": "invalid_body", "message": "…" } }
```

### `POST /api/report`

请求头:

```
X-Api-Key: <REPORT_API_KEY>
Content-Type: application/json
```

请求体:

```jsonc
{
  "date":    "2026-09-22",          // 必填,严格 YYYY-MM-DD
  "html":    "<!DOCTYPE html>…",    // 必填,完整 HTML 文档
  "title":   "AI大模型行业日报 · 2026-09-22",   // 可选,元数据
  "summary": "今日主旋律是…"          // 可选,元数据
}
```

`title` / `summary` 是**纯元数据,页面不用**(没有列表页)。留着只是排障时
`SELECT report_date, title FROM reports` 能一眼看出哪条是哪天的。

响应:

```jsonc
{
  "ok": true,
  "data": {
    "date": "2026-09-22",
    "url":  "https://<域名>/daily/2026-09-22",
    "created": true,        // true=新建,false=覆盖更新
    "bytes": 23338,
    "unchanged": false      // 内容与库中一致时为 true(跳过写)
  }
}
```

**幂等**:同一天重复上传 = 更新同一条(`report_date` 是主键),
内容完全一致时直接返回 `unchanged: true` 且不写库。

#### 状态码

| 状态 | code | 场景 |
| --- | --- | --- |
| 200 | — | 覆盖更新 / 内容未变化 |
| 201 | — | 新建 |
| 400 | `invalid_body` | 请求体不是 UTF-8 的 JSON 对象 |
| 400 | `invalid_date` | 日期缺失、格式不对或不是真实日期 |
| 401 | `unauthorized` | 密钥不对 |
| 403 | `forbidden_ip` | 来源 IP 不在白名单 |
| 405 | `method_not_allowed` | 用了 GET 等 |
| 413 | `payload_too_large` | 超过体积上限 |
| 422 | `content_rejected` | 内容校验未通过(**message 里带具体原因**) |
| 429 | `rate_limited` | 触发限流 |
| 503 | `api_key_not_configured` | 服务端没配密钥(**fail-closed**) |
| 503 | `allowlist_not_configured` | 服务端没配 IP 白名单(**fail-closed**) |

#### 调用示例

```bash
curl -X POST https://<域名>/api/report \
  -H "X-Api-Key: $REPORT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d "$(python3 - <<'PY'
import json, pathlib
print(json.dumps({
    "date": "2026-09-22",
    "html": pathlib.Path("output/2026-09-22/wechat.html").read_text(encoding="utf-8"),
    "title": "AI大模型行业日报 · 2026-09-22",
}))
PY
)"
```

### `GET /daily/<YYYY-MM-DD>`

原样返回入库的 HTML。响应头:

```
Content-Type: text/html; charset=utf-8
Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
X-Frame-Options: DENY
ETag: "<sha256>"
Cache-Control: public, max-age=300
```

支持 `If-None-Match` → `304`。日期不存在或路径不合法 → 友好的 404 页面。

---

## 4. 安全设计

### 4.1 威胁

这是一个**面向公网、且会把 HTML 原样返回给浏览器**的服务。如果写接口公开且无鉴权:

> 任何人都能 POST 一段 HTML,存到 `https://<域名>/daily/2026-01-01`,
> 然后拿这个链接去钓鱼 —— 域名是**腾讯云的、看起来可信的**,页面可以伪装成"微信登录"。

所以下面这些**全部必需**,不是"加固":

| 措施 | 实现位置 |
| --- | --- |
| 共享密钥,恒定时间比较 | `security.key_matches` |
| IP 白名单(支持 CIDR) | `security.ip_allowed` |
| 只收 JSON,不收文件/不接受任意路径 | `views.report_api` |
| 体积上限 256KB | `REPORT_MAX_BODY_BYTES` |
| 日期严格 `YYYY-MM-DD` 且必须是真实日期 | `security.DATE_RE` + `date.fromisoformat` |
| 内容审计(见 4.4) | `security.audit_html` |
| 响应安全头(CSP 等) | `middleware.SecurityHeadersMiddleware` |
| 限流 | `security.RateLimiter` |

检查顺序是刻意的:**先 IP 白名单,再密钥**。白名单能挡掉绝大多数扫描流量,
让密钥校验只面对可信来源,减少爆破面。

### 4.2 模板里被改掉的三处危险默认值

| 项 | 模板原值 | 现在 |
| --- | --- | --- |
| `DEBUG` | 硬编码 `True`(会向访问者泄露堆栈、配置、SQL) | 默认 `False`,只能由环境变量打开 |
| `SECRET_KEY` | 硬编码,而**本仓库是公开的** | 从 `DJANGO_SECRET_KEY` 读,缺省随机生成 |
| `ALLOWED_HOSTS` | `['*']` | `DJANGO_ALLOWED_HOSTS`,缺省 `*` |

不要改回去。`SECRET_KEY` 尤其:它等于公开在互联网上。

### 4.3 ⚠️ 云托管下的"真实客户端 IP"

**云托管网关会把 `REMOTE_ADDR` 换成网关出口 IP,不是真实客户端 IP。**
腾讯云文档还明确指出 `X-Forwarded-For` / `X-Real-IP` **可能拿不到**真实 IP,
建议改用 `X-Original-Forwarded-For`
([CloudBase 文档](https://docs.cloudbase.net/run/faq/gw))。

因此实现是:

1. 依次检查 `X-Original-Forwarded-For` → `X-Forwarded-For` → `X-Real-IP` → `X-WX-Client-IP`,
   再补上 `REMOTE_ADDR`;
2. `X-Forwarded-For` **只取最右一段** —— 它是最近一跳代理写入的;左边的段由客户端提供,
   可以随意伪造;
3. **任一候选 IP 命中白名单即通过**;
4. **每次写请求都把候选 IP 与各转发头的原始值记进日志**。

第 3 条用"任一"而不是"全部",是因为无法在本地确定平台究竟把真实 IP 放在哪个头里;
要求全部匹配会让正常上传必然失败。**这也意味着 IP 白名单是纵深防御,不是唯一防线
—— 共享密钥才是主防线。两者都必需。**

> **部署后请做一次验证**:手动触发一次上传,然后在云托管控制台看服务日志,找到
> `POST /api/report candidates=[...] observed={...}` 那一行。`observed` 会列出
> 平台实际发了哪些头、值是什么。确认之后,可以把 `REPORT_IP_HEADERS` 设成
> 真正生效的那一个(例如 `REPORT_IP_HEADERS=x-original-forwarded-for`),
> 让判断不再依赖"猜"。

### 4.4 内容审计(`security.audit_html`)

粗筛 + 结构化检查两道:

- **粗筛**:出现 `<script` / `<iframe` / `<object` / `<embed` / `javascript:` /
  `vbscript:` / `data:text/html` 即拒。
- **结构化**:用 `html.parser` 逐标签检查
  - 禁用标签(上述之外还有 `<form>` / `<base>` / `<link>` / `<svg>` / `<math>` 等)
  - 任何 `on*` 事件属性
  - 会发起网络请求的属性(`href` / `src` / …)的**协议白名单**(只允许 http / https / mailto / 相对路径)
  - `<meta http-equiv=refresh>`
  - CSS `expression(`

**为什么不用裸正则查 `on\w+=`**:正文文本里完全可能出现 `only =` 这类字样,
裸正则会把正常内容误判成事件属性。按**标签属性**解析才能区分 ——
测试里有专门一条 `test_不误杀正文里的_only_字样` 守着这个边界。

**必须是以 `<!DOCTYPE html>` 开头的完整文档** —— 这是"入库的是完整文档"这一契约的机器校验。

> 回归保障:`wxcloudrun/tests/fixtures/daily-2026-09-22.html` 是**真实产物**,
> `test_真实日报产物必须通过` 断言它不会被审计误杀。

### 4.5 `check --deploy` 的 4 条告警是**有意保留**的

跑 `python manage.py check --deploy` 会看到 4 条 warning,都不是疏漏:

| 告警 | 为什么故意这样 |
| --- | --- |
| `W003` 没有 CsrfViewMiddleware | 本服务没有会话、没有 cookie,写接口用自定义头传密钥 —— 不存在 CSRF 面。**加上它反而会拦掉不带 CSRF 令牌的 POST,把写接口打死** |
| `W005` 没开 HSTS includeSubDomains | 这是腾讯云域名,开大了影响面不受我们控制,收益很小 |
| `W008` 没开 SSL 重定向 | HTTPS 由网关终结,容器内看到的永远不是"原始 https";在这里做跳转只会死循环,还会把平台健康检查打成 301 |
| `W021` 没开 HSTS preload | 同上,preload 是不可逆的,不该由我们替整个域名决定 |

---

## 5. 本地开发与测试

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 跑测试(用内存 SQLite,不需要 MySQL —— 本机没有 MySQL 也能跑)
DJANGO_SETTINGS_MODULE=wxcloudrun.settings_test .venv/bin/python manage.py test wxcloudrun

# 部署自检
DJANGO_SETTINGS_MODULE=wxcloudrun.settings .venv/bin/python manage.py check --deploy
```

测试覆盖 107 项,包含全部拒绝路径(鉴权矩阵、体积、日期、内容审计、限流)、
读路径的**字节一致性**断言、以及真实产物的误杀回归。

有 Docker 时可以直接构建镜像验证:

```bash
docker build -t wxcloudrun .
docker run --rm -p 8080:80 -e REPORT_API_KEY=k -e REPORT_ALLOWED_IPS=0.0.0.0/0 wxcloudrun
```

> ⚠️ **本地没有 Docker 时无法验证镜像构建**。改动基础镜像或 `requirements.txt`
> 后,请留意云托管控制台的构建日志。

---

## 6. 目录结构

```
.
├── Dockerfile                  镜像定义(见文件内注释的 5 处改动及理由)
├── container.config.json       模板部署的「服务设置」初始值(二开可忽略)
├── manage.py
├── requirements.txt            Django 5.2 LTS / PyMySQL / gunicorn
└── wxcloudrun
    ├── settings.py             配置。⚠️ 三处安全默认值的改动别改回去
    ├── settings_test.py        测试专用:数据库换成内存 SQLite
    ├── security.py             全部安全逻辑(纯函数,可独立单测)
    ├── middleware.py           响应安全头 / CSP
    ├── models.py               Report 模型(MediumTextField)
    ├── views.py                四个端点
    ├── urls.py                 路由
    ├── migrations/             0001_initial 建 reports 表
    ├── templates/              index.html(说明页) / not_found.html
    └── tests/
        ├── fixtures/daily-2026-09-22.html   真实产物,用于防误杀回归
        ├── test_security.py
        ├── test_report_api.py
        └── test_daily.py
```

## 7. 与上游模板的差异速查

| 文件 | 改动 |
| --- | --- |
| `requirements.txt` | Django 3.2.8 → **5.2 LTS**;去掉 `powerline-status`(模板残留)、`pytz`;加 `gunicorn` |
| `Dockerfile` | 基础镜像 `alpine:3.13` → `python:3.12-alpine`;装 `tzdata`;分离依赖层;启动前 `migrate`;`runserver` → **gunicorn** |
| `settings.py` | `DEBUG` / `SECRET_KEY` / `ALLOWED_HOSTS` 改为环境变量;精简 `INSTALLED_APPS` 与 `MIDDLEWARE`;日志只走控制台 |
| `models.py` | `Counters` → `Report` |
| `views.py` | 计数器 demo → 四个端点 |
| `urls.py` | `django.conf.urls.url`(Django 4.0 已移除)→ `re_path`;新增路由与 `handler404` |
| `container.config.json` | `maxNum` 5 → 1;`executeSQLs` 不再建表 |

## License

[MIT](./LICENSE)
