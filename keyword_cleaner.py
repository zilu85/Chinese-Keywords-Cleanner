#!/usr/bin/env python3
"""
关键词清洗与标准化工具 v2.5
==========================
功能：
  1. 解析万方数据库XML格式文献记录，提取关键词
  2. 基础清洗：统一大小写、空格规范化、全角→半角、去特殊字符
  3. 关键词频次统计（含累积百分比）
  4. 智能识别同义词/近义词变体群（4种策略级联）
  5. 生成交互式审核文件（CSV）
  6. 应用映射表输出标准化关键词列表

用法：
  # 完整分析（解析+清洗+变体检测）
  python keyword_cleaner.py -i data/ -o output/

  # 仅解析和频次统计（不做变体检测）
  python keyword_cleaner.py -i data/ -o output/ --freq-only

  # 应用已有映射表（跳过变体检测，直接标准化）
  python keyword_cleaner.py -i data/ -m mapping.csv -o output/

  # 指定变体检测灵敏度（1=宽松 2=默认 3=严格）
  python keyword_cleaner.py -i data/ -o output/ --sensitivity 2
"""

import re
import os
import sys
import csv
import json
import argparse
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from itertools import combinations

# ============================================================
# 常量定义
# ============================================================

# 常见中文后缀模式（仅限语法性后缀，不含领域词汇）
# v2.2 修正：基于第二轮审核，移除"术"
#   "术"将概念从"疾病/状态"变为"手术/操作"，语义改变
#   反例：乳腺癌≠乳腺癌术，肠造瘘≠肠造瘘术
#   仅保留"学""法"：追加后核心概念不变
GRAMMATICAL_SUFFIXES = [
    '学', '法',  # v2.2: 仅保留2个经过验证的语法后缀
]

# 常见中文前缀模式（仅限限定性前缀，不改变核心语义）
# v2.1 修正：基于审核错误分析，移除所有前缀
# 原因：中文医学领域不存在"不改变核心语义"的前缀
#   "临床护理"≠"护理"，"综合干预"≠"干预"，"早期康复"≠"康复"
#   前缀总是限定范围或改变语义，不应作为同义词依据
GRAMMATICAL_PREFIXES = [
    # v2.1: 清空。如需恢复，必须逐个验证"追加前缀后核心语义不变"
]

# 全角→半角映射范围
FULLWIDTH_OFFSET = 0xFEE0

# v2.1 新增：语义关键字符集
# 这些字符在中文医学复合词中承载核心语义，替换后概念完全改变
# 例如：乳腺癌↔乳腺炎（癌/炎替换），依从性↔依赖性（从/赖替换）
MEDICAL_CRITICAL_CHARS = frozenset(
    '癌炎瘤症伤病衰愈死残畸竭'  # 疾病/病理类
    '急慢良恶'                   # 病性类
    '从赖'                       # 行为类（依从/依赖）
    '护心'                       # 领域类（护理/心理）
    '质控防'                     # 管理类（质量/质控/防控）
    '期率'                       # 度量类（新生儿≠新生期，依从性≠依从率）
    '口瘘'                       # 解剖类（造口≠造瘘）
    '后'                         # 时态类（脑出血≠脑出血后）
    '管伦'                       # 管理类（护理管理≠护理伦理）
    '深浅'                       # 程度类（深静脉≠静脉）
    '手'                         # 手术类（置换术≠置换手术）
    '理'                         # 概念类（护理≠护理理念）
    '室台'                       # 场所类（手术室≠手术台）
    '入植'                       # 操作类（插入≠植入）
    '械构'                       # 属性类（机械≠机构）
    '专'                         # 范围类（专科≠专业）
    # v2.3 新增：基于第三轮审核
    '科业'                       # 领域类（专科≠专业）
    '肠胃'                       # 器官类（鼻肠管≠鼻胃管）
    '肺'                         # 器官类（心脏≠心肺）
    # v2.4 新增：基于第四轮审核
    '习践见'                     # 活动类（实习≠实践≠见习）
    '出缺'                       # 状态类（出血≠缺血）
    '法案'                       # 方法类（方法≠方案）
    '学育员'                     # 角色类（教学≠教育，护理学≠护理员）
)

# v2.1 新增：已知异体字/繁简字对（编辑距离=1但语义相同）
# 这些字符替换后概念不变，不应被语义分歧检测拦截
VARIANT_CHAR_PAIRS = frozenset({
    ('压', '圧'), ('术', '術'), ('产', '産'), ('疗', '療'),
    ('护', '護'), ('验', '験'), ('强', '強'), ('围', '圍'),
    ('体', '體'), ('龄', '齡'), ('脏', '臟'), ('剂', '劑'),
})

# v2.1 新增：已知内容词替换对（字面相似但概念完全不同）
# 基于审核错误分析提取的高频FP模式
# 格式：(word_a, word_b) - 如果两个词的差异部分匹配这对，则拒绝
CONTENT_WORD_PAIRS = frozenset({
    ('护理', '心理'),    # 护理干预 vs 心理干预
    ('质量', '质控'),    # 护理质量 vs 护理质控
    ('质控', '防控'),    # 护理质控 vs 护理防控
    ('干预', '管理'),    # 护理干预 vs 护理管理
    ('管理', '伦理'),    # 护理管理 vs 护理伦理
    ('深静脉', '静脉'),  # 下肢深静脉血栓 vs 下肢静脉血栓
    ('置换', '置换手'),  # 髋关节置换术 vs 髋关节置换手术
    ('外科', '外科护'),  # 快速康复外科 vs 快速康复外科护理
    ('护理', '护理理念'), # 快速康复外科护理 vs 快速康复外科护理理念
    ('术', '手术'),      # 置换术 vs 置换手术
    # v2.3 新增：基于第三轮审核
    ('专科', '专业'),    # 专科护士 vs 专业护士
    ('插入', '植入'),    # 导管插入术 vs 导管植入术
    ('机械', '机构'),    # 机械通气 vs 机构通气
    ('心脏', '心肺'),    # 心脏康复 vs 心肺康复
    ('鼻肠', '鼻胃'),    # 鼻肠管 vs 鼻胃管
})


# ============================================================
# 工具函数
# ============================================================

def fullwidth_to_halfwidth(text):
    """全角字符转半角（字母、数字、常用符号）"""
    result = []
    for ch in text:
        code = ord(ch)
        # 全角空格
        if code == 0x3000:
            result.append(' ')
        # 全角ASCII范围：！～ (U+FF01 ~ U+FF5E)
        elif 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - FULLWIDTH_OFFSET))
        else:
            result.append(ch)
    return ''.join(result)


def clean_keyword(kw):
    """
    基础清洗单个关键词：
    1. 全角→半角
    2. 统一小写（英文部分）
    3. 去除首尾空格，合并连续空格
    4. 去除特殊字符（保留中文、英文、数字、常用符号）
    5. 去除首尾的连字符/斜杠
    """
    if not kw:
        return ''
    # 全角→半角
    kw = fullwidth_to_halfwidth(kw)
    # 小写
    kw = kw.lower()
    # 去除控制字符（换行、制表符等）
    kw = re.sub(r'[\x00-\x1f\x7f]', '', kw)
    # 保留：中文、英文、数字、空格、连字符、加号、斜杠、冒号、逗号、括号
    kw = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbfa-z0-9\s\-+/:,()（）]', '', kw)
    # 中文括号→半角
    kw = kw.replace('（', '(').replace('）', ')')
    # 合并连续空格
    kw = re.sub(r'\s+', ' ', kw)
    # 去首尾空格
    kw = kw.strip()
    # 去首尾连字符/斜杠/冒号/逗号
    kw = kw.strip('-/:,')
    return kw


def normalize_for_comparison(kw):
    """
    用于变体比较的归一化：
    去除所有空格、标点、符号，只保留中文+英文+数字
    """
    kw = fullwidth_to_halfwidth(kw)
    kw = kw.lower()
    # 只保留中文、英文、数字
    kw = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbfa-z0-9]', '', kw)
    return kw


def edit_distance(s1, s2):
    """计算两个字符串的编辑距离（Levenshtein）"""
    if len(s1) < len(s2):
        return edit_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (0 if c1 == c2 else 1)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]


def jaccard_similarity_chars(s1, s2):
    """字符级Jaccard相似度"""
    set1, set2 = set(s1), set(s2)
    if not set1 and not set2:
        return 1.0
    if not set1 or not set2:
        return 0.0
    return len(set1 & set2) / len(set1 | set2)


