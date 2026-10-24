"""公司情报检索 v2：多源宽抓取（0 token）→ AI 只综合一次
- 搜索引擎：Bing / 百度 / 360 三引擎并行（中小厂覆盖率大幅提升）
- 垂直源定向：职友集/看准/Boss直聘/拉勾/猎聘/智联/企查查/爱企查/脉脉（site: 语法）
- 官网候选：用户提供 + 域名推断 + 搜索结果挑官网
返回结构保持不变（sources / raw_text / website_found），抓取失败不调 AI。"""
import re

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

VERTICAL_SITES = [
    ("jobui", "职友集(公司点评/招聘)", "jobui.com"),
    ("kanzhun", "看准网(点评/招聘)", "kanzhun.com"),
    ("zhipin", "Boss直聘(招聘)", "zhipin.com"),
    ("lagou", "拉勾(招聘)", "lagou.com"),
    ("liepin", "猎聘(招聘)", "liepin.com"),
    ("zhaopin", "智联(招聘)", "zhaopin.com"),
    ("qcc", "企查查(工商)", "qcc.com"),
    ("aiqicha", "爱企查(工商)", "aiqicha.baidu.com"),
    ("maimai", "脉脉(口碑)", "maimai.cn"),
]

# 词典/百科/无关站点噪音（Bing 中文分词差，常返回单字词典结果）
NOISE_HOSTS = [
    "baike", "wiktionary", "hanyuguoxue", "hgcha", "zidian", "zdic",
    "dictionary", "cidian", "chazidian", "youdao", "iciba", "gushici",
    "hancibao", "hanziguoxue", "revised.moe", "sogou", "so.com",
    "fanqienovel", "fanqie", "jinritoutiao", "ixigua",
]


def _extract_text(html):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s{2,}", " ", text)


