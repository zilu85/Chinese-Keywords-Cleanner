#!/usr/bin/env python3
"""
审核记录分析工具 v1.0
====================
基于审核记录(JSONL)进行错误分析和规则优化建议。
可独立于GUI运行，适合批量分析和迭代优化。

用法：
  # 生成错误分析报告
  python review_analyzer.py -i output/

  # 导出审核数据为训练格式（供深度学习使用）
  python review_analyzer.py -i output/ --export-training

  # 应用规则修正建议（自动调整阈值）
  python review_analyzer.py -i output/ --apply-suggestions
"""

import os
import sys
import json
import argparse
from datetime import datetime
from collections import defaultdict, Counter


STRATEGY_NAMES = {
    1: 'S1-归一化匹配',
    2: 'S2-前后缀',
    3: 'S3-Jaccard',
    4: 'S4-编辑距离',
    5: 'S5-共现邻居',
    0: '未知',
}


def load_review_records(input_dir):
    """加载审核记录JSONL"""
    log_path = os.path.join(input_dir, '审核记录.jsonl')
    if not os.path.exists(log_path):
        print(f"ERROR: No review records found at {log_path}", file=sys.stderr)
        return []

    records = []
    with open(log_path, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"Warning: Invalid JSON at line {line_num}: {e}", file=sys.stderr)

    return records


def extract_pair_records(records):
    """从群组审核记录中提取配对级别数据"""
    all_pairs = []
    for rec in records:
        for pd in rec.get('pair_details', []):
            pd['group_id'] = rec.get('group_id')
            pd['timestamp'] = rec.get('timestamp', '')
            all_pairs.append(pd)
    return all_pairs


def compute_strategy_stats(pairs):
    """按策略统计错误率"""
    stats = {}
    for s in range(1, 6):
        pairs_s = [p for p in pairs if p['strategy'] == s]
        if not pairs_s:
            continue
        n_total = len(pairs_s)
        n_fp = sum(1 for p in pairs_s if p['error_type'] == 'false_positive')
        n_correct = sum(1 for p in pairs_s if p['error_type'] == 'correct')
        stats[s] = {
            'name': STRATEGY_NAMES[s],
            'total': n_total,
            'correct': n_correct,
            'fp': n_fp,
            'fp_rate': n_fp / n_total * 100 if n_total > 0 else 0,
            'precision': n_correct / n_total * 100 if n_total > 0 else 0,
            'fp_examples': [p for p in pairs_s if p['error_type'] == 'false_positive'][:20],
        }
    return stats


def cluster_error_patterns(fp_pairs):
    """对误匹配配对进行模式聚类"""
    patterns = defaultdict(list)

    # 反义词前缀对
    antonym_pairs = [
        ('急', '慢'), ('内', '外'), ('上', '下'), ('前', '后'),
        ('左', '右'), ('大', '小'), ('高', '低'), ('长', '短'),
        ('阳', '阴'), ('正', '负'), ('主', '次'), ('早', '晚'),
        ('深', '浅'), ('重', '轻'), ('强', '弱'), ('快', '慢'),
        ('多', '少'), ('新', '旧'), ('开', '闭'), ('热', '冷'),
    ]

    # 器官字符
    organ_chars = ['肺', '肝', '肾', '心', '脑', '胃', '肠', '骨',
                   '血', '皮', '眼', '耳', '口', '颈', '胸', '腹',
                   '脾', '胆', '胰', '喉', '鼻', '舌', '齿']

    for p in fp_pairs:
        wa, wb = p['word_a'], p['word_b']
        classified = False

        # 1. 反义词前缀
        for a_char, b_char in antonym_pairs:
            if (wa.startswith(a_char) and wb.startswith(b_char)) or \
               (wa.startswith(b_char) and wb.startswith(a_char)):
                patterns['反义词前缀'].append(p)
                classified = True
                break
        if classified:
            continue

        # 2. 不同器官/部位
        organs_a = [c for c in organ_chars if c in wa]
        organs_b = [c for c in organ_chars if c in wb]
        if organs_a and organs_b and set(organs_a) != set(organs_b):
            patterns['不同器官/部位'].append(p)
            continue

        # 3. 共享前缀但分歧
        norm_a = wa.replace(' ', '').lower()
        norm_b = wb.replace(' ', '').lower()
        if len(norm_a) >= 2 and len(norm_b) >= 2:
            common_prefix_len = 0
            for i in range(min(len(norm_a), len(norm_b))):
                if norm_a[i] == norm_b[i]:
                    common_prefix_len = i + 1
                else:
                    break
            if 1 <= common_prefix_len < min(len(norm_a), len(norm_b)):
                patterns['共享前缀但分歧'].append(p)
                continue

        # 4. 长度差异过大（可能是后缀误匹配）
        if len(wa) > 0 and len(wb) > 0:
            ratio = max(len(wa), len(wb)) / min(len(wa), len(wb))
            if ratio > 1.5:
                patterns['长度差异过大'].append(p)
                continue

        # 5. 默认
        patterns['其他误匹配'].append(p)

    return dict(patterns)