def is_semantic_divergence(norm1, norm2, kw_freq=None):
    """
    v2.1新增：检测两个归一化字符串是否存在"语义核心分歧"。

    核心思想：中文医学复合词中，某些字符承载核心语义（如"癌/炎"决定疾病类型，
    "护理/心理"决定专业领域）。如果两个词仅在这些核心字符上不同，则它们是
    不同概念，不应作为同义词合并。

    检测规则：
    1. 单字差异 + 两个差异字符都在MEDICAL_CRITICAL_CHARS中 → 语义分歧
       例：乳腺癌↔乳腺炎（癌/炎都是疾病关键字符）
       反例：高血压↔高血圧（圧是压的异体字，不在关键字符集中）
    2. 双字差异 + 两个差异子串都是独立关键词（freq>=5） → 语义分歧
       例：综合性护理干预↔综合性心理干预（"护理"和"心理"都是独立高频词）
       反例：围手术期护理↔围术期护理（"手"不是独立关键词）

    参数：
        norm1, norm2: 归一化后的字符串
        kw_freq: 关键词频次字典，用于判断差异子串是否为独立概念
    """
    if norm1 == norm2:
        return False

    # 找出所有差异位置
    if len(norm1) == len(norm2):
        diffs = [(i, norm1[i], norm2[i]) for i in range(len(norm1)) if norm1[i] != norm2[i]]
    else:
        # 长度不同时，用简单对齐找差异
        return _is_semantic_divergence_len_diff(norm1, norm2, kw_freq)

    if not diffs:
        return False

    # ---- 规则1：单字差异，医学关键字符检测 ----
    if len(diffs) == 1:
        _, char_a, char_b = diffs[0]
        # 先检查是否为已知异体字/繁简字对
        pair = tuple(sorted([char_a, char_b]))
        if pair in VARIANT_CHAR_PAIRS:
            return False  # 异体字替换，不是语义分歧
        # 两个差异字符都在医学关键字符集中 → 语义分歧
        # 例：癌↔炎, 从↔赖, 护↔心, 口↔瘘
        if char_a in MEDICAL_CRITICAL_CHARS and char_b in MEDICAL_CRITICAL_CHARS:
            return True
        # v2.2扩展：单字差异的语义分歧检测
        # 核心逻辑：如果差异字符中有一个是"关键字符"（承载核心语义），
        # 且另一个不是"语法性字符"（不改变语义的虚字），则判定为语义分歧
        # 例：率↔性(依从率≠依从性) - 率是关键字符，性是语法字符 → 分歧
        # 例：期↔儿(新生期≠新生儿) - 期是关键字符，儿是语法字符 → 分歧
        # 例：期↔人(老年期≠老年人) - 期是关键字符，人是语法字符 → 分歧
        # 反例：圧↔压(异体字) - 已在VARIANT_CHAR_PAIRS中保护
        GRAMMATICAL_CHARS = frozenset('的了吗了呢着性化学法术者人中上下')
        a_critical = char_a in MEDICAL_CRITICAL_CHARS
        b_critical = char_b in MEDICAL_CRITICAL_CHARS
        # 只要有一个是关键字符，就触发（另一个是否语法性不影响判断，
        # 因为关键字符替换语法字符同样改变了概念）
        if a_critical or b_critical:
            return True

    # ---- 规则2：双字差异，内容词替换 ----
    if len(diffs) == 2:
        # 检查差异字符是否连续（同一位置的2字替换）
        pos1, pos2 = diffs[0][0], diffs[1][0]
        if pos2 == pos1 + 1:  # 连续位置
            diff_str_a = diffs[0][1] + diffs[1][1]
            diff_str_b = diffs[0][2] + diffs[1][2]
            # 规则2a：已知内容词替换对黑名单
            pair_key = tuple(sorted([diff_str_a, diff_str_b]))
            if pair_key in CONTENT_WORD_PAIRS:
                return True
            # 规则2b：两个差异子串都是独立高频关键词
            if kw_freq is not None:
                freq_a = kw_freq.get(diff_str_a, 0)
                freq_b = kw_freq.get(diff_str_b, 0)
                if freq_a >= 5 and freq_b >= 5:
                    return True

    # ---- 规则3：3字及以上差异，检查是否为内容词替换 ----
    if len(diffs) >= 3:
        # 提取连续差异块
        diff_blocks = _extract_diff_blocks(diffs)
        for block_a, block_b in diff_blocks:
            if len(block_a) >= 2 and len(block_b) >= 2:
                # 检查内容词对黑名单
                pair_key = tuple(sorted([block_a, block_b]))
                if pair_key in CONTENT_WORD_PAIRS:
                    return True
                # 检查是否为独立高频关键词
                if kw_freq is not None:
                    freq_a = kw_freq.get(block_a, 0)
                    freq_b = kw_freq.get(block_b, 0)
                    if freq_a >= 5 and freq_b >= 5:
                        return True

    return False


def _is_semantic_divergence_len_diff(norm1, norm2, kw_freq=None):
    """处理长度不同的两个字符串的语义分歧检测"""
    # 短词是长词的子串时，检查多出部分是否为内容词
    if len(norm1) > len(norm2):
        long, short = norm1, norm2
    else:
        long, short = norm2, norm1

    # 如果短词是长词的前缀
    if long.startswith(short):
        extra = long[len(short):]
        # v2.4: 如果extra恰好是语法后缀，不触发关键字符检测
        # 例：循证护理→循证护理学（"学"是语法后缀，核心概念不变）
        if extra not in GRAMMATICAL_SUFFIXES:
            # 多出部分含关键字符 → 语义分歧
            # 例：脑出血→脑出血后（"后"在关键字符集）
            if any(ch in MEDICAL_CRITICAL_CHARS for ch in extra):
                return True
        # v2.2: 多出部分匹配内容词对黑名单
        # 例：置换术→置换手术（"手术"匹配内容词对 术↔手术）
        for pair_a, pair_b in CONTENT_WORD_PAIRS:
            if extra == pair_a or extra == pair_b:
                return True
            # 也检查extra是否包含内容词对中的词
            if len(pair_a) >= 2 and pair_a in short + extra:
                # 短词尾部+extra 构成内容词替换
                overlap = short[-(len(pair_a)-1):] if len(pair_a) > 1 else ''
                if overlap + extra[:len(extra)-(len(pair_a)-1)+1] == pair_a or \
                   overlap + extra[:len(extra)-(len(pair_a)-1)+1] == pair_b:
                    return True
        # 多出部分是独立高频内容词
        if kw_freq and extra in kw_freq and kw_freq[extra] >= 5:
            return True
    # 如果短词是长词的后缀
    if long.endswith(short):
        extra = long[:len(long)-len(short)]
        if extra not in GRAMMATICAL_PREFIXES:
            if any(ch in MEDICAL_CRITICAL_CHARS for ch in extra):
                return True
        for pair_a, pair_b in CONTENT_WORD_PAIRS:
            if extra == pair_a or extra == pair_b:
                return True
        if kw_freq and extra in kw_freq and kw_freq[extra] >= 5:
            return True

    # v2.2: 非前缀/后缀的子串关系（如"深静脉"被插入中间）
    # 检查两个词是否仅在某个子串处不同
    # 找最长公共子序列，差异部分检查内容词对
    if not long.startswith(short) and not long.endswith(short):
        # 简单方法：找公共前缀和公共后缀
        common_prefix_len = 0
        for i in range(min(len(long), len(short))):
            if long[i] == short[i]:
                common_prefix_len = i + 1
            else:
                break
        common_suffix_len = 0
        for i in range(1, min(len(long), len(short)) + 1):
            if long[-i] == short[-i]:
                common_suffix_len = i
            else:
                break
        # 提取差异部分
        if common_prefix_len > 0 or common_suffix_len > 0:
            long_diff = long[common_prefix_len:len(long)-common_suffix_len if common_suffix_len > 0 else len(long)]
            short_diff = short[common_prefix_len:len(short)-common_suffix_len if common_suffix_len > 0 else len(short)]
            # 检查差异部分是否匹配内容词对
            pair_key = tuple(sorted([long_diff, short_diff]))
            if pair_key in CONTENT_WORD_PAIRS:
                return True
            # 检查差异部分是否含关键字符
            if any(ch in MEDICAL_CRITICAL_CHARS for ch in long_diff + short_diff):
                return True

    return False


def _extract_diff_blocks(diffs):
    """从差异位置列表中提取连续差异块"""
    if not diffs:
        return []
    blocks = []
    current_a = diffs[0][1]
    current_b = diffs[0][2]
    last_pos = diffs[0][0]
    for pos, char_a, char_b in diffs[1:]:
        if pos == last_pos + 1:
            current_a += char_a
            current_b += char_b
        else:
            if len(current_a) >= 2 or len(current_b) >= 2:
                blocks.append((current_a, current_b))
            current_a = char_a
            current_b = char_b
        last_pos = pos
    if len(current_a) >= 2 or len(current_b) >= 2:
        blocks.append((current_a, current_b))
    return blocks


# ============================================================
# Union-Find（并查集）
# ============================================================

class UnionFind:
    """并查集，用于合并变体群"""

    def __init__(self, elements):
        self.parent = {e: e for e in elements}
        self.rank = {e: 0 for e in elements}

    def find(self, x):
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x, y):
        px, py = self.find(x), self.find(y)
        if px == py:
            return
        if self.rank[px] < self.rank[py]:
            px, py = py, px
        self.parent[py] = px
        if self.rank[px] == self.rank[py]:
            self.rank[px] += 1

    def groups(self):
        """返回分组列表，每组是元素的列表"""
        grp = defaultdict(list)
        for e in self.parent:
            grp[self.find(e)].append(e)
        return list(grp.values())


# ============================================================
# 第一步：XML解析
# ============================================================