def fetch_url(url, timeout=8, max_chars=6000):
    """抓取单个 URL 正文，失败返回 None"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            return None
        text = _extract_text(r.text)
        if len(text) < 40:
            return None
        return text[:max_chars]
    except Exception:
        return None


def _items_to_list(soup, sel_title, sel_snippet):
    items = []
    for li in soup.select(sel_title):
        a = li
        title = a.get_text(" ", strip=True)
        href = a.get("href", "")
        parent = a.find_parent("li") or a.find_parent("div")
        p = None
        if parent:
            p = parent.select_one(sel_snippet) if sel_snippet else None
        snippet = p.get_text(" ", strip=True) if p else ""
        if title and href:
            items.append({"title": title[:150], "snippet": snippet[:300], "url": href})
    return items


def _search_bing(query, limit=6):
    try:
        r = requests.get("https://www.bing.com/search",
                         params={"q": query, "setlang": "zh-CN"},
                         headers=HEADERS, timeout=7)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        items = []
        for li in soup.select("li.b_algo")[:limit]:
            a = li.select_one("h2 a")
            if not a:
                continue
            p = li.select_one(".b_caption p") or li.select_one("p")
            items.append({
                "title": a.get_text(" ", strip=True)[:150],
                "snippet": (p.get_text(" ", strip=True)[:300] if p else ""),
                "url": a.get("href", ""),
            })
        return items
    except Exception:
        return []


def _search_baidu(query, limit=6):
    try:
        r = requests.get("https://www.baidu.com/s",
                         params={"wd": query, "ie": "utf-8"},
                         headers=HEADERS, timeout=7)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        return _items_to_list(soup, "h3 a", "div.c-abstract")[:limit]
    except Exception:
        return []


def _search_360(query, limit=6):
    try:
        r = requests.get("https://www.so.com/s",
                         params={"q": query},
                         headers=HEADERS, timeout=7)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        items = []
        for li in soup.select("li.res-list")[:limit]:
            a = li.select_one("h3 a")
            if not a:
                continue
            p = li.select_one("p.res-desc")
            items.append({
                "title": a.get_text(" ", strip=True)[:150],
                "snippet": (p.get_text(" ", strip=True)[:300] if p else ""),
                "url": a.get("href", ""),
            })
        return items
    except Exception:
        return []


def _search_sogou(query, limit=6):
    """搜狗：中文公司检索质量最高的免费通道（实测任子行/中小厂全命中）"""
    for attempt in range(2):  # 搜狗偶发空返回，重试一次
        try:
            r = requests.get("https://www.sogou.com/web",
                             params={"query": query},
                             headers=HEADERS, timeout=8)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            items = []
            for h3 in soup.select("h3")[:limit]:
                a = h3.select_one("a")
                if not a:
                    continue
                title = a.get_text(" ", strip=True)
                href = a.get("href", "")
                if not title or not href:
                    continue
                parent = h3.find_parent("div")
                p = None
                if parent:
                    p = parent.select_one("p.str_info") or parent.select_one("p.text-layout")
                    if not p:
                        p = parent.select_one("p")
                snippet = p.get_text(" ", strip=True) if p else ""
                items.append({"title": title[:150], "snippet": snippet[:300], "url": href})
            if items:
                return items
            time.sleep(1)
        except Exception:
            continue
    return []


def _resolve(url, timeout=4):
    """搜狗 /link 跳转链接 → 最终真实 URL（用于官网推断）"""
    if "/link?" in url or url.startswith("/"):
        try:
            r = requests.get(url if url.startswith("http") else "https://www.sogou.com" + url,
                             headers=HEADERS, timeout=timeout, allow_redirects=True, stream=True)
            return r.url
        except Exception:
            return url
    return url


def _core_name(company_name):
    """提取公司核心名（搜狗对超长查询返回空；去掉后缀/行业词后更稳）
    任子行网络技术股份有限公司 → 任子行；北京某某科技有限公司 → 北京某某"""
    c = company_name.strip()
    c = re.sub(r"(网络技术|信息技术|科技|软件|数据|网络|信息|智能|电子|通信)(股份)?有限公司$", "", c)
    c = re.sub(r"股份有限公司$|有限公司$|有限责任公司$", "", c)
    c = re.sub(r"(网络技术|信息技术|科技|软件|数据|网络|信息|智能|电子|通信)", "", c)
    return c if len(c) >= 2 else company_name.strip()


def search_multi(query, timeout=9, limit=6):
    """多引擎并发搜索并去重聚合（搜狗优先，中文质量最高），0 token"""
    import concurrent.futures
    results = {}

    def _run(fn):
        try:
            return fn(query, limit=limit)
        except Exception:
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = {
            ex.submit(_run, _search_sogou): "sogou",
            ex.submit(_run, _search_bing): "bing",
            ex.submit(_run, _search_baidu): "baidu",
        }
        try:
            for f in concurrent.futures.as_completed(futs, timeout=timeout):
                eng = futs[f]
                for it in f.result():
                    results.setdefault(eng, []).append(it)
        except concurrent.futures.TimeoutError:
            pass
    flat = []
    seen = set()
    for eng in ("sogou", "bing", "baidu"):
        for it in results.get(eng, []):
            url = it["url"]
            if "/link?" in url:
                key = ("link", it["title"][:40])
            else:
                host = re.sub(r"^https?://", "", url).split("/")[0].lower()
                key = (host, it["title"][:40])
            if key in seen:
                continue
            seen.add(key)
            flat.append({**it, "engine": eng})
    return flat


def _is_noise(url, title=""):
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    if any(n in host for n in NOISE_HOSTS):
        return True
    # 词典/百科类标题特征（Bing 单字结果的标题通常是"X 字的意思/拼音"）
    t = title or ""
    if re.search(r"(的意思|的拼音|的笔顺|字怎么读|怎么读|汉字|字形|部首|汉语字典|国语辞典|写作助手|一键生成|豆包AI)", t):
        return True
    return False


# 在招职位 / 口碑评价 识别
_JOB_HOSTS = ("zhipin", "liepin", "lagou", "zhaopin", "51job", "jobui", "kanzhun", "mokahr", "shixi", "boss")
_JOB_STRONG = ("招聘", "岗位", "职位", "实习", "诚聘", "高薪", "校招", "社招", "猎头", "简历")
_JOB_KW = ("工程师", "运营", "设计师", "销售", "助理", "专员", "经理", "顾问", "职责", "待遇", "薪资")
_REP_KW = ("评价", "口碑", "怎么样", "靠谱", "待遇", "工资", "加班", "员工", "脉脉",
           "看准", "职友集", "避雷", "黑幕", "离职", "面试经验", "offer")


def _is_job_item(url, title="", snippet=""):
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    if any(h in host for h in _JOB_HOSTS):
        return True
    t = (title or "") + " " + (snippet or "")
    if any(w in t for w in _JOB_STRONG):
        return True
    hits = sum(1 for w in _JOB_KW if w in t)
    return hits >= 2


def _is_reputation_item(url, title="", snippet=""):
    t = (title or "") + " " + (snippet or "")
    return any(w in t for w in _REP_KW) and not _is_job_item(url, title, snippet)


def _find_official_domain(items):
    bad = ["zhaopin", "51job", "jobui", "kanzhun", "boss", "liepin", "lagou", "shixi",
           "mokahr", "app", "apps", "tianyancha", "qcc", "aiqicha", "企查查", "天眼查",
           "36kr", "ithome", "zhihu", "weibo", "bilibili", "douyin", "xiaohongshu",
           "sohu", "163.com", "sina", "qq.com", "baidu", "jianshu", "csdn",
           "toutiao", "uc.cn", "maimai", "jobui.com", "zhipin.com", "lagou.com",
           "fanqienovel", "fanqie", "jinritoutiao", "douyin.com", "ixigua",
           "baike", "wiktionary", "hanyuguoxue", "hgcha", "zidian", "zdic",
           "dictionary", "cidian", "chazidian", "youdao", "iciba", "gushici",
           "sogou"]
    resolved = 0
    for it in items:
        url = it.get("url", "") or ""
        if "/link?" in url:  # 搜狗跳转链接 → 跟随拿到真实域名（限次）
            if resolved >= 8:
                continue
            resolved += 1
            url = _resolve(url)
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        host = host.split(":")[0]
        if not host or len(host) < 6:
            continue
        if any(b in host for b in bad):
            continue
        if "bing" in host or "microsoft" in host or "google" in host or "sogou" in host:
            continue
        return "https://" + host
    return None


def collect(company_name, website_url="", extra_text=""):
    """收集公司情报（限时并发，多引擎+垂直源）：
    返回 {company, sources:[{source,content}], raw_text, website_found}"""
    import concurrent.futures
    sources = []
    raw_parts = []
    website_found = ""

    # ① 官网候选 URL（用户提供 + 常见域名推断）
    url_list = []
    if website_url:
        base = website_url.rstrip("/")
        for p in ["", "/about", "/about-us", "/about/", "/company", "/aboutus",
                  "/careers", "/jobs", "/join", "/recruit", "/news", "/news/"]:
            url_list.append(base + p)

    # ② 多引擎四维搜索（公司名取核心名；搜狗串行主引擎，Bing/百度并发补充）
    c = _core_name(company_name)
    queries = {
        "招聘": f"{c} 招聘 岗位 职位",
        "动态": f"{c} 最新 动态 业务",
        "融资规模": f"{c} 公司 融资 上市 规模",
        "工商正规性": f"{c} 工商 注册资本 成立",
        "口碑": f"{c} 怎么样 员工 评价 口碑",
    }
    search_results = {}
    # 主引擎：搜狗串行（搜狗对并发请求风控严格，串行最稳且质量最高）
    for k, q in queries.items():
        items = _search_sogou(q, limit=5)
        if items:
            search_results[k] = items
    if not search_results:
        # 兜底：搜狗偶发全空时，用核心名单查询一次
        items = _search_sogou(f"{c} 公司", limit=8)
        if items:
            search_results["动态"] = items
    # 副引擎：Bing/百度并发补充
    def _search_others(kv):
        k, q = kv
        items = _search_bing(q, limit=4) + _search_baidu(q, limit=4)
        return k, items
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_search_others, kv): kv for kv in queries.items()}
        try:
            for f in concurrent.futures.as_completed(futs, timeout=14):
                k, items = f.result()
                search_results.setdefault(k, []).extend(items)
        except concurrent.futures.TimeoutError:
            pass

    # ③ 垂直源定向（搜狗串行查关键平台收录页：招聘/点评/口碑，精选对中小厂最有价值的 3 源）
    vertical_results = {}
    for key, label, _domain in VERTICAL_SITES:
        if key not in ("zhipin", "liepin", "maimai"):
            continue
        q = f"{c} {label.split('(')[0]}"
        items = _search_sogou(q, limit=3)
        if items:
            vertical_results[key] = (label, [it for it in items if not _is_noise(it.get("url", ""), it.get("title", ""))])

    # ④ 官网域名发现：多引擎结果 + 垂直源结果
    domain_candidates = []
    for k in ("招聘", "动态"):
        domain_candidates.extend(search_results.get(k) or [])
    for _k, (_label, items) in vertical_results.items():
        domain_candidates.extend(items)
    dom = _find_official_domain(domain_candidates)
    if dom:
        website_found = dom
        for p in ["", "/about", "/about-us", "/about/", "/news"]:
            url_list.append(dom + p)
    if not website_found:
        for dom in [f"https://www.{c}.com", f"https://{c}.com",
                    f"https://www.{c}.cn", f"https://{c}.cn",
                    f"https://{c}.com.cn"]:
            url_list.append(dom)

    # ⑤ 并发抓取官网页面（限时，失败不影响）
    def _grab(u):
        return u, fetch_url(u, timeout=6, max_chars=1500)
    seen = set()
    grabbed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_grab, u): u for u in url_list[:14]}
        try:
            for f in concurrent.futures.as_completed(futures, timeout=12):
                u, text = f.result()
                if text and u not in seen:
                    seen.add(u)
                    grabbed.append((u, text))
        except concurrent.futures.TimeoutError:
            pass
    for u, text in grabbed[:6]:
        sources.append({"source": u, "content": text[:1000]})
        raw_parts.append(f"[官网 {u}]\n{text[:1800]}")

    # ⑥ 多引擎搜索结果汇总（过滤词典/百科噪音）
    for k, items in search_results.items():
        for it in items:
            if _is_noise(it.get("url", ""), it.get("title", "")):
                continue
            text = f"{it['title']}。{it['snippet']}".strip()
            if len(text) < 15:
                continue
            eng = it.get("engine", "")
            sources.append({"source": f"搜索[{k}/{eng}]：{it.get('url','')[:80]}", "content": text[:350]})
            raw_parts.append(f"[搜索{k}]\n{text[:500]}")

    # ⑦ 垂直源结果汇总（重点标注来源类型，供 AI 判断正规性/招聘信号/口碑）
    for key, (label, items) in vertical_results.items():
        for it in items[:2]:
            text = f"{it['title']}。{it['snippet']}".strip()
            if len(text) < 15:
                continue
            sources.append({"source": f"垂直源[{label}]：{it.get('url','')[:80]}", "content": text[:350]})
            raw_parts.append(f"[{label}]\n{text[:500]}")

    # ⑦.5 在招职位 + 口碑评价 定向聚合（供 AI 分析"公司真正需要什么样的人"）
    job_items, rep_items = [], []
    for k, items in search_results.items():
        for it in items:
            u, ti, sn = it.get("url", ""), it.get("title", ""), it.get("snippet", "")
            if _is_job_item(u, ti, sn):
                job_items.append(f"{ti}。{sn}".strip()[:300])
            elif _is_reputation_item(u, ti, sn):
                rep_items.append(f"{ti}。{sn}".strip()[:300])
    for key, (label, items) in vertical_results.items():
        for it in items:
            u, ti, sn = it.get("url", ""), it.get("title", ""), it.get("snippet", "")
            text = f"{ti}。{sn}".strip()[:300]
            if key in ("zhipin", "liepin"):
                job_items.append(f"[{label}] {text}")
            elif key == "maimai" or _is_reputation_item(u, ti, sn):
                rep_items.append(f"[{label}] {text}")
    seen_j, seen_r = set(), set()
    job_uniq, rep_uniq = [], []
    for x in job_items:
        if x[:40] not in seen_j:
            seen_j.add(x[:40])
            job_uniq.append(x)
    for x in rep_items:
        if x[:40] not in seen_r:
            seen_r.add(x[:40])
            rep_uniq.append(x)
    if job_uniq:
        raw_parts.append("[在招职位（含该公司其他在招岗位，用于分析真实用人需求）]\n" + "\n".join(job_uniq[:8]))
        sources.append({"source": "招聘平台在招职位聚合", "content": "；".join(job_uniq[:6])[:600]})
    if rep_uniq:
        raw_parts.append("[口碑与评价（员工/求职者视角）]\n" + "\n".join(rep_uniq[:6]))
        sources.append({"source": "口碑评价聚合", "content": "；".join(rep_uniq[:5])[:500]})

    # ⑧ 用户补充资料
    if extra_text and extra_text.strip():
        sources.append({"source": "用户补充资料", "content": extra_text.strip()[:4000]})
        raw_parts.append(f"[用户补充资料]\n{extra_text.strip()[:5000]}")

    # 公开信息充足度：有效来源太少则标记，前端提示用户可补充官网/全称
    effective = [s for s in sources if "搜索" not in s.get("source", "") or len(s.get("content", "")) > 20]
    insufficient = len([s for s in sources if len(s.get("content", "")) > 25]) < 3

    return {"company": company_name, "sources": sources,
            "raw_text": "\n\n".join(raw_parts), "website_found": website_found,
            "insufficient": insufficient}
