"""DeepSeek AI 客户端：匹配分析 / 定向改写 / 情报综合 / 图片识别"""
import base64
import csv
import json
import os
import re
import time
from pathlib import Path

import requests
import storage as _storage

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
USAGE_CSV = BASE_DIR / "data" / "usage.csv"


def _log_usage(model, prompt_tokens, completion_tokens, stage="misc", cache_hit=0, cache_miss=0):
    """把每次 AI 调用的 token 用量追加到 usage.csv（ts,model,stage,prompt,completion,cache_hit,cache_miss）"""
    try:
        USAGE_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE_CSV, "a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow([
                time.strftime("%Y-%m-%d %H:%M:%S"), model, stage,
                int(prompt_tokens or 0), int(completion_tokens or 0),
                int(cache_hit or 0), int(cache_miss or 0),
            ])
    except Exception:
        pass


def _load_env():
    cfg = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    except Exception:
        pass
    cfg.setdefault("DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
    cfg.setdefault("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    cfg.setdefault("DEEPSEEK_MODEL", "deepseek-flash")
    return cfg


CFG = _load_env()


def chat(messages, model=None, temperature=0.3, max_tokens=4000, json_mode=False, retries=2,
         fallback_model="deepseek-v4-pro", stage="misc"):
    model = model or CFG["DEEPSEEK_MODEL"]
    if not CFG["DEEPSEEK_API_KEY"]:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，请检查 resume-tool/.env 文件")
    url = CFG["DEEPSEEK_BASE_URL"].rstrip("/") + "/chat/completions"
    last_err = None
    models = [model] if model == fallback_model else [model, fallback_model]
    for mi, model in enumerate(models):
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # 禁用模型推理（deepseek-flash 默认思考，会烧掉大量 token 且拖慢响应）
            "thinking": {"type": "disabled"},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(retries):
            try:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {CFG['DEEPSEEK_API_KEY']}"},
                    json=payload,
                    timeout=90,
                )
                if resp.status_code == 402:
                    raise RuntimeError("DeepSeek API 余额不足：请登录 platform.deepseek.com 充值后再试")
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    continue
                if resp.status_code != 200:
                    raise RuntimeError(f"DeepSeek API 错误 {resp.status_code}: {resp.text[:300]}")
                content = resp.json()["choices"][0]["message"]["content"]
                if content and content.strip():
                    _usage = resp.json().get("usage") or {}
                    _log_usage(model, _usage.get("prompt_tokens", 0), _usage.get("completion_tokens", 0), stage,
                               _usage.get("prompt_cache_hit_tokens", 0), _usage.get("prompt_cache_miss_tokens", 0))
                    return content.strip()
                # 空内容：等待后重试
                time.sleep(2 * (attempt + 1))
            except requests.exceptions.RequestException as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
    if last_err:
        _storage.log_event("ai_error", "error", "", f"{stage} | DeepSeek API 请求失败（已重试）: {last_err}")
        raise RuntimeError(f"DeepSeek API 请求失败（已重试）: {last_err}")
    _storage.log_event("ai_error", "error", "", f"{stage} | DeepSeek API 返回空内容（已重试全部模型）")
    raise RuntimeError("DeepSeek API 返回空内容（已重试全部模型）")


def get_balance():
    """查询 DeepSeek 账户当前余额（元）。失败返回 None。"""
    if not CFG["DEEPSEEK_API_KEY"]:
        return None
    try:
        resp = requests.get(
            CFG["DEEPSEEK_BASE_URL"].rstrip("/") + "/user/balance",
            headers={"Authorization": f"Bearer {CFG['DEEPSEEK_API_KEY']}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            infos = data.get("balance_infos") or []
            total = sum(float(i.get("total_balance") or 0) for i in infos)
            return {
                "available": data.get("is_available", False),
                "total_balance": round(total, 4),
                "currency": infos[0].get("currency", "CNY") if infos else "CNY",
            }
    except Exception:
        pass
    return None


def _safe_json(content):
    """从模型输出中稳健解析 JSON"""
    content = content.strip()
    try:
        return json.loads(content)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", content)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    # 兜底：把非 JSON 输出包裹为文本
    return {"raw_text": content}


# ---------- ① JD-简历匹配分析 ----------
ANALYZE_SYSTEM = """你是一位资深HR与简历优化专家。你的任务：把一份简历与一个岗位要求(JD)逐项对照分析。

要求：
1. 只输出一个 JSON 对象，不要输出任何多余文字。
2. JSON 结构：
{
  "match_score": 0到100的整数,
  "summary": "总体判断，2-3句话",
  "matched": [{"jd_requirement":"JD要求要点","resume_evidence":"简历中对应证据，没有则写'无'","comment":"一句话点评"}],
  "gaps": [{"requirement":"缺失或薄弱的要求","severity":"高/中/低","suggestion":"基于简历已有事实给出真实可行的补强建议，不得建议编造经历"}],
  "hidden_strengths": [{"skill":"可迁移能力","evidence":"简历中哪段经历体现","why":"为什么这个岗位需要它"}],
  "worth_applying": "建议投递/谨慎投递/不建议投递，并给一句理由",
  "rewrite_guide": "给改写引擎的具体指令：1)本岗位核心关键词（5-8个，来自JD）；2)建议前置/置顶的简历模块与要点（按相关度排序）；3)每段主要经历应突出的岗位化角度（一句话）；4)应压缩或弱化的内容（基于原文判断）"
}
3. 严格基于简历原文事实分析，不得凭空想象简历中没有的经历或技能。
4. matched 和 gaps 各至少 3 条。
5. 【行业语言适配（重要）】先判断这份简历的岗位性质（设计艺术岗 / 运营市场岗 / 技术岗 / 行政服务岗 等），全部点评、建议、短板描述一律使用该行业 HR 熟悉的语言：
   - 设计艺术岗用「视觉表达、作品集、版式、品牌调性、创意、审美、细节把控」等，禁止用「软件熟练度分级、技术栈、编码」等技术岗措辞（软件工具本身可提，但要弱化）。
   - 运营市场岗用「内容、用户、活动、复盘、渠道」等业务语言；技术岗才用技术语言。
   判断依据是简历与 JD 的实际内容，不要默认套用程序员话术。"""


def analyze_match(resume_text, jd_text):
    user = f"【简历原文】\n{resume_text}\n\n【岗位要求JD】\n{jd_text}"
    content = chat(
        [{"role": "system", "content": ANALYZE_SYSTEM},
         {"role": "user", "content": user}],
        json_mode=True,
        max_tokens=5000,
        stage="analysis",
    )
    return _safe_json(content)


# ---------- ② 简历定向改写（岗位完全定制版） ----------
REWRITE_SYSTEM = """你是一位顶级简历专家 + 资深HR视角的面试顾问。用户大多不会写简历：原文要么平淡、要么零散、要么没有重点。你的任务不是「在原简历上修修补补」，而是「整容式重构」——用现代简历的表达系统，把用户真实的经历重新组织成一份「第一眼就让HR觉得专业、第二眼就觉得'这人就是我们要的'」的简历。

【现代简历范式（必须遵守）】
1. 【品牌头部】顶部一行式定位：姓名 + 目标岗位 + 一句话个人品牌（例如：懂业务的数据型运营，擅长从0到1搭建内容增长体系）。让HR 3秒知道你是谁、为什么适合这个岗位。
2. 【核心优势区】紧随头部放「核心优势」3-4条：每条 = 能力点 + 证据（来自原文经历）+ 结果/量化（可推导才写）。这是全文最重要的位置，必须句句有信息、句句贴JD。
3. 【经历重写规则】每段经历不是照抄原文，而是按「强动词 + 动作 + 方法 + 结果」重写：
   - 动词必须强：「主导/搭建/落地/驱动/从0到1/重构/建立/优化/达成」，禁用「负责/参与/协助」等弱词（须有原文事实支撑）。
   - 表达必须专业具体：把「做了xx」升级为「针对xx问题，通过xx方法，达成xx结果」，方法可以写得专业（基于原文），结果能量化就量化。
   - 原文太简略的地方：在不编造的前提下，把隐含的动作和影响写出来（例如原文「管理公众号」→「独立运营公众号，建立选题-排期-复盘机制，输出内容提升活跃」——机制可补、数字不可编）。
4. 【贴岗重构】把 JD 的每一条核心要求映射到用户真实经历上：匹配的经历放大写、提前写；弱相关的压缩为一句；无关的合并精简。让 HR 读完发现「JD 要的每条，简历都有对应」。
5. 【现代排版】模块按含金量排：核心优势 → 项目/经历 → 技能 → 教育。技能用标签式短语（如「Python · 数据可视化 · 用户增长」），不要长句。全篇一页信息密度，无空话、无套话、无「吃苦耐劳」类废话。
6. 【事实铁律】严禁编造：不得新增原文不存在的经历、公司、学历、奖项、证书、数字、技能。数字只允许「原文可推导」（原文有800，可写增长至2000/增长150%）；原文没有数字就写「可量化结果待补充（面试时口头补充）」或者不写数字只写动作。不得为了好看编造任何东西。
7. 【格式】输出 Markdown 结构（这是页面渲染简历的依据，务必遵守）：
   - 第一行：`# 姓名`（单井号，仅姓名一个词）。
   - 模块标题：`## 模块名`（如「## 核心竞争力」「## 代表项目」「## 技能栈」「## 教育背景」「## 荣誉证书」）。
   - 要点/条目：行首用 `- `（短横+空格）。
   - 定位行与一句话品牌：作为头部第 2、3 行纯文本，不要加井号。
   - 严禁 inline 标记：禁止 `**` 星号、`*` 斜体、` 反引号、表格、代码块、图片链接。版式干净，只用 #/## 和 -。
8. 直接输出改写后的完整简历，不要任何前言后语。
9. 【句式 100% 重写铁律】任何一句都不允许与原文句子相同或近似（含句式骨架相同、换几个词的情况）。原文每个信息点都必须用全新的句子表达：换角度、换结构、换动词、换主语视角（例如原文「负责xx系统的开发」→「独立设计并上线xx系统，支撑xxx场景」；原文「参与了xx大赛」→「带队从0到1完成xx，斩获省级一等奖（全校第一）」）。宁可重写得更专业，也绝不允许出现「几乎没变」的句子。
10. 【模块现代化】模块名不沿用原文：核心优势类可叫「核心竞争力」/「为什么选我」；项目经历类可叫「代表项目」/「实战作品」；技能可叫「技能栈」/「技术工具箱」。段落顺序、信息分组必须与原文不同。
11. 【行业语言适配（重要）】动手前先判断岗位性质：技术岗 / 设计艺术岗 / 运营市场岗 / 行政服务岗 等。全文一律使用该行业 HR 熟悉的语言：
   - 设计、艺术、视觉类岗位：用「视觉表达、作品集、版式与排版、品牌调性、色彩构成、创意落地、审美与细节」等，禁止出现「技术栈、编码、系统开发、软件熟练度分级」等技术岗措辞；软件工具（PS/AI/Figma）作为「技能标签」一笔带过即可。
   - 运营、市场、内容类岗位：用「内容增长、用户洞察、活动落地、数据复盘、渠道协同」等业务语言。
   - 技术岗才使用技术栈/工程化语言。
   判断依据来自 JD 与简历原文的实际内容，不要默认套用程序员话术。
12. 【HR 眼前一亮】整体读感要达到：招聘者第一眼被头部定位吸引，第二眼被优势区的量化证据说服，第三眼确认「JD 要的每一条都有对应」。信息密度高、无一句废话、无一句「负责/参与/协助」弱词。
13. 简历全文结束后，另起一行输出：【事实核验】本版本未新增任何原文不存在的事实；已按现代简历范式整容式重构并贴合目标岗位。
14. 【篇幅约束】整份简历正文控制在 900 字左右（单页 A4 信息密度，宁精勿滥）：头部定位 1-2 行、核心优势 3-4 条、每段经历 2-4 行、技能 1 行、教育 1 行。删除一切空话、套话与重复表达，不留水分。"""

STYLE_NOTES = {
    "custom": "【本次版本：岗位完全定制版】针对目标公司的JD要求与业务痛点做现代范式重构：品牌句、核心优势、经历 STAR 全部围绕「这个岗位要什么」组织；结合公司情报（只能基于用户真实经历与情报事实，不得编造），让 HR 明显感到这是专门为该公司写的。",
}


def strip_md(text):
    """轻净化 AI 输出：去掉 inline 标记（** 星号、` 反引号），压缩多余空行。
    保留 # / ## 标题与 - 列表结构——前端 md2html 与后端 _parse_resume 依赖它们渲染结构化简历，
    不能删除结构符号，否则整篇会塌成不可读的纯文本流。"""
    if not text:
        return text or ""
    lines = []
    for ln in text.splitlines():
        s = ln.rstrip().replace("**", "").replace("`", "")
        lines.append(s)
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def rewrite_resume(resume_text, jd_text, analysis_text="", style="general", intel_text="", supplements=None):
    style_note = STYLE_NOTES.get(style, STYLE_NOTES["custom"])
    # 前缀缓存友好：稳定内容（系统指令 + 公司情报块）放最前，易变内容（简历/JD/分析）放最后
    messages = [{"role": "system", "content": REWRITE_SYSTEM + "\n\n" + style_note}]
    if intel_text.strip():
        messages.append({"role": "system", "content":
            "【公司情报库（仅作定制参考；只能参考其事实，严禁编造或照抄其中任何表述）】\n" + intel_text})
    user = (f"【简历原文】\n{resume_text}\n\n【岗位要求JD】\n{jd_text}\n\n"
            f"【匹配分析结论】\n{analysis_text if analysis_text else '（无，按JD直接优化）'}")
    if supplements:
        supp = "\n".join(f"- {s}" for s in supplements[:3])
        user += ("\n\n【用户补充素材（口语原始内容，不是简历成稿）】\n" + supp +
                 "\n\n加工要求：把上述补充素材逐条加工成简历语言——"
                 "① 每条展开为 2-3 句完整的简历条目：强动词开头 + 具体做法/方法/机制 + 可感知的结果或影响；"
                 "② 主动补全叙事连接（做了什么→怎么做的→达到了什么效果），把口语短句扩展为 40-90 字的成稿片段；"
                 "③ 有机融入「核心优势/对应经历/项目」等最相关的板块，不新增板块、不单列「补充」区块；"
                 "④ 只对用户提到的内容做表达升级与合理展开；严禁编造具体数字、公司、奖项、证书，"
                 "模糊处可用「多次/持续/独立/跨周期」等非虚构程度词衔接。"
                 "示例：用户说「我做过海报，贴在食堂门口很多人看」→ 成稿「独立设计社团活动海报并落地张贴于食堂、"
                 "图书馆等真实公共场景，以鲜明视觉吸引大量同学驻足拍照，被社团选为长期展示物料，"
                 "积累了从创意到落地的完整实战经验」。")
    messages.append({"role": "user", "content": user})
    return chat(
        messages,
        model="deepseek-v4-pro",
        temperature=0.55,
        max_tokens=4000,
        stage="rewrite",
    )


# ---------- ②b 自动提取公司名 / 岗位名 / 求职意向 ----------
EXTRACT_SYSTEM = """从【材料】中提取关键求职信息，只输出一个JSON对象，不要任何多余文字：
{"company_name":"目标公司名称（材料中无则留空字符串）","job_title":"目标岗位名称（取职位名称/求职意向/岗位标题；材料中无明确标题时根据职责概括，实在无法判断才留空）","note":"一句话说明这份材料是简历还是招聘JD；若岗位名是概括推断的，注明'岗位名由职责概括推断'"}
规则：
- 若材料是简历：company_name 留空，job_title 取求职意向（如"AI运营实习生"）。
- 若材料是招聘JD/公司要求：company_name 取公司名称（若出现），job_title 优先取独立岗位标题/职位名（如"视觉设计师"）。
- 若JD中没有独立岗位标题：根据岗位职责与任职要求概括出一个合理、具体的岗位名称（例如职责是数据标注SOP、标注团队管理、质量体系 → "AI数据标注项目经理"），并在note中注明是概括推断的。
- 忽略示例占位文字（如"例如：新媒体运营专员"）、界面元素文字。
- 不确定公司名时留空，不要猜测编造。"""


_META_CACHE = {}


def extract_meta(text):
    """从简历或招聘JD中提取 公司名/岗位名。

    本地规则优先（0 token，且带结果缓存）；本地规则漏掉的部分才调 AI 补齐。
    重复识别同一段文本直接命中缓存，不产生任何消耗。
    """
    key = text[:120]
    hit = _META_CACHE.get(key)
    if hit is not None:
        return hit
    out = _local_extract(text)
    # 本地规则已经把公司/岗位都识别出来了 → 不调 AI
    if out.get("company_name") and out.get("job_title"):
        _META_CACHE[key] = out
        return out
    # 缺哪项问哪项（一次调用补全）
    try:
        content = chat(
            [{"role": "system", "content": EXTRACT_SYSTEM},
             {"role": "user", "content": f"【材料】\n{text[:6000]}"}],
            json_mode=True, max_tokens=800, retries=1, stage="meta",
        )
        if content:
            meta = _safe_json(content)
            if meta:
                for k in ("company_name", "job_title"):
                    if meta.get(k) and not out.get(k):
                        out[k] = meta[k]
    except Exception:
        pass
    _META_CACHE[key] = out
    return out


_NOISE_WORDS = ["状态栏", "顶部", "横幅", "图标", "返回", "收藏", "分享", "更多", "可留空",
                "请输入", "例如", "上传", "一键", "自动识别", "官网地址", "补充情报",
                "广告", "弹窗", "房间", "请输入房间"]

# 本地规则容易误判成公司名的普通短语（以这些词结尾的直接丢弃）
_COMPANY_FALSE_TAIL = ("经验", "专业", "要求", "职责", "技术", "方向", "项目", "开发",
                       "岗位", "能力", "技能", "背景", "经历", "工作", "内容", "相关",
                       "优先", "更佳", "者优先", "招聘")


def _local_extract(text):
    """本地规则兜底：从文本中提取公司名/岗位名/求职意向（带噪音过滤）"""
    out = {"company_name": "", "job_title": "", "note": "本地规则识别"}
    # 公司名⓪：<名字>·人事 / <名字>·HR（招聘详情页"XX·人事"最可靠）
    m0 = re.search(r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{1,15}?)[·｜]\s*(?:人事|HR|人力|招聘)", text)
    if m0 and not _hit_noise(m0.group(1)) and not m0.group(1).endswith(_COMPANY_FALSE_TAIL):
        out["company_name"] = m0.group(1)[:30]
    # 公司名①：<名字> 招聘/招募（招聘JD最可靠模式）
    if not out["company_name"]:
        m = re.search(r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{1,15}?)\s*(?:2025|2026)?\s*(?:秋招|校招|社招|招聘|招募)", text)
        if m:
            name = m.group(1)
            # 排除学校/机构名 + 误匹配短语
            if (not re.search(r"(学院|大学|学校)$", name) and not _hit_noise(name)
                    and not name.endswith(_COMPANY_FALSE_TAIL)):
                out["company_name"] = name[:30]
    # 公司名②：<名字>公司/集团/科技/网络...
    if not out["company_name"]:
        m2 = re.search(r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{1,15}?)(?:公司|集团|科技|网络|传媒|数据|智能|信息|有限(?:公司)?)", text)
        if m2:
            name = m2.group(1)
            suffix = m2.group(0)[len(name):]
            cand = (name + suffix)[:30]
            # 若匹配后紧跟学院/大学/学校等教育机构后缀，视为学校名，丢弃
            tail = text[m2.end():m2.end() + 4]
            if (not re.search(r"(学院|大学|学校)", tail) and not _hit_noise(cand)
                    and not cand.startswith(_COMPANY_FALSE_TAIL)):
                out["company_name"] = cand
    # 岗位①：招聘/招募/诚聘 + 岗位词（含后缀词）
    m3 = re.search(r"(?:招聘|招募|诚聘)[\s:：]*(?:岗位|职位|实习生)?[\s:：]*([\u4e00-\u9fa5A-Za-z0-9·/／]{1,20}(?:实习生|运营|开发|工程师|设计师|产品|编辑|专员|经理|主管|顾问|分析师|策划|助理|市场|销售))", text)
    # 岗位②：岗位/职位：xxx
    m4 = re.search(r"(?:岗位|职位|岗位名称)[\s:：]+([\u4e00-\u9fa5A-Za-z0-9·/／]{2,28})", text)
    # 岗位③：简历求职意向
    m5 = re.search(r"求职意向[\s:：]+([\u4e00-\u9fa5A-Za-z0-9·/／]{2,30})", text)
    # 岗位④：标题行里的"XX实习生"等（JD首行常见）
    m7 = re.search(r"^[\s\S]{0,60}?([\u4e00-\u9fa5A-Za-z0-9·/／]{1,20}(?:实习生|运营|开发|工程师|设计师|产品|编辑|专员|经理|主管|顾问|分析师|策划|助理))", text)
    cand = None
    if m3:
        cand = m3.group(1)
    elif m4:
        cand = m4.group(1)
    elif m5:
        cand = m5.group(1)
    elif m7:
        cand = m7.group(1)
    if cand and not _hit_noise(cand):
        out["job_title"] = cand
    out["company_name"] = (out["company_name"] or "").strip()
    out["job_title"] = (out["job_title"] or "").strip()
    return out


def _hit_noise(text):
    """判断提取结果是否命中界面/示例噪音词"""
    return any(w in text for w in _NOISE_WORDS)


# ---------- ③ 公司情报综合 ----------
INTEL_SYSTEM = """你是一位行业与公司研究分析师。根据【公司原始情报】判断该公司的真实现状：业务方向、当前最紧迫的需求与问题、招聘活跃度、企业正规性，并给出求职建议。

要求：
1. 只输出一个 JSON 对象。
2. JSON 结构：
{
  "company_profile": "企业基础信息：成立时间、规模、行业、注册/上市/融资状态（仅来自情报，无则写明'未见'）",
  "company_status": "企业当前真实状态判断：扩张/收缩/转型/稳定/未知，给出依据（来自官网页面、新闻、招聘信号）",
  "legitimacy": "正规性评估：是否有工商注册信息、融资报道、官网存在、知名雇主等佐证；若未找到任何正规性佐证，明确说明'未查到可靠佐证，投递前建议核实'",
  "pain_points": [{"point":"公司当前最紧迫的需求或问题","evidence":"依据哪条情报","confidence":"证据充分/推断"}],
  "business_direction": "公司当前的业务方向判断及依据（若情报不足请如实说明）",
  "recruiting_signals": "当前招聘活跃度与信号：哪些岗位在招、招聘规模、是否扩张（来自招聘平台/官网招聘页情报；无则说明）",
  "job_mix": "在招岗位集合分析（重点）：该公司同时在招哪些岗位（含用户投递岗位之外的其他在招职位）→ 推断公司业务重心、最急需什么样的人、团队处于什么阶段（扩张/补缺/新业务/收缩）。若没有在招职位情报，如实说明。",
  "reputation": "口碑与用人状态：员工/求职者对该公司的评价（来自脉脉/看准/职友集等口碑源，如'加班多''流动性大''氛围好'等），判断该公司是否真的在招人、是否值得投、投递风险点。无口碑情报则如实说明。",
  "resume_angle": [{"title":"简历表述方向","detail":"具体怎么做，必须基于用户真实简历事实，不得建议编造"}],
  "interview_questions": ["面试官可能问的3-5个问题"],
  "sources_note": "情报可靠性说明：哪些有证据、哪些是推断；官网是否成功访问"
}
3. 若情报明显不足，必须在相关字段明确说明"情报有限，以下为基于有限信息的推断"。
4. 不得编造公司事实；所有结论要能追溯到情报来源或明确标注为推断。"""


def intel_summary(raw_text, company_name, resume_text=""):
    user = (f"【公司名称】{company_name}\n\n【公司原始情报】\n{raw_text}\n\n"
            f"【用户简历摘要】\n{resume_text[:1500] if resume_text else '（未提供）'}")
    return _safe_json(chat(
        [{"role": "system", "content": INTEL_SYSTEM},
         {"role": "user", "content": user}],
        json_mode=True,
        max_tokens=6000,
        stage="intel",
    ))


# ---------- ④ 图片简历 OCR（视觉模型，尽力而为） ----------
def vision_extract_text(image_path, kind="resume", retries=2):
    """用 DeepSeek 视觉模型识别图片文字；kind=resume|jd；自动重试 + 长图预压缩"""
    b64 = base64.b64encode(_prepare_image(image_path)).decode()
    url = CFG["DEEPSEEK_BASE_URL"].rstrip("/") + "/chat/completions"
    if kind == "jd":
        text_rule = ("这是招聘信息/岗位要求（JD）相关的截图。请提取其中与招聘相关的全部文字内容："
                     "公司名称、岗位名称、职位描述、任职要求、薪资福利等，保持原有顺序与结构。"
                     "【重要】忽略以下与招聘无关的内容：状态栏、系统UI、图标名称、按钮文字、"
                     "广告弹窗、界面提示、示例占位文字（如“例如：新媒体运营专员”）。"
                     "若图中没有任何与招聘相关的内容，请只输出“（未识别到招聘相关内容）”。")
    else:
        text_rule = ("这是简历相关的截图。请提取其中与求职者相关的全部文字内容："
                     "个人信息、求职意向、教育背景、经历、技能等，保持原有顺序与结构。"
                     "【重要】忽略以下与简历无关的内容：状态栏、系统UI、图标名称、按钮文字、"
                     "广告弹窗、界面提示、示例占位文字。"
                     "若图中没有任何与简历相关的内容，请只输出“（未识别到简历相关内容）”。")
    payload = {
        "model": "deepseek-v4-flash-vision-exp",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text",
                 "text": text_rule},
            ],
        }],
        "max_tokens": 6000,
        "thinking": {"type": "disabled"},
    }
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {CFG['DEEPSEEK_API_KEY']}"},
                json=payload,
                timeout=120,
            )
            if resp.status_code != 200:
                if resp.status_code == 402:
                    raise RuntimeError("DeepSeek API 余额不足：请登录 platform.deepseek.com 充值后再试")
                raise RuntimeError(f"视觉模型错误 {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if content and content.strip():
                _usage = data.get("usage") or {}
                _log_usage("deepseek-v4-flash-vision-exp", _usage.get("prompt_tokens", 0), _usage.get("completion_tokens", 0), "ocr",
                           _usage.get("prompt_cache_hit_tokens", 0), _usage.get("prompt_cache_miss_tokens", 0))
                return content.strip()
            time.sleep(3 * (attempt + 1))
        except Exception as e:
            last_exc = e
            time.sleep(3 * (attempt + 1))
    if last_exc:
        raise RuntimeError(f"视觉识别失败（已重试 {retries} 次）: {last_exc}")
    raise RuntimeError("视觉识别返回空内容（已重试）")


def _prepare_image(image_path, max_side=2400, max_pixels=3200000):
    """读取图片字节；超大/超长图先等比压缩，控制请求体大小（提高识别稳定性）"""
    import io
    try:
        from PIL import Image
        img = Image.open(image_path)
        w, h = img.size
        if max(w, h) <= max_side and w * h <= max_pixels:
            return Path(image_path).read_bytes()
        scale = max_side / max(w, h)
        if w * h * scale * scale > max_pixels:
            scale = (max_pixels / (w * h)) ** 0.5
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        return buf.getvalue()
    except Exception:
        return Path(image_path).read_bytes()