def parse_wanfang_xml(filepath):
    """
    解析万方数据库XML文件，提取文献记录。
    返回 list[dict]，每个dict包含：
      record_id, year, title, authors, journal, keywords, abstract
    """
    records = []
    try:
        tree = ET.parse(filepath)
    except ET.ParseError as e:
        print(f"  ERROR: XML解析失败 {filepath}: {e}", file=sys.stderr)
        return records

    root = tree.getroot()
    for idx, bib in enumerate(root.findall('.//Bibliography'), 1):
        rec = {}

        # 标题
        title_el = bib.find('.//PrimaryTitle/Title')
        rec['title'] = (title_el.text or '').strip() if title_el is not None else ''

        # 年份
        year_el = bib.find('Year')
        rec['year'] = (year_el.text or '').strip() if year_el is not None else ''

        # 作者
        authors = []
        for au_el in bib.findall('.//Author/Info/FullName'):
            name = (au_el.text or '').strip()
            if name:
                authors.append(name)
        rec['authors'] = authors

        # 期刊
        media_el = bib.find('.//Medium/Media')
        rec['journal'] = (media_el.text or '').strip() if media_el is not None else ''

        # 关键词
        keywords = []
        for kw_el in bib.findall('.//Keywords/Keyword'):
            kw = (kw_el.text or '').strip()
            if kw:
                keywords.append(kw)
        rec['keywords_raw'] = keywords

        # 摘要
        abstract_el = bib.find('.//Abstracts/Abstract')
        rec['abstract'] = (abstract_el.text or '').strip() if abstract_el is not None else ''

        # URL
        url_el = bib.find('Url')
        rec['url'] = (url_el.text or '').strip() if url_el is not None else ''

        rec['record_id'] = f"{os.path.basename(filepath)}_{idx:04d}"
        rec['source_file'] = os.path.basename(filepath)

        records.append(rec)

    return records


def _is_xml_file(fpath):
    """
    通过读取文件头部判断是否为XML文件，不依赖扩展名。
    检查前500字节中是否包含 <?xml 或 <Bibliographies 标记。
    """
    try:
        with open(fpath, 'rb') as f:
            head = f.read(500)
        # 跳过BOM
        if head.startswith(b'\xef\xbb\xbf'):
            head = head[3:]
        head_lower = head.lower()
        return b'<?xml' in head_lower or b'<bibliographies' in head_lower
    except (IOError, OSError):
        return False


def parse_input(input_path):
    """
    解析输入路径（单文件或目录），返回所有记录。
    自动检测文件内容是否为XML，不依赖扩展名。
    """
    all_records = []
    if os.path.isfile(input_path):
        files = [input_path]
    elif os.path.isdir(input_path):
        # 扫描目录下所有文件，通过内容检测判断是否为XML
        all_files = []
        for f in os.listdir(input_path):
            fpath = os.path.join(input_path, f)
            if os.path.isfile(fpath):
                all_files.append(fpath)
        # 先按扩展名筛选(.xml/.txt)，再对无扩展名/其他扩展名的文件做内容检测
        known_ext = [f for f in all_files
                     if f.lower().endswith(('.xml', '.txt'))]
        unknown_ext = [f for f in all_files
                       if not f.lower().endswith(('.xml', '.txt'))]
        # 对未知扩展名的文件做内容检测
        detected = [f for f in unknown_ext if _is_xml_file(f)]
        files = sorted(known_ext + detected)
        if detected:
            print(f"  内容检测发现 {len(detected)} 个XML文件（非.xml/.txt扩展名）")
    else:
        print(f"ERROR: 输入路径不存在: {input_path}", file=sys.stderr)
        return all_records

    if not files:
        print("WARNING: 未找到任何文件", file=sys.stderr)
        return all_records

    for fpath in files:
        print(f"  解析: {os.path.basename(fpath)}")
        recs = parse_wanfang_xml(fpath)
        all_records.extend(recs)
        print(f"    → {len(recs)} 条记录")

    return all_records


# ============================================================
# 第二步：关键词清洗与频次统计
# ============================================================

def extract_and_clean_keywords(records):
    """
    对每条记录的关键词进行清洗，返回：
    - records: 每条记录增加 'keywords_cleaned' 字段
    - kw_freq: Counter，清洗后关键词频次
    """
    kw_freq = Counter()
    for rec in records:
        cleaned = []
        for kw in rec.get('keywords_raw', []):
            c = clean_keyword(kw)
            if c:  # 跳过清洗后为空的关键词
                cleaned.append(c)
        # 去重（同一篇文献内重复关键词）
        cleaned = list(dict.fromkeys(cleaned))
        rec['keywords_cleaned'] = cleaned
        kw_freq.update(cleaned)

    return records, kw_freq


def build_frequency_table(kw_freq, total_papers):
    """
    构建频次表，含百分比和累积百分比。
    返回 list[dict]
    """
    total_kw = sum(kw_freq.values())
    rows = []
    cum_pct = 0.0
    for kw, freq in kw_freq.most_common():
        pct = freq / total_kw * 100 if total_kw > 0 else 0
        cum_pct += pct
        n_papers_pct = freq / total_papers * 100 if total_papers > 0 else 0
        rows.append({
            '关键词': kw,
            '频次': freq,
            '占关键词总数%': round(pct, 2),
            '累积%': round(cum_pct, 2),
            '出现论文数': freq,  # 清洗后每个关键词出现一次即一篇论文
            '占论文总数%': round(n_papers_pct, 2),
        })
    return rows


# ============================================================
# 第三步：变体检测（多策略级联）
# ============================================================

def build_cooccurrence_neighbors(records, kw_freq):
    """
    从文献记录构建关键词的共现邻居集。

    返回 dict: keyword → Counter({neighbor: co_freq})
    每个关键词的"邻居"是与它在同一篇文献中共同出现的关键词及其共现频次。
    """
    neighbors = defaultdict(Counter)
    for rec in records:
        kws = rec.get('keywords_cleaned', [])
        if len(kws) < 2:
            continue
        for i, kw_i in enumerate(kws):
            for j, kw_j in enumerate(kws):
                if i != j:
                    neighbors[kw_i][kw_j] += 1
    return dict(neighbors)


def neighbor_jaccard(neighbors_a, neighbors_b):
    """
    计算两个关键词的共现邻居Jaccard相似度。
    基于邻居集合的交集/并集，而非频次加权。
    """
    set_a = set(neighbors_a.keys())
    set_b = set(neighbors_b.keys())
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def neighbor_cosine(neighbors_a, neighbors_b):
    """
    计算两个关键词的共现邻居余弦相似度。
    基于频次加权，更关注高频共现邻居的重叠。
    """
    all_keys = set(neighbors_a.keys()) | set(neighbors_b.keys())
    if not all_keys:
        return 0.0
    dot = sum(neighbors_a.get(k, 0) * neighbors_b.get(k, 0) for k in all_keys)
    norm_a = sum(v * v for v in neighbors_a.values()) ** 0.5
    norm_b = sum(v * v for v in neighbors_b.values()) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def neighbor_containment(neighbors_a, neighbors_b):
    """
    计算双向包含率：min(A∩B/|A|, A∩B/|B|)。
    
    对于同义词对，低频词的邻居通常是高频词邻居的子集。
    例如"生命质量"的6个邻居可能都在"生活质量"的109个邻居中，
    此时 A∩B/|B| = 6/6 = 1.0，但 A∩B/|A| = 6/109 = 0.055。
    取 min 可以确保两个方向都有足够的重叠。
    
    返回 (min_containment, max_containment, intersection_size)
    """
    set_a = set(neighbors_a.keys())
    set_b = set(neighbors_b.keys())
    if not set_a or not set_b:
        return 0.0, 0.0, 0
    intersection = set_a & set_b
    if not intersection:
        return 0.0, 0.0, 0
    contain_a = len(intersection) / len(set_a)  # B的邻居有多少在A中
    contain_b = len(intersection) / len(set_b)  # A的邻居有多少在B中
    return min(contain_a, contain_b), max(contain_a, contain_b), len(intersection)