def generate_rule_suggestions(strategy_stats, error_patterns, pairs):
    """基于错误分析生成规则修正建议"""
    suggestions = []

    # 按策略FP率排序
    high_fp = [(s, st) for s, st in strategy_stats.items() if st['fp_rate'] > 5]
    high_fp.sort(key=lambda x: -x[1]['fp_rate'])

    for s, st in high_fp:
        if s == 1:
            suggestions.append(
                f"S1(归一化匹配) FP率={st['fp_rate']:.1f}% — "
                f"归一化匹配通常不应出错，请检查清洗规则是否遗漏了特殊情况"
            )
        elif s == 2:
            suggestions.append(
                f"S2(前后缀) FP率={st['fp_rate']:.1f}% — "
                f"建议：(1) 收紧GRAMMATICAL_SUFFIXES列表，移除领域性后缀 "
                f"(2) 增加短词最小长度(当前3→4) (3) 降低suffix_max_extra"
            )
        elif s == 3:
            suggestions.append(
                f"S3(Jaccard) FP率={st['fp_rate']:.1f}% — "
                f"建议：(1) 提高jaccard_threshold(0.85→0.88) "
                f"(2) 缩短长度比限制(1.3→1.2) (3) 增加反义词前缀黑名单"
            )
        elif s == 4:
            suggestions.append(
                f"S4(编辑距离) FP率={st['fp_rate']:.1f}% — "
                f"建议：(1) 降低edit_ratio(0.20→0.15) "
                f"(2) 增加首字符必须相同约束 (3) 增加语义验证"
            )
        elif s == 5:
            suggestions.append(
                f"S5(共现邻居) FP率={st['fp_rate']:.1f}% — "
                f"建议：(1) 提高contain_min(0.40→0.50) "
                f"(2) 增加intersect_min(3→4) (3) 增加neighbor_min_co"
            )

    # 基于错误模式的建议
    if '反义词前缀' in error_patterns:
        n = len(error_patterns['反义词前缀'])
        examples = [f"{p['word_a']}↔{p['word_b']}" for p in error_patterns['反义词前缀'][:5]]
        suggestions.append(
            f"发现{n}对反义词误匹配 — 建议在策略3/4中增加反义词前缀黑名单\n"
            f"    示例: {', '.join(examples)}"
        )
    if '不同器官/部位' in error_patterns:
        n = len(error_patterns['不同器官/部位'])
        examples = [f"{p['word_a']}↔{p['word_b']}" for p in error_patterns['不同器官/部位'][:5]]
        suggestions.append(
            f"发现{n}对不同器官误匹配 — 建议增加器官字符互斥约束\n"
            f"    示例: {', '.join(examples)}"
        )

    # 整体评估
    overall_fp = sum(st['fp'] for st in strategy_stats.values())
    overall_total = sum(st['total'] for st in strategy_stats.values())
    if overall_total > 0:
        fp_rate = overall_fp / overall_total * 100
        if fp_rate < 5:
            suggestions.append(
                f"整体FP率仅{fp_rate:.1f}%，规则表现良好。"
                f"可考虑放宽灵敏度(sensitivity=1)以减少漏检(FN)"
            )
        elif fp_rate > 30:
            suggestions.append(
                f"整体FP率高达{fp_rate:.1f}%，建议使用sensitivity=3严格模式，"
                f"或考虑引入语义相似度模型辅助判断"
            )

    if not suggestions:
        suggestions.append("当前规则表现良好，暂无修正建议")

    return suggestions