def detect_variants(kw_freq, sensitivity=2, records=None, global_mapping=None, global_keeps=None):
    """
    多策略变体检测，返回变体群组列表。

    核心原则：只合并"同一概念的不同写法"，不合并"相关但不同的概念"。
    不使用并查集（避免传递性导致概念爆炸），改为直接构建配对群组。

    策略级联：
    0. 全局映射预合并（已知同义词直接合并，跳过审核）
    1. 归一化精确匹配（去除空格/标点后相同）
    2. 严格后缀/前缀追加（短词 = 长词 - 语法性后缀/前缀）
    3. 字符级Jaccard相似度（仅长度相近的词）
    4. 编辑距离（仅少量字符差异）
    5. 共现邻居否决（仅否决，不增强）

    sensitivity: 1=宽松(多合并) 2=默认 3=严格(少合并)
    records: 文献记录列表，策略5需要；若为None则跳过策略5
    global_mapping: dict, {变体词: 标准词}，来自已审核期刊的映射表
    global_keeps: list of set, 已知不合并的词组集合，策略1-4遇到这些词对时跳过

    返回: (variant_groups, pair_sources)
      pair_sources: dict, (word_a, word_b) → strategy_number
    """
    # 灵敏度参数
    params = {
        1: {'jaccard_threshold': 0.75, 'edit_ratio': 0.30, 'suffix_max_extra': 3,
            'contain_min': 0.30, 'contain_max_min': 0.60, 'intersect_min': 2, 'neighbor_min_co': 2},
        2: {'jaccard_threshold': 0.85, 'edit_ratio': 0.20, 'suffix_max_extra': 2,
            'contain_min': 0.40, 'contain_max_min': 0.70, 'intersect_min': 3, 'neighbor_min_co': 2},
        3: {'jaccard_threshold': 0.90, 'edit_ratio': 0.12, 'suffix_max_extra': 1,
            'contain_min': 0.50, 'contain_max_min': 0.80, 'intersect_min': 4, 'neighbor_min_co': 3},
    }
    p = params.get(sensitivity, params[2])

    keywords = list(kw_freq.keys())
    if not keywords:
        return [], {}

    # 记录每对匹配的策略来源（用于审核反馈和错误分析）
    pair_sources = {}  # (word_a, word_b) → strategy_number (sorted tuple keys)

    def _record_pair(kw1, kw2, strategy):
        """记录配对的策略来源"""
        key = tuple(sorted([kw1, kw2]))
        if key not in pair_sources:  # 保留首次匹配的策略
            pair_sources[key] = strategy

    # 用 dict 记录每个关键词所属的群组ID
    kw_to_group = {}   # keyword → group_id
    groups = {}        # group_id → set of keywords
    next_gid = [0]

    def _assign_group(kw_set):
        """将一组关键词分配到同一群组"""
        kw_list = list(kw_set)
        existing_gids = [kw_to_group[kw] for kw in kw_list if kw in kw_to_group]
        if existing_gids:
            # 合并到已有群组
            target_gid = existing_gids[0]
            for gid in existing_gids[1:]:
                if gid != target_gid:
                    groups[target_gid].update(groups[gid])
                    for k in groups[gid]:
                        kw_to_group[k] = target_gid
                    del groups[gid]
            for kw in kw_list:
                if kw not in kw_to_group:
                    kw_to_group[kw] = target_gid
                    groups[target_gid].add(kw)
        else:
            gid = next_gid[0]
            next_gid[0] += 1
            groups[gid] = set(kw_list)
            for kw in kw_list:
                kw_to_group[kw] = gid

    # ---- 策略0：全局映射预合并 ----
    # 已审核确认的同义词直接合并，标记为strategy=0，无需再次审核
    n_grouped_s0 = 0
    if global_mapping:
        print(f"  策略0: 全局映射预合并 ({len(global_mapping)} 条映射)...")
        # 构建反向索引：标准词 → 所有变体词（含自身）
        std_to_variants = defaultdict(set)
        for variant, standard in global_mapping.items():
            std_to_variants[standard].add(variant)
            std_to_variants[standard].add(standard)

        # 只合并当前数据集中存在的词
        kw_set = set(keywords)
        for standard, variants in std_to_variants.items():
            # 找出当前数据集中存在的变体
            existing = variants & kw_set
            if len(existing) >= 2:
                _assign_group(existing)
                for a in existing:
                    for b in existing:
                        if a != b:
                            _record_pair(a, b, 0)
                n_grouped_s0 += len(existing) - 1
        print(f"    全局映射: 预合并 {n_grouped_s0} 对已知同义词")

    # ---- 构建KEEP否决表 ----
    # 已审核为"不合并"的词对，策略1-4遇到时跳过
    kept_pairs = set()  # set of frozenset({word_a, word_b})
    if global_keeps:
        kw_set = set(keywords)
        n_kept = 0
        for keep_set in global_keeps:
            existing = keep_set & kw_set
            if len(existing) >= 2:
                for a in existing:
                    for b in existing:
                        if a != b:
                            kept_pairs.add(frozenset({a, b}))
                            n_kept += 1
        # 去重计数
        if kept_pairs:
            print(f"    KEEP否决: {len(kept_pairs)} 对已知非同义词将被跳过")

    def _is_kept(kw1, kw2):
        """检查该词对是否在KEEP列表中"""
        return frozenset({kw1, kw2}) in kept_pairs

    # ---- 策略1：归一化精确匹配 ----
    # 这类变体是确定性的，直接合并（但SKIP keep列表中的对）
    norm_map = defaultdict(list)
    for kw in keywords:
        norm = normalize_for_comparison(kw)
        if norm:
            norm_map[norm].append(kw)

    n_grouped_s1 = 0
    n_kept_s1 = 0
    for norm, kws in norm_map.items():
        if len(kws) > 1:
            # 过滤掉KEEP列表中的词对
            filtered_kws = []
            for kw in kws:
                # 检查该词是否与已选词在KEEP列表中
                is_kept_pair = False
                for fk in filtered_kws:
                    if _is_kept(kw, fk):
                        is_kept_pair = True
                        n_kept_s1 += 1
                        break
                if not is_kept_pair:
                    filtered_kws.append(kw)
            if len(filtered_kws) > 1:
                _assign_group(set(filtered_kws))
                n_grouped_s1 += len(filtered_kws)
                for i in range(len(filtered_kws)):
                    for j in range(i + 1, len(filtered_kws)):
                        _record_pair(filtered_kws[i], filtered_kws[j], 1)

    # ---- 策略2：严格后缀/前缀追加 ----
    # 只匹配：短词是长词的完整前缀或后缀，且多出部分是语法性后缀/前缀
    n_grouped_s2 = 0
    n_vetoed_s2 = 0
    n_kept_s2 = 0
    norm_list = [(kw, normalize_for_comparison(kw)) for kw in keywords]
    norm_list.sort(key=lambda x: len(x[1]))
    n_total = len(norm_list)
    print(f"  策略2: 严格前后缀匹配 ({n_total} keywords)...")
    for i in range(len(norm_list)):
        kw_i, norm_i = norm_list[i]
        if i % 2000 == 0 and i > 0:
            print(f"    进度: {i}/{n_total}")
        for j in range(i + 1, len(norm_list)):
            kw_j, norm_j = norm_list[j]
            if len(norm_j) - len(norm_i) > p['suffix_max_extra']:
                break
            if _is_strict_suffix_prefix_variant(norm_i, norm_j, p['suffix_max_extra']):
                # v2.4: 语义分歧检测（防止"护理学↔护理员"等通过后缀匹配）
                if is_semantic_divergence(norm_i, norm_j, kw_freq):
                    n_vetoed_s2 += 1
                    continue
                # KEEP否决：用户已审核为不合并
                if _is_kept(kw_i, kw_j):
                    n_kept_s2 += 1
                    continue
                _assign_group({kw_i, kw_j})
                _record_pair(kw_i, kw_j, 2)
                n_grouped_s2 += 1

    # ---- 策略3：字符级Jaccard相似度 ----
    # 严格限制：长度比 ≤ 1.3，且共享核心子串
    # v2.1新增：语义核心分歧检测
    n_grouped_s3 = 0
    n_vetoed_s3 = 0
    n_kept_s3 = 0
    print(f"  策略3: Jaccard相似度匹配...")
    for i in range(len(norm_list)):
        kw_i, norm_i = norm_list[i]
        if len(norm_i) < 3:
            continue
        if i % 2000 == 0 and i > 0:
            print(f"    进度: {i}/{n_total}")
        for j in range(i + 1, len(norm_list)):
            kw_j, norm_j = norm_list[j]
            if len(norm_j) < 3:
                continue
            len_min = min(len(norm_i), len(norm_j))
            len_max = max(len(norm_i), len(norm_j))
            if len_max / len_min > 1.3:
                continue
            # 首字符必须相同（防止"慢性"↔"急性"等反义词匹配）
            if norm_i[0] != norm_j[0]:
                continue
            # 必须共享至少一个2字子串（核心语义重叠）
            has_common_bigram = False
            bigrams_i = set(norm_i[k:k+2] for k in range(len(norm_i)-1))
            for k in range(len(norm_j)-1):
                if norm_j[k:k+2] in bigrams_i:
                    has_common_bigram = True
                    break
            if not has_common_bigram:
                continue
            sim = jaccard_similarity_chars(norm_i, norm_j)
            if sim >= p['jaccard_threshold']:
                # v2.1: 语义核心分歧检测
                if is_semantic_divergence(norm_i, norm_j, kw_freq):
                    n_vetoed_s3 += 1
                    continue
                # KEEP否决
                if _is_kept(kw_i, kw_j):
                    n_kept_s3 += 1
                    continue
                _assign_group({kw_i, kw_j})
                _record_pair(kw_i, kw_j, 3)
                n_grouped_s3 += 1

    # ---- 策略4：编辑距离 ----
    # 严格限制：仅1-2个字符差异，长度比 ≤ 1.2，且首字符必须相同
    # v2.1新增：语义核心分歧检测，排除"癌↔炎""护理↔心理"等概念替换
    n_grouped_s4 = 0
    n_vetoed_s4 = 0
    n_kept_s4 = 0
    print(f"  策略4: 编辑距离匹配...")
    for i in range(len(norm_list)):
        kw_i, norm_i = norm_list[i]
        if len(norm_i) < 3:
            continue
        if i % 2000 == 0 and i > 0:
            print(f"    进度: {i}/{n_total}")
        for j in range(i + 1, len(norm_list)):
            kw_j, norm_j = norm_list[j]
            if len(norm_j) < 3:
                continue
            len_min = min(len(norm_i), len(norm_j))
            len_max = max(len(norm_i), len(norm_j))
            if len_max == 0 or len_max / len_min > 1.2:
                continue
            # 首字符必须相同（防止"慢性"↔"急性"等反义词匹配）
            if norm_i[0] != norm_j[0]:
                continue
            max_ed = max(1, int(len_max * p['edit_ratio']))
            if abs(len(norm_i) - len(norm_j)) > max_ed:
                continue
            ed = edit_distance(norm_i, norm_j)
            if ed <= max_ed:
                # v2.1: 语义核心分歧检测
                if is_semantic_divergence(norm_i, norm_j, kw_freq):
                    n_vetoed_s4 += 1
                    continue
                # KEEP否决
                if _is_kept(kw_i, kw_j):
                    n_kept_s4 += 1
                    continue
                _assign_group({kw_i, kw_j})
                _record_pair(kw_i, kw_j, 4)
                n_grouped_s4 += 1

    # ---- 策略5：共现邻居验证（仅否决，不增强） ----
    # v2.3 修正：禁用增强功能（b部分）
    # 原因：第三轮审核显示S5增强功能FP率=100%（40/40）
    #   共现邻居高度重叠≠同义词，在医学领域相关概念经常共现但概念不同
    #   例：效度↔信度、焦虑↔抑郁、肺疾病↔慢性阻塞性
    # 保留否决功能（a部分）：邻居完全不重叠的配对确实不太可能是同义词
    n_grouped_s5 = 0
    n_vetoed = 0
    if records is not None:
        print(f"  策略5: 共现邻居否决（增强已禁用）...")
        neighbors = build_cooccurrence_neighbors(records, kw_freq)
        min_co = p['neighbor_min_co']

        # (a) 否决：检查策略1-4已匹配的配对，如果邻居完全不重叠则否决
        pairs_to_veto = []
        for gid, kw_set in groups.items():
            if len(kw_set) <= 1:
                continue
            kw_list = list(kw_set)
            for i in range(len(kw_list)):
                for j in range(i + 1, len(kw_list)):
                    a, b = kw_list[i], kw_list[j]
                    na = neighbors.get(a, Counter())
                    nb = neighbors.get(b, Counter())
                    if len(na) >= 5 and len(nb) >= 5:
                        _, _, intn = neighbor_containment(na, nb)
                        if intn == 0:
                            pairs_to_veto.append((a, b, gid))

        vetoed_kws = set()
        for a, b, gid in pairs_to_veto:
            if gid in groups and len(groups[gid]) > 2:
                if kw_freq[a] <= kw_freq[b]:
                    vetoed_kws.add(a)
                else:
                    vetoed_kws.add(b)
            elif gid in groups and len(groups[gid]) == 2:
                vetoed_kws.add(a)
                vetoed_kws.add(b)

        for kw in vetoed_kws:
            if kw in kw_to_group:
                gid = kw_to_group[kw]
                if gid in groups:
                    groups[gid].discard(kw)
                    if not groups[gid]:
                        del groups[gid]
                    del kw_to_group[kw]
        n_vetoed = len(pairs_to_veto)
        print(f"    否决: {n_vetoed} 对误匹配")

        # (b) 增强功能已禁用（v2.3）
        # 原增强代码：发现字面不同但邻居高度重叠的同义词
        # 审核结果证明此功能在医学领域完全不可靠
    else:
        print(f"  策略5: 跳过（未提供文献记录）")

    # 构建输出格式
    # 判断每个群组是否纯粹由策略0产生（已审核，无需再审）
    # 一个群组是"策略0纯群组"当且仅当：其所有配对都来自策略0
    s0_pairs = set()  # 策略0产生的配对集合
    if global_mapping:
        for key, strat in pair_sources.items():
            if strat == 0:
                s0_pairs.add(key)

    variant_groups = []
    group_id = 0

    # 先处理有群组的关键词
    for gid, kw_set in groups.items():
        if len(kw_set) == 1:
            kw = list(kw_set)[0]
            variant_groups.append({
                'group_id': group_id,
                'variants': [(kw, kw_freq[kw])],
                'suggested_standard': kw,
                'has_variants': False,
                'from_global_mapping': False,
            })
        else:
            sorted_variants = sorted(kw_set, key=lambda x: -kw_freq[x])
            # v2.4: 确定建议标准词
            # 规则：如果群组中存在"XX学"和"XX"对，优先选"XX学"作为标准词
            # 用户要求：所有XX↔XX学统一采用XX学的规范表达
            suggested = sorted_variants[0]  # 默认取频次最高的
            for kw in sorted_variants:
                if kw.endswith('学') or kw.endswith('法'):
                    # 检查群组中是否有去掉"学/法"后缀的对应词
                    base = kw[:-1]
                    if base in kw_set:
                        suggested = kw
                        break

            # 判断该群组是否纯粹来自策略0（所有配对都是策略0）
            all_s0 = True
            variants_list = list(kw_set)
            for i in range(len(variants_list)):
                for j in range(i + 1, len(variants_list)):
                    key = tuple(sorted([variants_list[i], variants_list[j]]))
                    if key not in s0_pairs:
                        all_s0 = False
                        break
                if not all_s0:
                    break

            variant_groups.append({
                'group_id': group_id,
                'variants': [(kw, kw_freq[kw]) for kw in sorted_variants],
                'suggested_standard': suggested,
                'has_variants': True,
                'from_global_mapping': all_s0,  # 策略0纯群组，已审核无需再审
            })
        group_id += 1

    # 处理未分组的关键词
    grouped_kws = set(kw_to_group.keys())
    for kw in keywords:
        if kw not in grouped_kws:
            variant_groups.append({
                'group_id': group_id,
                'variants': [(kw, kw_freq[kw])],
                'suggested_standard': kw,
                'has_variants': False,
                'from_global_mapping': False,
            })
            group_id += 1

    # 排序：有变体的组在前，组内按最高频次降序
    variant_groups.sort(key=lambda g: (-g['has_variants'], -g['variants'][0][1]))

    # 重新编号
    for i, g in enumerate(variant_groups):
        g['group_id'] = i

    n_with_variants = sum(1 for g in variant_groups if g['has_variants'])
    n_variants_total = sum(len(g['variants']) for g in variant_groups if g['has_variants'])
    n_kept_total = n_kept_s1 + n_kept_s2 + n_kept_s3 + n_kept_s4
    print(f"\n  变体检测完成:")
    print(f"    策略0(全局映射): 预合并 {n_grouped_s0} 对已知同义词")
    print(f"    策略1(归一化匹配): 合并 {n_grouped_s1} 个关键词" +
          (f", KEEP否决 {n_kept_s1} 对" if n_kept_s1 else ""))
    print(f"    策略2(严格前后缀): 新增合并 {n_grouped_s2} 对, 语义分歧否决 {n_vetoed_s2} 对" +
          (f", KEEP否决 {n_kept_s2} 对" if n_kept_s2 else ""))
    print(f"    策略3(Jaccard相似): 新增合并 {n_grouped_s3} 对, 语义分歧否决 {n_vetoed_s3} 对" +
          (f", KEEP否决 {n_kept_s3} 对" if n_kept_s3 else ""))
    print(f"    策略4(编辑距离): 新增合并 {n_grouped_s4} 对, 语义分歧否决 {n_vetoed_s4} 对" +
          (f", KEEP否决 {n_kept_s4} 对" if n_kept_s4 else ""))
    print(f"    策略5(共现邻居): 否决 {n_vetoed} 对误匹配, 新增合并 {n_grouped_s5} 对")
    if n_kept_total:
        print(f"    KEEP否决合计: {n_kept_total} 对（来自全局keep列表）")
    print(f"    共 {n_with_variants} 个变体群组，涉及 {n_variants_total} 个关键词")
    print(f"    配对策略溯源: {len(pair_sources)} 对")

    return variant_groups, pair_sources


def _is_strict_suffix_prefix_variant(norm1, norm2, max_extra=2):
    """
    严格判断两个归一化字符串是否为后缀/前缀变体。

    核心规则：短词必须是长词的**完整前缀或完整后缀**，
    且多出的部分必须是**语法性后缀/前缀**（如"学""研究"），
    而非领域词汇（如"护理""管理""康复"）。

    额外约束：
    - 短词长度必须 ≥ 3字符（2字词太容易成为其他词的前缀，如"护理"）
    - 多出部分长度不超过短词长度的50%（防止"护理"+"研究"被匹配）

    正例：循证护理 ↔ 循证护理学（多出"学"，1字 ≤ 4*50%=2）
    反例：护理 ↔ 循证护理（多出"循证"，不是语法前缀）
    反例：护理 ↔ 护理研究（短词仅2字，太短）
    """
    if norm1 == norm2:
        return True
    if len(norm1) == 0 or len(norm2) == 0:
        return False

    # 确定短词和长词
    if len(norm1) <= len(norm2):
        short, long = norm1, norm2
    else:
        short, long = norm2, norm1

    # 短词必须 ≥ 3字符（中文2字词如"护理""管理"太容易误匹配）
    if len(short) < 3:
        return False

    # 情况1：short 是 long 的前缀（long = short + suffix）
    if long.startswith(short):
        extra = long[len(short):]
        extra_len = len(extra)
        if extra_len == 0 or extra_len > max_extra:
            return False
        # 多出部分不超过短词长度的50%
        if extra_len > len(short) * 0.5:
            return False
        if _is_grammatical_suffix(extra):
            return True

    # 情况2：short 是 long 的后缀（long = prefix + short）
    if long.endswith(short):
        extra = long[:len(long)-len(short)]
        extra_len = len(extra)
        if extra_len == 0 or extra_len > max_extra:
            return False
        if extra_len > len(short) * 0.5:
            return False
        if _is_grammatical_prefix(extra):
            return True

    return False


def _is_grammatical_suffix(extra):
    """判断多出的部分是否为语法性后缀"""
    # v2.4: 改为精确匹配，不再用endswith
    # 原因：endswith导致"教学"匹配"学"后缀，但"教学"≠"学"
    # 例：仿真模拟教学 ≠ 仿真模拟 + 语法后缀
    return extra in GRAMMATICAL_SUFFIXES


def _is_grammatical_prefix(extra):
    """判断多出的部分是否为语法性前缀"""
    for prefix in GRAMMATICAL_PREFIXES:
        if extra == prefix or extra.startswith(prefix):
            return True
    return False