def generate_report(input_dir, records, pairs, strategy_stats, error_patterns, suggestions):
    """生成完整分析报告"""
    lines = []
    lines.append("=" * 60)
    lines.append("  审核错误分析报告")
    lines.append(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"  数据来源: {input_dir}")
    lines.append("=" * 60)
    lines.append("")

    # 总览
    n_groups = len(records)
    n_pairs = len(pairs)
    n_fp = sum(1 for p in pairs if p['error_type'] == 'false_positive')
    n_correct = sum(1 for p in pairs if p['error_type'] == 'correct')
    lines.append(f"已审核群组: {n_groups}")
    lines.append(f"已审核配对: {n_pairs}")
    if n_pairs > 0:
        lines.append(f"正确合并: {n_correct} ({n_correct/n_pairs*100:.1f}%)")
        lines.append(f"误匹配(FP): {n_fp} ({n_fp/n_pairs*100:.1f}%)")
    lines.append("")

    # 按策略统计
    lines.append("-" * 40)
    lines.append("各策略错误率:")
    lines.append("-" * 40)
    for s in sorted(strategy_stats.keys()):
        st = strategy_stats[s]
        lines.append(f"  {st['name']}: {st['fp']}/{st['total']} FP ({st['fp_rate']:.1f}%)")
        if st['fp_examples']:
            lines.append(f"    误匹配示例:")
            for ex in st['fp_examples'][:10]:
                lines.append(f"      {ex['word_a']} <-> {ex['word_b']}")
    lines.append("")

    # 错误模式
    if error_patterns:
        lines.append("-" * 40)
        lines.append("错误模式聚类:")
        lines.append("-" * 40)
        for pattern, examples in sorted(error_patterns.items(), key=lambda x: -len(x[1])):
            lines.append(f"  [{pattern}] ({len(examples)} 例)")
            for ex in examples[:10]:
                lines.append(f"    {ex['word_a']} <-> {ex['word_b']}")
        lines.append("")

    # 规则修正建议
    lines.append("-" * 40)
    lines.append("规则修正建议:")
    lines.append("-" * 40)
    for i, sug in enumerate(suggestions, 1):
        lines.append(f"  {i}. {sug}")
    lines.append("")

    # 下一步行动
    lines.append("-" * 40)
    lines.append("下一步行动建议:")
    lines.append("-" * 40)
    lines.append("  1. 根据上述建议修改 keyword_cleaner.py 中的规则参数")
    lines.append("  2. 重新运行分析: python keyword_cleaner.py -i data/ -o output/")
    lines.append("  3. 在GUI中继续审核新结果，观察FP率是否下降")
    lines.append("  4. 当规则优化到瓶颈后，考虑引入语义相似度模型")
    lines.append("     (使用 --export-training 导出训练数据)")

    return '\n'.join(lines)