# ============================================================
# 第四步：映射表构建与应用
# ============================================================

def build_mapping_from_groups(variant_groups):
    """
    从变体群组自动构建映射表（变体→建议标准词）。
    仅包含有变体的群组。
    """
    mapping = {}
    for g in variant_groups:
        if not g['has_variants']:
            continue
        standard = g['suggested_standard']
        for kw, _ in g['variants']:
            if kw != standard:
                mapping[kw] = standard
    return mapping


def load_mapping_table(filepath):
    """加载用户审核后的映射表CSV"""
    mapping = {}
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            variant = row.get('变体', '').strip()
            standard = row.get('标准词', '').strip()
            if variant and standard and variant != standard:
                mapping[variant] = standard
    return mapping


def apply_mapping(records, mapping):
    """
    应用映射表到记录的关键词列表。
    返回更新后的records和新的频次Counter。
    """
    kw_freq = Counter()
    for rec in records:
        standardized = []
        for kw in rec.get('keywords_cleaned', []):
            std = mapping.get(kw, kw)
            standardized.append(std)
        # 去重（映射后可能出现重复）
        standardized = list(dict.fromkeys(standardized))
        rec['keywords_standardized'] = standardized
        kw_freq.update(standardized)
    return records, kw_freq


# ============================================================
# 第五步：导出
# ============================================================

def safe_csv_writer(filepath, rows, fieldnames):
    """安全写入CSV（utf-8-sig编码，兼容Excel）"""
    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def export_raw_keywords(records, output_dir):
    """导出原始关键词列表（长格式：每行一条记录-关键词对）"""
    rows = []
    for rec in records:
        for kw in rec.get('keywords_raw', []):
            rows.append({
                '记录ID': rec['record_id'],
                '年份': rec['year'],
                '标题': rec['title'],
                '原始关键词': kw,
                '清洗后关键词': clean_keyword(kw),
            })
    filepath = os.path.join(output_dir, '1_原始关键词列表.csv')
    safe_csv_writer(filepath, rows, ['记录ID', '年份', '标题', '原始关键词', '清洗后关键词'])
    print(f"  → {filepath} ({len(rows)} 行)")
    return filepath


def export_frequency_table(freq_rows, output_dir, prefix='2'):
    """导出频次表"""
    filepath = os.path.join(output_dir, f'{prefix}_关键词频次表.csv')
    safe_csv_writer(filepath, freq_rows,
                    ['关键词', '频次', '占关键词总数%', '累积%', '出现论文数', '占论文总数%'])
    print(f"  → {filepath} ({len(freq_rows)} 个关键词)")
    return filepath


def export_variant_groups(variant_groups, output_dir, pair_sources=None):
    """导出变体群组（供用户审核），含策略溯源信息
    策略0纯群组（from_global_mapping=True）自动标记为已确认，不进入待审核列表
    """
    rows = []
    n_s0_auto_confirmed = 0
    for g in variant_groups:
        if not g['has_variants']:
            continue
        # 策略0纯群组：已审核，自动确认，不进入待审核CSV
        if g.get('from_global_mapping'):
            n_s0_auto_confirmed += 1
            continue
        # 收集该群组内所有配对的策略来源
        group_strategies = set()
        variants = [kw for kw, _ in g['variants']]
        for i in range(len(variants)):
            for j in range(i + 1, len(variants)):
                key = tuple(sorted([variants[i], variants[j]]))
                if pair_sources and key in pair_sources:
                    group_strategies.add(pair_sources[key])
        strategy_str = ','.join(str(s) for s in sorted(group_strategies)) if group_strategies else ''

        for kw, freq in g['variants']:
            rows.append({
                '群组ID': g['group_id'],
                '关键词': kw,
                '频次': freq,
                '建议标准词': g['suggested_standard'],
                '是否建议标准词': '是' if kw == g['suggested_standard'] else '',
                '匹配策略': strategy_str,
                '审核结果': '',  # 用户填写：保留/合并/删除
                '指定标准词': '',  # 用户填写：自定义标准词
            })
    filepath = os.path.join(output_dir, '3_变体群组_待审核.csv')
    safe_csv_writer(filepath, rows,
                    ['群组ID', '关键词', '频次', '建议标准词', '是否建议标准词',
                     '匹配策略', '审核结果', '指定标准词'])
    n_to_review = sum(1 for g in variant_groups if g['has_variants'] and not g.get('from_global_mapping'))
    print(f"  → {filepath} ({len(rows)} 行，{n_to_review} 个待审核群组)")
    if n_s0_auto_confirmed:
        print(f"    策略0自动确认: {n_s0_auto_confirmed} 个群组（来自全局映射表，无需审核）")
    return filepath


def export_mapping_template(variant_groups, output_dir):
    """导出映射表模板（供用户编辑），策略0群组已自动确认，不进入待审核"""
    rows = []
    for g in variant_groups:
        if not g['has_variants']:
            continue
        # 策略0纯群组不进入待审核
        if g.get('from_global_mapping'):
            continue
        standard = g['suggested_standard']
        for kw, freq in g['variants']:
            if kw != standard:
                rows.append({
                    '变体': kw,
                    '标准词': standard,
                    '频次': freq,
                    '用户确认': '',  # 用户填写：是/否/修改
                    '修改后标准词': '',  # 用户填写
                })
    filepath = os.path.join(output_dir, '4_映射表_待审核.csv')
    safe_csv_writer(filepath, rows,
                    ['变体', '标准词', '频次', '用户确认', '修改后标准词'])
    print(f"  → {filepath} ({len(rows)} 条映射)")
    return filepath


def export_mapping_auto(mapping, output_dir):
    """导出自动生成的映射表（无需审核，直接可用）"""
    rows = []
    for variant, standard in sorted(mapping.items()):
        rows.append({
            '变体': variant,
            '标准词': standard,
        })
    filepath = os.path.join(output_dir, '4_映射表_自动.csv')
    safe_csv_writer(filepath, rows, ['变体', '标准词'])
    print(f"  → {filepath} ({len(rows)} 条映射)")
    return filepath


def export_standardized_keywords(records, output_dir):
    """导出标准化后的关键词列表"""
    rows = []
    for rec in records:
        for kw in rec.get('keywords_standardized', []):
            rows.append({
                '记录ID': rec['record_id'],
                '年份': rec['year'],
                '标题': rec['title'],
                '标准化关键词': kw,
            })
    filepath = os.path.join(output_dir, '5_标准化关键词列表.csv')
    safe_csv_writer(filepath, rows, ['记录ID', '年份', '标题', '标准化关键词'])
    print(f"  → {filepath} ({len(rows)} 行)")
    return filepath


def export_cooccurrence_ready(records, output_dir):
    """
    导出共现分析就绪格式：
    每行一条记录，关键词用分号分隔
    """
    rows = []
    for rec in records:
        kws = rec.get('keywords_standardized', rec.get('keywords_cleaned', []))
        rows.append({
            '记录ID': rec['record_id'],
            '年份': rec['year'],
            '标题': rec['title'],
            '关键词（分号分隔）': ';'.join(kws),
            '关键词数': len(kws),
        })
    filepath = os.path.join(output_dir, '6_共现分析就绪.csv')
    safe_csv_writer(filepath, rows,
                    ['记录ID', '年份', '标题', '关键词（分号分隔）', '关键词数'])
    print(f"  → {filepath} ({len(rows)} 条记录)")
    return filepath


def export_summary(records, kw_freq, variant_groups, mapping, output_dir):
    """导出分析摘要"""
    filepath = os.path.join(output_dir, '分析摘要.txt')
    n_with_variants = sum(1 for g in variant_groups if g['has_variants'])
    n_variants_kw = sum(len(g['variants']) for g in variant_groups if g['has_variants'])
    years = sorted(set(rec['year'] for rec in records if rec['year']))
    n_no_kw = sum(1 for rec in records if not rec.get('keywords_cleaned'))

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("=" * 60 + "\n")
        f.write("  关键词清洗与标准化工具 - 分析摘要\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"文献记录总数: {len(records)}\n")
        f.write(f"年份范围: {years[0]}-{years[-1]}\n" if years else "年份范围: 无\n")
        f.write(f"无关键词记录数: {n_no_kw}\n")
        f.write(f"清洗后唯一关键词数: {len(kw_freq)}\n")
        f.write(f"关键词总频次: {sum(kw_freq.values())}\n")
        f.write(f"变体群组数: {n_with_variants}\n")
        f.write(f"涉及变体的关键词数: {n_variants_kw}\n")
        f.write(f"映射表条目数: {len(mapping)}\n\n")
        f.write("Top-20 高频关键词:\n")
        for kw, freq in kw_freq.most_common(20):
            f.write(f"  {kw}: {freq}\n")
        f.write(f"\n输出文件:\n")
        for fname in sorted(os.listdir(output_dir)):
            if fname.endswith('.csv') or fname.endswith('.txt'):
                f.write(f"  {fname}\n")

    print(f"  → {filepath}")
    return filepath


# ============================================================
# 全局映射表
# ============================================================

def load_global_mapping(filepath):
    """加载全局映射表CSV文件，返回 {变体词: 标准词} 字典"""
    mapping = {}
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            variant_col = 0
            standard_col = 1
            if header:
                header_lower = [h.strip().lower() for h in header]
                for i, h in enumerate(header_lower):
                    if h in ('变体', 'variant', '原词', '关键词'):
                        variant_col = i
                    elif h in ('标准词', 'standard', '标准', '映射词'):
                        standard_col = i
            for row in reader:
                if len(row) > max(variant_col, standard_col):
                    variant = row[variant_col].strip()
                    standard = row[standard_col].strip()
                    if variant and standard and variant != standard:
                        mapping[variant] = standard
    except Exception as e:
        print(f"  ERROR loading global mapping: {e}", file=sys.stderr)
        return None
    return mapping


def load_global_keeps(filepath):
    """加载全局KEEP列表文件，返回 [set(...), set(...)] 列表
    文件格式：每行一个KEEP群组，逗号分隔的词列表
    """
    keeps = []
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                words = [w.strip() for w in line.split(',') if w.strip()]
                if len(words) >= 2:
                    keeps.append(set(words))
    except Exception as e:
        print(f"  ERROR loading global keeps: {e}", file=sys.stderr)
        return []
    return keeps


def auto_find_keep_file(mapping_path):
    """根据mapping文件路径自动查找keep文件"""
    base, ext = os.path.splitext(mapping_path)
    for candidate in [base + '_keep' + ext, base + '.keep' + ext]:
        if os.path.exists(candidate):
            return candidate
    return None


# ============================================================
# 主函数
# ============================================================

def run_analysis(input_path, output_dir, sensitivity=2, freq_only=False,
                 global_mapping_path=None, global_keep_path=None):
    """完整分析流程"""
    os.makedirs(output_dir, exist_ok=True)

    # 加载全局映射表
    global_mapping = None
    if global_mapping_path:
        global_mapping = load_global_mapping(global_mapping_path)
        if global_mapping:
            print(f"  全局映射表: {len(global_mapping)} 条映射 (from {os.path.basename(global_mapping_path)})")
        else:
            print(f"  WARNING: 全局映射表加载失败，将跳过策略0")

    # 加载全局KEEP列表
    global_keeps = None
    if global_keep_path:
        global_keeps = load_global_keeps(global_keep_path)
        if global_keeps:
            print(f"  全局KEEP列表: {len(global_keeps)} 个群组 (from {os.path.basename(global_keep_path)})")
    elif global_mapping_path:
        # 自动查找同名keep文件
        auto_keep = auto_find_keep_file(global_mapping_path)
        if auto_keep:
            global_keeps = load_global_keeps(auto_keep)
            if global_keeps:
                print(f"  全局KEEP列表: {len(global_keeps)} 个群组 (auto-detected: {os.path.basename(auto_keep)})")

    print("=" * 60)
    print("  关键词清洗与标准化工具 v2.5")
    print("=" * 60)

    # Step 1: 解析XML
    print(f"\n[Step 1] 解析万方XML文件")
    records = parse_input(input_path)
    total_records = len(records)
    print(f"  共 {total_records} 条文献记录")
    if not records:
        print("ERROR: 未解析到任何记录", file=sys.stderr)
        return None

    # Step 2: 清洗关键词
    print(f"\n[Step 2] 清洗关键词")
    records, kw_freq = extract_and_clean_keywords(records)
    n_unique = len(kw_freq)
    n_total = sum(kw_freq.values())
    n_no_kw = sum(1 for rec in records if not rec.get('keywords_cleaned'))
    print(f"  清洗后唯一关键词: {n_unique}")
    print(f"  关键词总频次: {n_total}")
    if n_no_kw:
        print(f"  WARNING: {n_no_kw} 条记录无关键词")

    # 导出原始关键词列表
    print(f"\n[Step 3] 导出原始数据")
    export_raw_keywords(records, output_dir)

    # 频次表
    freq_rows = build_frequency_table(kw_freq, total_records)
    export_frequency_table(freq_rows, output_dir)

    if freq_only:
        print("\n  --freq-only 模式，跳过变体检测")
        # 仍然导出共现分析就绪格式（用清洗后关键词）
        for rec in records:
            rec['keywords_standardized'] = rec.get('keywords_cleaned', [])
        export_cooccurrence_ready(records, output_dir)
        print("\n分析完成！")
        return records, kw_freq, [], {}, {}

    # Step 4: 变体检测
    print(f"\n[Step 4] 变体检测（灵敏度={sensitivity}）")
    variant_groups, pair_sources = detect_variants(kw_freq, sensitivity, records=records,
                                                    global_mapping=global_mapping,
                                                    global_keeps=global_keeps)

    # 导出变体群组（含策略溯源）
    export_variant_groups(variant_groups, output_dir, pair_sources)

    # 导出配对策略溯源（供审核分析使用）
    if pair_sources:
        pair_rows = []
        for (wa, wb), strategy in sorted(pair_sources.items()):
            pair_rows.append({
                '词A': wa, '词B': wb,
                '策略': strategy,
                '策略名称': {1: '归一化匹配', 2: '前后缀', 3: 'Jaccard',
                            4: '编辑距离', 5: '共现邻居'}.get(strategy, '?'),
            })
        pair_path = os.path.join(output_dir, '3_配对策略溯源.csv')
        safe_csv_writer(pair_path, pair_rows, ['词A', '词B', '策略', '策略名称'])
        print(f"  → {pair_path} ({len(pair_rows)} 对)")

    # 构建自动映射表
    mapping = build_mapping_from_groups(variant_groups)
    export_mapping_template(variant_groups, output_dir)
    export_mapping_auto(mapping, output_dir)

    # Step 5: 应用映射表
    print(f"\n[Step 5] 应用映射表")
    records, std_freq = apply_mapping(records, mapping)
    print(f"  标准化后唯一关键词: {len(std_freq)}")
    print(f"  标准化后关键词总频次: {sum(std_freq.values())}")

    # 导出标准化结果
    export_standardized_keywords(records, output_dir)

    # 标准化频次表
    std_freq_rows = build_frequency_table(std_freq, total_records)
    export_frequency_table(std_freq_rows, output_dir, prefix='5')

    # 共现分析就绪格式
    export_cooccurrence_ready(records, output_dir)

    # 分析摘要
    export_summary(records, kw_freq, variant_groups, mapping, output_dir)

    # Step 6: 清洗效果检验
    print(f"\n[Step 6] 清洗效果检验")
    validation = validate_cleaning_effect(kw_freq, std_freq, mapping, records, variant_groups)
    export_validation_report(validation, output_dir)

    print("\n" + "=" * 60)
    print("  分析完成！输出文件清单")
    print("=" * 60)
    for f in sorted(os.listdir(output_dir)):
        if f.endswith(('.csv', '.txt')):
            fpath = os.path.join(output_dir, f)
            size_kb = os.path.getsize(fpath) / 1024
            print(f"  {f} ({size_kb:.1f} KB)")

    return records, kw_freq, variant_groups, mapping, pair_sources


def validate_cleaning_effect(kw_freq_before, kw_freq_after, mapping, records, variant_groups):
    """
    清洗效果检验：从有效性和效率两个维度量化评估。
    
    有效性指标：
    1. 关键词缩减率 = (原始词数 - 标准化词数) / 原始词数
    2. 频次增益 = 标准化后Top-N关键词频次之和 / 清洗前对应词频次之和
    3. 共现强度增益 = 标准化后共现对权重之和 / 清洗前共现对权重之和
    4. 孤立节点减少率 = (清洗前孤立词数 - 标准化后孤立词数) / 清洗前孤立词数
    
    效率指标：
    5. 审核工作量 = 需人工审核的群组数
    6. 自动合并率 = 策略0(全局映射)+策略1(归一化)合并的词数 / 总合并词数
    """
    from itertools import combinations
    
    validation = {}
    
    # ── 1. 关键词缩减率 ──
    n_before = len(kw_freq_before)
    n_after = len(kw_freq_after)
    reduction_rate = (n_before - n_after) / n_before if n_before > 0 else 0
    validation['关键词数_清洗前'] = n_before
    validation['关键词数_标准化后'] = n_after
    validation['缩减词数'] = n_before - n_after
    validation['缩减率'] = f'{reduction_rate:.1%}'
    print(f"  关键词缩减: {n_before} → {n_after} (缩减 {n_before - n_after}, 率={reduction_rate:.1%})")
    
    # ── 2. 频次增益（Top-10/20/50） ──
    # 核心问题：清洗前同一概念分散在多个变体中，研究者通常只看到最高频变体
    # 频次增益 = 标准化后标准词频次 / 清洗前该概念最高频变体的频次
    # 例如：循证护理(90)+循证护理学(40) → 循证护理学(130)
    #   增益 = 130/90 = 1.44（清洗前只看到"循证护理"90次，实际该概念出现130次）
    # 
    # 直接从kw_freq_before计算标准化后频次（不依赖records，避免数据不一致）
    std_freq_from_before = defaultdict(int)
    for w, f in kw_freq_before.items():
        std_word = mapping.get(w, w)
        std_freq_from_before[std_word] += f
    
    before_top = sorted(kw_freq_before.items(), key=lambda x: -x[1])
    
    for top_n in [10, 20, 50]:
        if len(before_top) < top_n:
            continue
        
        # 收集Top-N词对应的标准词集合
        std_to_max_variant_freq = {}  # 标准词 → 清洗前最高频变体的频次
        for w, f in before_top[:top_n]:
            std_word = mapping.get(w, w)
            if std_word not in std_to_max_variant_freq or f > std_to_max_variant_freq[std_word]:
                std_to_max_variant_freq[std_word] = f
        
        # 频次增益 = Σ(标准词频次) / Σ(最高频变体频次)
        freq_max_variant_sum = sum(std_to_max_variant_freq.values())
        freq_after_sum = sum(std_freq_from_before.get(std, 0) for std in std_to_max_variant_freq)
        
        gain = freq_after_sum / freq_max_variant_sum if freq_max_variant_sum > 0 else 1.0
        validation[f'Top{top_n}_频次增益'] = f'{gain:.3f}'
        print(f"  Top-{top_n} 频次增益: {freq_max_variant_sum} → {freq_after_sum} (×{gain:.3f})")
    
    # ── 3. 共现强度增益 ──
    def build_cooccurrence(kw_lists):
        """从关键词列表构建共现对权重"""
        cooc = Counter()
        for kws in kw_lists:
            for a, b in combinations(sorted(set(kws)), 2):
                cooc[(a, b)] += 1
        return cooc
    
    # 清洗前共现
    kw_lists_before = [rec.get('keywords_cleaned', []) for rec in records]
    cooc_before = build_cooccurrence(kw_lists_before)
    
    # 标准化后共现
    kw_lists_after = [rec.get('keywords_standardized', []) for rec in records]
    cooc_after = build_cooccurrence(kw_lists_after)
    
    total_weight_before = sum(cooc_before.values())
    total_weight_after = sum(cooc_after.values())
    cooc_gain = total_weight_after / total_weight_before if total_weight_before > 0 else 1.0
    
    validation['共现对数_清洗前'] = len(cooc_before)
    validation['共现对数_标准化后'] = len(cooc_after)
    validation['共现总权重_清洗前'] = total_weight_before
    validation['共现总权重_标准化后'] = total_weight_after
    validation['共现强度增益'] = f'{cooc_gain:.3f}'
    print(f"  共现强度: {len(cooc_before)}对/{total_weight_before}权重 → "
          f"{len(cooc_after)}对/{total_weight_after}权重 (×{cooc_gain:.3f})")
    
    # ── 4. 孤立节点减少率 ──
    # 清洗前：只出现1次且不与任何词共现的词
    nodes_before = set(kw_freq_before.keys())
    connected_before = set()
    for a, b in cooc_before:
        connected_before.add(a)
        connected_before.add(b)
    isolated_before = nodes_before - connected_before
    
    nodes_after = set(kw_freq_after.keys())
    connected_after = set()
    for a, b in cooc_after:
        connected_after.add(a)
        connected_after.add(b)
    isolated_after = nodes_after - connected_after
    
    iso_reduction = (len(isolated_before) - len(isolated_after)) / len(isolated_before) if len(isolated_before) > 0 else 0
    validation['孤立词数_清洗前'] = len(isolated_before)
    validation['孤立词数_标准化后'] = len(isolated_after)
    validation['孤立词减少率'] = f'{iso_reduction:.1%}'
    print(f"  孤立词: {len(isolated_before)} → {len(isolated_after)} (减少率={iso_reduction:.1%})")
    
    # ── 5. 合并详情 ──
    n_merged_groups = sum(1 for g in variant_groups if g['has_variants'])
    n_merged_words = sum(len(g['variants']) for g in variant_groups if g['has_variants'])
    n_auto_merged = 0  # 策略0+策略1合并的
    for g in variant_groups:
        if g['has_variants']:
            # 如果suggested_standard与所有变体的归一化形式相同，说明是策略1
            std = g['suggested_standard']
            for v, freq in g['variants']:
                if v != std and normalize_for_comparison(v) == normalize_for_comparison(std):
                    n_auto_merged += 1
                    break
    
    validation['合并群组数'] = n_merged_groups
    validation['涉及词数'] = n_merged_words
    validation['需审核群组数'] = n_merged_groups  # 实际需审核 = 总群组 - 策略0预合并
    print(f"  合并群组: {n_merged_groups}, 涉及词: {n_merged_words}")
    
    # ── 6. 频次提升典型案例 ──
    # 找出频次增益最大的词（标准化后频次/清洗前最大变体频次）
    gain_cases = []
    for g in variant_groups:
        if not g['has_variants']:
            continue
        std = g['suggested_standard']
        std_freq = kw_freq_after.get(std, 0)
        max_variant_freq = max(f for v, f in g['variants'])
        if max_variant_freq > 0 and std_freq > max_variant_freq:
            variants_str = '/'.join(v for v, f in g['variants'] if v != std)
            gain_cases.append({
                '标准词': std,
                '变体': variants_str,
                '最大变体频次': max_variant_freq,
                '标准词频次': std_freq,
                '频次增益': f'{std_freq/max_variant_freq:.2f}',
            })
    
    gain_cases.sort(key=lambda x: -float(x['频次增益']))
    validation['频次增益案例'] = gain_cases[:20]  # Top 20
    if gain_cases:
        top = gain_cases[0]
        print(f"  最大频次增益: {top['标准词']}({top['最大变体频次']}→{top['标准词频次']}, ×{top['频次增益']})")
    
    return validation


def export_validation_report(validation, output_dir):
    """导出清洗效果检验报告"""
    filepath = os.path.join(output_dir, '7_清洗效果检验.csv')
    
    rows = []
    for key, val in validation.items():
        if key == '频次增益案例':
            continue
        rows.append({'指标': key, '值': val})
    
    safe_csv_writer(filepath, rows, ['指标', '值'])
    print(f"  → {filepath}")
    
    # 导出频次增益案例
    if validation.get('频次增益案例'):
        case_path = os.path.join(output_dir, '7_频次增益案例.csv')
        safe_csv_writer(case_path, validation['频次增益案例'],
                        ['标准词', '变体', '最大变体频次', '标准词频次', '频次增益'])
        print(f"  → {case_path}")


def run_apply_mapping(input_path, mapping_file, output_dir):
    """应用已有映射表"""
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("  关键词清洗与标准化工具 - 应用映射表模式")
    print("=" * 60)

    # 解析
    print(f"\n[Step 1] 解析万方XML文件")
    records = parse_input(input_path)
    print(f"  共 {len(records)} 条文献记录")

    # 清洗
    print(f"\n[Step 2] 清洗关键词")
    records, kw_freq = extract_and_clean_keywords(records)
    print(f"  清洗后唯一关键词: {len(kw_freq)}")

    # 加载映射表
    print(f"\n[Step 3] 加载映射表: {mapping_file}")
    mapping = load_mapping_table(mapping_file)
    print(f"  映射条目: {len(mapping)}")

    # 统计命中
    hit = sum(1 for kw in kw_freq if kw in mapping)
    print(f"  命中关键词: {hit}/{len(kw_freq)}")

    # 应用
    print(f"\n[Step 4] 应用映射表")
    records, std_freq = apply_mapping(records, mapping)
    print(f"  标准化后唯一关键词: {len(std_freq)}")

    # 导出
    export_raw_keywords(records, output_dir)
    freq_rows = build_frequency_table(kw_freq, len(records))
    export_frequency_table(freq_rows, output_dir)
    export_standardized_keywords(records, output_dir)
    std_freq_rows = build_frequency_table(std_freq, len(records))
    export_frequency_table(std_freq_rows, output_dir, prefix='5')
    export_cooccurrence_ready(records, output_dir)

    print("\n应用完成！")
    return records, std_freq


def main():
    parser = argparse.ArgumentParser(
        description='关键词清洗与标准化工具 v2.5',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 完整分析
  python keyword_cleaner.py -i data/ -o output/

  # 仅频次统计
  python keyword_cleaner.py -i data/ -o output/ --freq-only

  # 应用已有映射表
  python keyword_cleaner.py -i data/ -m mapping.csv -o output/

  # 使用全局映射+keep列表（自动查找同名keep文件）
  python keyword_cleaner.py -i data/ -o output/ -g global_mapping.csv

  # 显式指定keep文件
  python keyword_cleaner.py -i data/ -o output/ -g global_mapping.csv --global-keep global_mapping_keep.csv

  # 调整灵敏度
  python keyword_cleaner.py -i data/ -o output/ --sensitivity 1
        """)
    parser.add_argument('--input', '-i', required=True,
                        help='输入XML文件或目录路径')
    parser.add_argument('--output', '-o', default=None,
                        help='输出目录（默认: 输入路径/output）')
    parser.add_argument('--mapping', '-m', default=None,
                        help='映射表CSV文件路径（应用模式）')
    parser.add_argument('--global-mapping', '-g', default=None,
                        help='全局映射表CSV文件路径（预合并已知同义词，减少审核量）')
    parser.add_argument('--global-keep', default=None,
                        help='全局KEEP列表CSV文件路径（已知非同义词，阻止重复匹配）')
    parser.add_argument('--freq-only', action='store_true',
                        help='仅做频次统计，不做变体检测')
    parser.add_argument('--sensitivity', type=int, default=2, choices=[1, 2, 3],
                        help='变体检测灵敏度: 1=宽松 2=默认 3=严格')
    args = parser.parse_args()

    # 输出目录
    output_dir = args.output
    if not output_dir:
        if os.path.isdir(args.input):
            output_dir = os.path.join(args.input, 'output')
        else:
            output_dir = os.path.join(os.path.dirname(os.path.abspath(args.input)), 'output')

    if args.mapping:
        run_apply_mapping(args.input, args.mapping, output_dir)
    else:
        run_analysis(args.input, output_dir, args.sensitivity, args.freq_only,
                     global_mapping_path=args.global_mapping,
                     global_keep_path=args.global_keep)


if __name__ == '__main__':
    main()