def export_training_data(input_dir, pairs, output_dir=None):
    """导出训练数据（供深度学习模型使用）"""
    if output_dir is None:
        output_dir = input_dir

    # 格式1: 二分类标注（同义/不同义）
    train_rows = []
    for p in pairs:
        label = 1 if p['human_decision'] == 'merge' else 0
        train_rows.append({
            'word_a': p['word_a'],
            'word_b': p['word_b'],
            'label': label,
            'strategy': p.get('strategy', 0),
            'strategy_name': p.get('strategy_name', ''),
        })

    train_path = os.path.join(output_dir, '训练数据_配对标注.csv')
    with open(train_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = None
        for row in train_rows:
            if writer is None:
                writer = csv.DictWriter(f, fieldnames=row.keys())
                writer.writeheader()
            writer.writerow(row)

    # 格式2: JSON格式（供sentence-transformers使用）
    json_path = os.path.join(output_dir, '训练数据_配对标注.json')
    json_data = []
    for p in pairs:
        label = 1 if p['human_decision'] == 'merge' else 0
        json_data.append({
            'sentence1': p['word_a'],
            'sentence2': p['word_b'],
            'label': label,
        })
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    # 统计
    n_pos = sum(1 for p in pairs if p['human_decision'] == 'merge')
    n_neg = sum(1 for p in pairs if p['human_decision'] == 'reject')

    print(f"\n训练数据已导出:")
    print(f"  CSV: {train_path} ({len(train_rows)} 条)")
    print(f"  JSON: {json_path} ({len(json_data)} 条)")
    print(f"  正样本(同义): {n_pos} ({n_pos/len(pairs)*100:.1f}%)")
    print(f"  负样本(不同义): {n_neg} ({n_neg/len(pairs)*100:.1f}%)")

    if n_pos > 0 and n_neg > 0:
        ratio = n_neg / n_pos
        if ratio > 3:
            print(f"  注意: 正负样本比 1:{ratio:.1f}，训练时需做类别平衡")
        elif ratio < 0.3:
            print(f"  注意: 正负样本比 {1/ratio:.1f}:1，训练时需做类别平衡")

    return train_path, json_path


def main():
    parser = argparse.ArgumentParser(
        description='审核记录分析工具 v1.0',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('-i', '--input', required=True,
                        help='包含审核记录.jsonl的输出目录')
    parser.add_argument('--export-training', action='store_true',
                        help='导出训练数据（供深度学习模型使用）')
    parser.add_argument('-o', '--output',
                        help='输出目录（默认与输入相同）')

    args = parser.parse_args()
    input_dir = args.input
    output_dir = args.output or input_dir

    # 加载数据
    print("Loading review records...")
    records = load_review_records(input_dir)
    if not records:
        print("No review records found. Please review some groups first.")
        return 1

    pairs = extract_pair_records(records)
    print(f"  {len(records)} group records, {len(pairs)} pair records")

    # 统计分析
    strategy_stats = compute_strategy_stats(pairs)
    fp_pairs = [p for p in pairs if p['error_type'] == 'false_positive']
    error_patterns = cluster_error_patterns(fp_pairs)
    suggestions = generate_rule_suggestions(strategy_stats, error_patterns, pairs)

    # 生成报告
    report = generate_report(input_dir, records, pairs, strategy_stats,
                             error_patterns, suggestions)
    report_path = os.path.join(output_dir, '审核错误分析报告.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\nReport saved: {report_path}")

    # 打印摘要
    print("\n" + "=" * 40)
    print("Summary:")
    print("=" * 40)
    n_fp = len(fp_pairs)
    if pairs:
        print(f"  Total pairs: {len(pairs)}")
        print(f"  FP rate: {n_fp/len(pairs)*100:.1f}% ({n_fp}/{len(pairs)})")
    for s in sorted(strategy_stats.keys()):
        st = strategy_stats[s]
        print(f"  {st['name']}: FP={st['fp_rate']:.1f}% ({st['fp']}/{st['total']})")

    # 导出训练数据
    if args.export_training:
        import csv  # 确保导入
        export_training_data(input_dir, pairs, output_dir)

    return 0


if __name__ == '__main__':
    sys.exit(main())
