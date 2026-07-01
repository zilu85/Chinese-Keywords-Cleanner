#!/usr/bin/env python3
"""
关键词清洗工具 - GUI审核界面 v2.0
============================
交互式审核变体群组，确认/修改标准词，导出映射表。
支持增量审核、进度自动保存、断点续审、错误分析。

用法：
  python gui_review.py -i output/
  python gui_review.py -i output/ -m 4_映射表_待审核.csv
"""

import os
import sys
import csv
import json
import argparse
import platform
import subprocess
import webbrowser
from datetime import datetime

# tkinter
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_SCRIPT = os.path.join(SCRIPT_DIR, 'keyword_cleaner.py')

# 审核记录文件名
REVIEW_LOG_FILE = '审核记录.jsonl'

# 当前工具版本号（用于审核记录版本追踪）
TOOL_VERSION = '2.4'

# Python executable
if platform.system() == 'Windows':
    PYTHON = os.path.join(SCRIPT_DIR, 'python', 'python.exe')
    if not os.path.exists(PYTHON):
        PYTHON = sys.executable
else:
    PYTHON = os.path.join(SCRIPT_DIR, 'venv', 'bin', 'python')
    if not os.path.exists(PYTHON):
        PYTHON = 'python3'


class ReviewApp:
    def __init__(self, root, input_dir, mapping_file=None):
        self.root = root
        self.root.title("关键词清洗工具 - 变体审核 v2.4")
        self.root.geometry("1200x750")
        self.root.minsize(900, 550)

        self.input_dir = input_dir
        self.mapping_file = mapping_file
        self.groups = []       # 变体群组数据
        self.mapping = {}      # 变体→标准词
        self.reviewed = {}     # group_id → {standard, action}
        self.current_group = None
        self.pair_sources = {} # (word_a, word_b) → strategy_number
        self.review_log_path = None  # 审核记录JSONL路径
        self.global_mapping = {}  # 全局映射表（来自已审核期刊）merge决策
        self.global_mapping_path = None  # 全局映射文件路径
        self.global_keeps = []  # 全局KEEP列表 [set(...), set(...)] KEEP决策
        self.global_keep_path = None  # 全局KEEP文件路径

        self._build_ui()
        self._load_data()

    # ============================================================
    # UI构建
    # ============================================================

    def _build_ui(self):
        # ---- 顶部工具栏 ----
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill=tk.X, padx=8, pady=4)

        ttk.Button(toolbar, text="Load Data", command=self._load_data).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Run Analysis", command=self._run_analysis).pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        # 全局映射按钮
        ttk.Button(toolbar, text="Load Global Map", command=self._load_global_mapping).pack(side=tk.LEFT, padx=2)
        self.global_map_label = ttk.Label(toolbar, text="(no global map)")
        self.global_map_label.pack(side=tk.LEFT, padx=4)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(toolbar, textvariable=self.status_var).pack(side=tk.LEFT, padx=4)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(toolbar, text="Confirm All & Export", command=self._confirm_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Export Mapping", command=self._export_mapping).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Apply & Export Final", command=self._apply_and_export).pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(toolbar, text="Error Analysis", command=self._error_analysis).pack(side=tk.LEFT, padx=2)

        # ---- 主区域：左右分栏 ----
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        # 左侧：群组列表
        left_frame = ttk.LabelFrame(paned, text="Variant Groups", padding=4)
        paned.add(left_frame, weight=1)

        # 筛选
        filter_frame = ttk.Frame(left_frame)
        filter_frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(filter_frame, text="Filter:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add('write', lambda *_: self._apply_filter())
        ttk.Entry(filter_frame, textvariable=self.filter_var, width=20).pack(side=tk.LEFT, padx=4)
        ttk.Label(filter_frame, text="Show:").pack(side=tk.LEFT, padx=(8, 0))
        self.show_var = tk.StringVar(value="with_variants")
        show_combo = ttk.Combobox(filter_frame, textvariable=self.show_var,
                                  values=["all", "with_variants", "reviewed", "unreviewed"],
                                  state='readonly', width=12)
        show_combo.pack(side=tk.LEFT, padx=4)
        show_combo.bind('<<ComboboxSelected>>', lambda _: self._apply_filter())

        # Treeview
        cols = ('id', 'variants', 'top_kw', 'freq', 'status')
        self.tree = ttk.Treeview(left_frame, columns=cols, show='headings', height=20)
        self.tree.heading('id', text='ID')
        self.tree.heading('variants', text='N')
        self.tree.heading('top_kw', text='Top Keyword')
        self.tree.heading('freq', text='Freq')
        self.tree.heading('status', text='Status')
        self.tree.column('id', width=40, anchor='center')
        self.tree.column('variants', width=35, anchor='center')
        self.tree.column('top_kw', width=180)
        self.tree.column('freq', width=50, anchor='center')
        self.tree.column('status', width=70, anchor='center')
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind('<<TreeviewSelect>>', self._on_group_select)

        scrollbar = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 右侧：详情与操作
        right_frame = ttk.LabelFrame(paned, text="Group Detail", padding=4)
        paned.add(right_frame, weight=2)

        # 群组信息
        info_frame = ttk.Frame(right_frame)
        info_frame.pack(fill=tk.X, pady=(0, 8))
        self.group_info_var = tk.StringVar(value="Select a group to review")
        ttk.Label(info_frame, textvariable=self.group_info_var,
                  font=('', 11, 'bold')).pack(anchor='w')

        # 变体列表
        detail_cols = ('std', 'merge', 'keyword', 'freq', 'pct')
        self.detail_tree = ttk.Treeview(right_frame, columns=detail_cols,
                                        show='headings', height=10)
        self.detail_tree.heading('std', text='Std')
        self.detail_tree.heading('merge', text='Merge')
        self.detail_tree.heading('keyword', text='Keyword')
        self.detail_tree.heading('freq', text='Freq')
        self.detail_tree.heading('pct', text='%')
        self.detail_tree.column('std', width=40, anchor='center')
        self.detail_tree.column('merge', width=50, anchor='center')
        self.detail_tree.column('keyword', width=260)
        self.detail_tree.column('freq', width=50, anchor='center')
        self.detail_tree.column('pct', width=50, anchor='center')
        self.detail_tree.pack(fill=tk.BOTH, expand=True)
        self.detail_tree.bind('<<TreeviewSelect>>', self._on_detail_select)
        self.detail_tree.bind('<Button-1>', self._on_detail_click)

        # 操作区
        action_frame = ttk.Frame(right_frame)
        action_frame.pack(fill=tk.X, pady=(8, 0))

        ttk.Label(action_frame, text="Action:").pack(side=tk.LEFT)
        self.action_var = tk.StringVar(value="selective")
        actions = [("Selective merge", "selective"),
                   ("Merge all to std", "confirm"),
                   ("Keep all separate", "keep"),
                   ("Custom standard:", "custom")]
        for text, val in actions:
            ttk.Radiobutton(action_frame, text=text, variable=self.action_var,
                            value=val, command=self._on_action_change).pack(side=tk.LEFT, padx=4)

        self.custom_std_var = tk.StringVar()
        self.custom_std_entry = ttk.Entry(action_frame, textvariable=self.custom_std_var, width=20)
        self.custom_std_entry.pack(side=tk.LEFT, padx=4)
        self.custom_std_entry.configure(state='disabled')

        ttk.Button(action_frame, text="Apply",
                   command=self._apply_to_group).pack(side=tk.LEFT, padx=8)

        # 导航
        nav_frame = ttk.Frame(right_frame)
        nav_frame.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(nav_frame, text="<< Prev", command=self._prev_group).pack(side=tk.LEFT, padx=2)
        ttk.Button(nav_frame, text="Next >>", command=self._next_group).pack(side=tk.LEFT, padx=2)
        self.nav_info_var = tk.StringVar()
        ttk.Label(nav_frame, textvariable=self.nav_info_var).pack(side=tk.LEFT, padx=8)

        # ---- 底部日志 ----
        log_frame = ttk.LabelFrame(self.root, text="Log", padding=4)
        log_frame.pack(fill=tk.X, padx=8, pady=(0, 4))
        self.log_text = tk.Text(log_frame, height=4, wrap=tk.WORD, state='disabled')
        self.log_text.pack(fill=tk.X)

    # ============================================================
    # 数据加载
    # ============================================================

    def _load_data(self):
        """从输出目录加载变体群组数据，含策略溯源和审核进度恢复"""
        # 如果当前目录没有结果文件，弹出选择目录对话框
        has_result = any(
            os.path.exists(os.path.join(self.input_dir, fname))
            for fname in ['3_变体群组_待审核.csv', '3_变体群组.csv']
        )
        if not has_result:
            chosen = filedialog.askdirectory(
                title="Select Output Folder (with analysis results)")
            if chosen:
                self.input_dir = chosen
            else:
                return

        # 尝试加载变体群组CSV
        groups_file = None
        for fname in ['3_变体群组_待审核.csv', '3_变体群组.csv']:
            fpath = os.path.join(self.input_dir, fname)
            if os.path.exists(fpath):
                groups_file = fpath
                break

        if not groups_file:
            self.log("No variant groups file found in selected folder.")
            self.log("Click [Run Analysis] to analyze XML files first.")
            self.status_var.set("No data - Click [Run Analysis] to start")
            return

        self.log(f"Loading: {groups_file}")

        # 解析变体群组
        groups_dict = {}  # group_id → {variants, suggested, strategies}
        with open(groups_file, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                gid = int(row['群组ID'])
                if gid not in groups_dict:
                    groups_dict[gid] = {
                        'variants': [],
                        'suggested': row.get('建议标准词', ''),
                        'strategies': row.get('匹配策略', ''),
                    }
                groups_dict[gid]['variants'].append({
                    'keyword': row['关键词'],
                    'freq': int(row['频次']),
                })

        # 加载配对策略溯源
        pair_sources_file = os.path.join(self.input_dir, '3_配对策略溯源.csv')
        self.pair_sources = {}
        if os.path.exists(pair_sources_file):
            with open(pair_sources_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    wa, wb = row['词A'], row['词B']
                    key = tuple(sorted([wa, wb]))
                    self.pair_sources[key] = int(row['策略'])
            self.log(f"Loaded pair sources: {len(self.pair_sources)} pairs")

        self.groups = []
        for gid, data in sorted(groups_dict.items()):
            self.groups.append({
                'group_id': gid,
                'variants': data['variants'],
                'suggested_standard': data['suggested'],
                'has_variants': len(data['variants']) > 1,
                'status': 'unreviewed',
                'selected_standard': data['suggested'],
                'action': 'confirm',
                'strategies': data.get('strategies', ''),
            })

        # 加载已有映射表
        if self.mapping_file and os.path.exists(self.mapping_file):
            self._load_mapping(self.mapping_file)

        # 恢复审核进度
        self.review_log_path = os.path.join(self.input_dir, REVIEW_LOG_FILE)
        n_restored = self._restore_review_progress()

        # 如果已加载全局映射表，自动确认已知群组
        n_merge, n_keep = self._auto_confirm_from_global()

        self._populate_tree()
        n_with = sum(1 for g in self.groups if g['has_variants'])
        n_reviewed = sum(1 for g in self.groups if g['has_variants'] and g['status'] != 'unreviewed')
        self.status_var.set(f"Loaded {len(self.groups)} groups ({n_with} with variants, {n_reviewed} reviewed)")
        parts = []
        if n_merge: parts.append(f"{n_merge} auto-merged")
        if n_keep: parts.append(f"{n_keep} auto-kept")
        extra = f", {', '.join(parts)} from global" if parts else ""
        self.log(f"Loaded {len(self.groups)} groups, {n_with} with variants, {n_reviewed} reviewed{extra}")

    def _load_mapping(self, filepath):
        """加载映射表"""
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                variant = row.get('变体', '').strip()
                standard = row.get('标准词', '').strip()
                confirmed = row.get('用户确认', '').strip()
                modified = row.get('修改后标准词', '').strip()
                if variant and standard:
                    if confirmed == '否' and modified:
                        self.mapping[variant] = modified
                    elif confirmed != '否':
                        self.mapping[variant] = standard

    # ============================================================
    # 审核进度持久化
    # ============================================================

    def _restore_review_progress(self):
        """从JSONL文件恢复审核进度，返回恢复的群组数"""
        if not self.review_log_path or not os.path.exists(self.review_log_path):
            return 0

        # 读取所有审核记录，构建 group_id → 最新记录 的映射
        group_reviews = {}  # group_id → latest review record
        log_versions = set()  # 记录中出现的版本号
        try:
            with open(self.review_log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        gid = rec.get('group_id')
                        if gid is not None:
                            group_reviews[gid] = rec
                        ver = rec.get('tool_version', '')
                        if ver:
                            log_versions.add(ver)
                    except json.JSONDecodeError:
                        continue
        except (IOError, OSError):
            return 0

        # 版本不匹配检测：审核记录的版本与当前工具版本不同
        if log_versions and TOOL_VERSION not in log_versions:
            old_versions = ', '.join(sorted(log_versions))
            answer = messagebox.askyesnocancel(
                "Version Mismatch",
                f"Review records were created with v{old_versions},\n"
                f"but current tool is v{TOOL_VERSION}.\n\n"
                f"Rules have changed, so old review results may not\n"
                f"match the new variant groups.\n\n"
                f"Yes = Clear old records and start fresh\n"
                f"No  = Keep old records (may cause mismatches)\n"
                f"Cancel = Abort loading"
            )
            if answer is None:  # Cancel
                return 0
            elif answer:  # Yes - clear old records
                try:
                    os.rename(self.review_log_path,
                              self.review_log_path + f'.v{old_versions}.bak')
                    self.log(f"Old review records backed up: "
                             f"{REVIEW_LOG_FILE}.v{old_versions}.bak")
                except (IOError, OSError) as e:
                    self.log(f"Warning: Failed to backup old records: {e}")
                return 0
            # No - continue with old records (user accepts risk)

        # 恢复每个群组的审核状态
        n_restored = 0
        n_mismatch = 0
        for g in self.groups:
            gid = g['group_id']
            if gid not in group_reviews:
                continue
            rec = group_reviews[gid]

            # 验证群组内容是否匹配（防止规则变化后群组ID错位）
            rec_variants = set(rec.get('merge_flags', {}).keys())
            cur_variants = set(v['keyword'] for v in g['variants'])
            if rec_variants and cur_variants and not rec_variants.issubset(cur_variants):
                n_mismatch += 1
                continue  # 跳过不匹配的群组

            action = rec.get('action', 'confirm')
            g['action'] = action
            g['selected_standard'] = rec.get('selected_standard', g['suggested_standard'])

            if action == 'keep':
                g['status'] = 'kept'
                g['merge_flags'] = {v['keyword']: False for v in g['variants']}
            elif action in ('confirm', 'selective', 'custom'):
                g['status'] = 'confirmed'
                # 恢复merge_flags
                merge_flags = rec.get('merge_flags', {})
                if merge_flags:
                    g['merge_flags'] = merge_flags
                else:
                    g['merge_flags'] = {v['keyword']: (v['keyword'] != g['selected_standard'])
                                        for v in g['variants']}
            n_restored += 1

        if n_mismatch > 0:
            self.log(f"Warning: {n_mismatch} groups skipped due to content mismatch")

        return n_restored

    def _save_review_record(self, group):
        """将审核记录追加到JSONL文件（自动保存）"""
        if not self.review_log_path:
            self.review_log_path = os.path.join(self.input_dir, REVIEW_LOG_FILE)

        # 构建配对级别的策略溯源
        pair_details = []
        if group['has_variants']:
            variants = [v['keyword'] for v in group['variants']]
            for i in range(len(variants)):
                for j in range(i + 1, len(variants)):
                    key = tuple(sorted([variants[i], variants[j]]))
                    strategy = self.pair_sources.get(key, 0)
                    # 判断人工决策：这对是合并还是保留
                    merge_flags = group.get('merge_flags', {})
                    merged_a = merge_flags.get(variants[i], variants[i] != group.get('selected_standard', ''))
                    merged_b = merge_flags.get(variants[j], variants[j] != group.get('selected_standard', ''))
                    # 如果两个都合并到标准词，则配对被确认；否则被否决
                    if variants[i] == group.get('selected_standard', ''):
                        human_decision = 'merge' if merged_b else 'reject'
                    elif variants[j] == group.get('selected_standard', ''):
                        human_decision = 'merge' if merged_a else 'reject'
                    else:
                        human_decision = 'merge' if (merged_a or merged_b) else 'reject'

                    pair_details.append({
                        'word_a': variants[i],
                        'word_b': variants[j],
                        'strategy': strategy,
                        'strategy_name': {1: '归一化匹配', 2: '前后缀', 3: 'Jaccard',
                                          4: '编辑距离', 5: '共现邻居'}.get(strategy, 'unknown'),
                        'auto_decision': 'merge',
                        'human_decision': human_decision,
                        'error_type': 'false_positive' if human_decision == 'reject' else
                                      'false_negative' if group['action'] == 'keep' and strategy == 0 else
                                      'correct',
                    })

        record = {
            'tool_version': TOOL_VERSION,
            'timestamp': datetime.now().isoformat(),
            'group_id': group['group_id'],
            'action': group.get('action', 'confirm'),
            'selected_standard': group.get('selected_standard', ''),
            'suggested_standard': group.get('suggested_standard', ''),
            'merge_flags': group.get('merge_flags', {}),
            'n_variants': len(group['variants']),
            'strategies': group.get('strategies', ''),
            'pair_details': pair_details,
        }

        try:
            with open(self.review_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
        except (IOError, OSError) as e:
            self.log(f"Warning: Failed to save review record: {e}")

    # ============================================================
    # 错误分析
    # ============================================================

    def _error_analysis(self):
        """基于审核记录生成错误分析报告"""
        if not self.review_log_path or not os.path.exists(self.review_log_path):
            messagebox.showinfo("Error Analysis",
                                "No review records found.\nPlease review some groups first.")
            return

        # 读取所有审核记录
        all_pairs = []
        group_records = []
        try:
            with open(self.review_log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        group_records.append(rec)
                        for pd in rec.get('pair_details', []):
                            all_pairs.append(pd)
                    except json.JSONDecodeError:
                        continue
        except (IOError, OSError):
            messagebox.showerror("Error", "Failed to read review records.")
            return

        if not all_pairs:
            messagebox.showinfo("Error Analysis", "No pair-level review data found.")
            return

        # 统计
        strategy_names = {1: 'S1-归一化', 2: 'S2-前后缀', 3: 'S3-Jaccard',
                          4: 'S4-编辑距离', 5: 'S5-共现邻居', 0: '未知'}

        # 按策略统计错误率
        strategy_stats = {}
        for s in range(1, 6):
            pairs_s = [p for p in all_pairs if p['strategy'] == s]
            if not pairs_s:
                continue
            n_total = len(pairs_s)
            n_fp = sum(1 for p in pairs_s if p['error_type'] == 'false_positive')
            n_correct = sum(1 for p in pairs_s if p['error_type'] == 'correct')
            strategy_stats[s] = {
                'name': strategy_names[s],
                'total': n_total,
                'correct': n_correct,
                'fp': n_fp,
                'fp_rate': n_fp / n_total * 100 if n_total > 0 else 0,
                'fp_examples': [p for p in pairs_s if p['error_type'] == 'false_positive'][:10],
            }

        # 错误模式聚类
        fp_pairs = [p for p in all_pairs if p['error_type'] == 'false_positive']
        error_patterns = self._cluster_error_patterns(fp_pairs)

        # 生成报告
        report_lines = []
        report_lines.append("=" * 60)
        report_lines.append("  审核错误分析报告")
        report_lines.append(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        report_lines.append("=" * 60)
        report_lines.append("")

        # 总览
        n_reviewed_groups = len(group_records)
        n_total_pairs = len(all_pairs)
        n_fp = len(fp_pairs)
        n_correct = sum(1 for p in all_pairs if p['error_type'] == 'correct')
        report_lines.append(f"已审核群组: {n_reviewed_groups}")
        report_lines.append(f"已审核配对: {n_total_pairs}")
        report_lines.append(f"正确合并: {n_correct} ({n_correct/n_total_pairs*100:.1f}%)")
        report_lines.append(f"误匹配(FP): {n_fp} ({n_fp/n_total_pairs*100:.1f}%)")
        report_lines.append("")

        # 按策略统计
        report_lines.append("-" * 40)
        report_lines.append("各策略错误率:")
        report_lines.append("-" * 40)
        for s in sorted(strategy_stats.keys()):
            st = strategy_stats[s]
            report_lines.append(f"  {st['name']}: {st['fp']}/{st['total']} FP "
                              f"({st['fp_rate']:.1f}%)")
            if st['fp_examples']:
                report_lines.append(f"    误匹配示例:")
                for ex in st['fp_examples'][:5]:
                    report_lines.append(f"      {ex['word_a']} <-> {ex['word_b']}")
        report_lines.append("")

        # 错误模式
        if error_patterns:
            report_lines.append("-" * 40)
            report_lines.append("错误模式聚类:")
            report_lines.append("-" * 40)
            for pattern, examples in error_patterns.items():
                report_lines.append(f"  [{pattern}] ({len(examples)} 例)")
                for ex in examples[:5]:
                    report_lines.append(f"    {ex['word_a']} <-> {ex['word_b']}")
            report_lines.append("")

        # 规则修正建议
        report_lines.append("-" * 40)
        report_lines.append("规则修正建议:")
        report_lines.append("-" * 40)
        suggestions = self._generate_rule_suggestions(strategy_stats, error_patterns)
        for i, sug in enumerate(suggestions, 1):
            report_lines.append(f"  {i}. {sug}")

        # 保存报告
        report_path = os.path.join(self.input_dir, '审核错误分析报告.txt')
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))

        self.log(f"Error analysis report saved: {report_path}")
        self.log(f"  Reviewed: {n_reviewed_groups} groups, {n_total_pairs} pairs")
        self.log(f"  FP rate: {n_fp/n_total_pairs*100:.1f}% ({n_fp}/{n_total_pairs})")

        # 在日志区显示摘要
        self.log("--- Error Analysis Summary ---")
        for s in sorted(strategy_stats.keys()):
            st = strategy_stats[s]
            self.log(f"  {st['name']}: FP rate = {st['fp_rate']:.1f}%")

        messagebox.showinfo("Error Analysis",
                            f"Report saved to:\n{report_path}\n\n"
                            f"Total pairs: {n_total_pairs}\n"
                            f"FP rate: {n_fp/n_total_pairs*100:.1f}%")

    def _cluster_error_patterns(self, fp_pairs):
        """对误匹配配对进行模式聚类"""
        patterns = {}

        for p in fp_pairs:
            wa, wb = p['word_a'], p['word_b']
            # 检测反义词模式
            antonym_prefixes = [('急', '慢'), ('内', '外'), ('上', '下'),
                                ('前', '后'), ('左', '右'), ('大', '小'),
                                ('高', '低'), ('长', '短'), ('阳', '阴'),
                                ('正', '负'), ('主', '次'), ('早', '晚')]
            is_antonym = False
            for a_char, b_char in antonym_prefixes:
                if (wa.startswith(a_char) and wb.startswith(b_char)) or \
                   (wa.startswith(b_char) and wb.startswith(a_char)):
                    patterns.setdefault('反义词前缀', []).append(p)
                    is_antonym = True
                    break

            # 检测不同器官/部位
            organ_chars = ['肺', '肝', '肾', '心', '脑', '胃', '肠', '骨',
                          '血', '皮', '眼', '耳', '口', '颈', '胸', '腹']
            organs_in = [c for c in organ_chars if c in wa or c in wb]
            if len(organs_in) >= 2 and not is_antonym:
                patterns.setdefault('不同器官/部位', []).append(p)
                continue

            # 检测不同疾病/状态
            if not is_antonym:
                # 长度相近但核心不同
                norm_a = wa.replace(' ', '').lower()
                norm_b = wb.replace(' ', '').lower()
                if len(norm_a) >= 2 and len(norm_b) >= 2:
                    # 共享前缀但后缀不同
                    common_prefix_len = 0
                    for i in range(min(len(norm_a), len(norm_b))):
                        if norm_a[i] == norm_b[i]:
                            common_prefix_len = i + 1
                        else:
                            break
                    if common_prefix_len >= 1 and common_prefix_len < len(norm_a) and common_prefix_len < len(norm_b):
                        patterns.setdefault('共享前缀但分歧', []).append(p)
                        continue

                # 默认
                if not is_antonym:
                    patterns.setdefault('其他误匹配', []).append(p)

        return patterns

    def _generate_rule_suggestions(self, strategy_stats, error_patterns):
        """基于错误分析生成规则修正建议"""
        suggestions = []

        # 按策略FP率排序
        high_fp_strategies = [(s, st) for s, st in strategy_stats.items()
                              if st['fp_rate'] > 10]
        high_fp_strategies.sort(key=lambda x: -x[1]['fp_rate'])

        for s, st in high_fp_strategies:
            if s == 3:  # Jaccard
                suggestions.append(
                    f"S3(Jaccard) FP率={st['fp_rate']:.1f}%，"
                    f"建议提高阈值(当前0.85→0.88)或增加首字符约束"
                )
            elif s == 4:  # 编辑距离
                suggestions.append(
                    f"S4(编辑距离) FP率={st['fp_rate']:.1f}%，"
                    f"建议降低edit_ratio或增加语义验证"
                )
            elif s == 2:  # 前后缀
                suggestions.append(
                    f"S2(前后缀) FP率={st['fp_rate']:.1f}%，"
                    f"建议收紧后缀列表或增加短词最小长度"
                )
            elif s == 5:  # 共现邻居
                suggestions.append(
                    f"S5(共现邻居) FP率={st['fp_rate']:.1f}%，"
                    f"建议提高contain_min阈值或增加intersect_min"
                )

        # 基于错误模式的建议
        if '反义词前缀' in error_patterns:
            n = len(error_patterns['反义词前缀'])
            suggestions.append(
                f"发现{n}对反义词误匹配，建议新增反义词前缀黑名单"
                f"(急/慢/内/外/上/下/前/后等)"
            )
        if '不同器官/部位' in error_patterns:
            n = len(error_patterns['不同器官/部位'])
            suggestions.append(
                f"发现{n}对不同器官误匹配，建议增加器官字符互斥约束"
            )

        # 如果FP率整体低，建议可以放宽
        overall_fp = sum(st['fp'] for st in strategy_stats.values())
        overall_total = sum(st['total'] for st in strategy_stats.values())
        if overall_total > 0 and overall_fp / overall_total < 5:
            suggestions.append(
                f"整体FP率仅{overall_fp/overall_total*100:.1f}%，"
                f"可考虑放宽灵敏度(如sensitivity=1)以减少漏检"
            )

        if not suggestions:
            suggestions.append("当前规则表现良好，暂无修正建议")

        return suggestions

    # ============================================================
    # Tree操作
    # ============================================================

    def _populate_tree(self):
        """填充群组列表"""
        for item in self.tree.get_children():
            self.tree.delete(item)

        show = self.show_var.get()
        keyword_filter = self.filter_var.get().strip().lower()

        for g in self.groups:
            # 筛选
            if show == 'with_variants' and not g['has_variants']:
                continue
            elif show == 'reviewed' and g['status'] == 'unreviewed':
                continue
            elif show == 'unreviewed' and g['status'] != 'unreviewed':
                continue

            # 关键词过滤
            if keyword_filter:
                all_kw = ' '.join(v['keyword'] for v in g['variants'])
                if keyword_filter not in all_kw.lower():
                    continue

            top_kw = g['variants'][0]['keyword'] if g['variants'] else ''
            top_freq = g['variants'][0]['freq'] if g['variants'] else 0
            status = g['status']
            status_text = {'unreviewed': '--', 'confirmed': 'OK', 'kept': 'KEEP', 'custom': 'EDIT'}.get(status, status)

            self.tree.insert('', tk.END, iid=str(g['group_id']),
                             values=(g['group_id'], len(g['variants']),
                                     top_kw, top_freq, status_text))

    def _apply_filter(self):
        self._populate_tree()

    def _on_group_select(self, event):
        """选中群组时显示详情"""
        sel = self.tree.selection()
        if not sel:
            return
        gid = int(sel[0])
        self._show_group_detail(gid)

    def _show_group_detail(self, gid):
        """显示群组详情"""
        group = None
        for g in self.groups:
            if g['group_id'] == gid:
                group = g
                break
        if not group:
            return

        self.current_group = gid

        # 信息
        total_freq = sum(v['freq'] for v in group['variants'])
        strategies = group.get('strategies', '')
        strategy_label = f" | Strategies: {strategies}" if strategies else ""
        self.group_info_var.set(
            f"Group {gid} | {len(group['variants'])} variants | "
            f"Total freq: {total_freq} | Suggested: {group['suggested_standard']}"
            f"{strategy_label}"
        )

        # 变体列表
        for item in self.detail_tree.get_children():
            self.detail_tree.delete(item)

        selected_std = group.get('selected_standard', group['suggested_standard'])
        merge_flags = group.get('merge_flags', None)
        for i, v in enumerate(group['variants']):
            pct = v['freq'] / total_freq * 100 if total_freq > 0 else 0
            is_std = '●' if v['keyword'] == selected_std else ''
            if merge_flags is not None:
                is_merge = 'Y' if merge_flags.get(v['keyword'], False) else ''
            else:
                is_merge = 'Y' if v['keyword'] != selected_std else ''
            self.detail_tree.insert('', tk.END, iid=str(i),
                                    values=(is_std, is_merge, v['keyword'], v['freq'], f'{pct:.1f}%'))

        # 操作状态
        self.action_var.set(group.get('action', 'selective'))
        self.custom_std_var.set(group.get('custom_standard', ''))
        self._on_action_change()

        # 导航信息
        idx = next((i for i, g in enumerate(self.groups) if g['group_id'] == gid), 0)
        self.nav_info_var.set(f"{idx + 1} / {len(self.groups)}")

    def _on_detail_select(self, event=None):
        """Treeview行选中事件"""
        pass

    def _on_detail_click(self, event):
        """点击变体列表：点击Std列选择标准词，点击Merge列切换合并勾选"""
        region = self.detail_tree.identify_region(event.x, event.y)
        if region != 'cell':
            return
        column = self.detail_tree.identify_column(event.x)
        item_id = self.detail_tree.identify_row(event.y)
        if not item_id:
            return

        idx = int(item_id)
        group = self._get_current_group()
        if not group or idx >= len(group['variants']):
            return

        clicked_kw = group['variants'][idx]['keyword']

        if column == '#1':  # Std列 → 选择标准词
            group['selected_standard'] = clicked_kw
            if 'merge_flags' not in group:
                group['merge_flags'] = {}
            group['merge_flags'][clicked_kw] = False
            self._show_group_detail(self.current_group)

        elif column == '#2':  # Merge列 → 切换合并勾选
            if 'merge_flags' not in group:
                group['merge_flags'] = {}
            selected_std = group.get('selected_standard', group['suggested_standard'])
            if clicked_kw == selected_std:
                return  # 标准词本身不需要合并
            current = group['merge_flags'].get(clicked_kw, False)
            group['merge_flags'][clicked_kw] = not current
            self._show_group_detail(self.current_group)

    def _on_action_change(self):
        """操作类型变更"""
        if self.action_var.get() == 'custom':
            self.custom_std_entry.configure(state='normal')
        else:
            self.custom_std_entry.configure(state='disabled')

    def _get_current_group(self):
        """获取当前选中的群组"""
        if self.current_group is None:
            return None
        for g in self.groups:
            if g['group_id'] == self.current_group:
                return g
        return None

    # ============================================================
    # 操作
    # ============================================================

    def _apply_to_group(self):
        """对当前群组应用操作"""
        group = self._get_current_group()
        if not group:
            return

        action = self.action_var.get()
        selected_std = group.get('selected_standard', group['suggested_standard'])

        if action == 'keep':
            group['action'] = 'keep'
            group['status'] = 'kept'
            group['merge_flags'] = {v['keyword']: False for v in group['variants']}

        elif action == 'confirm':
            group['action'] = 'confirm'
            group['status'] = 'confirmed'
            group['selected_standard'] = selected_std
            group['merge_flags'] = {v['keyword']: (v['keyword'] != selected_std)
                                    for v in group['variants']}

        elif action == 'custom':
            custom = self.custom_std_var.get().strip()
            if not custom:
                messagebox.showwarning("Warning", "Please enter a custom standard keyword.")
                return
            group['action'] = 'custom'
            group['status'] = 'custom'
            group['selected_standard'] = custom
            group['merge_flags'] = {v['keyword']: (v['keyword'] != custom)
                                    for v in group['variants']}

        elif action == 'selective':
            # 使用merge_flags中的逐项勾选状态
            if 'merge_flags' not in group:
                group['merge_flags'] = {v['keyword']: (v['keyword'] != selected_std)
                                        for v in group['variants']}
            has_any_merge = any(group['merge_flags'].values())
            if not has_any_merge:
                group['action'] = 'keep'
                group['status'] = 'kept'
            else:
                group['action'] = 'selective'
                group['status'] = 'confirmed'

        # 更新Treeview
        self._update_tree_status(group['group_id'], group['status'])
        self._show_group_detail(self.current_group)

        # 自动保存审核记录
        self._save_review_record(group)

        n_merge = sum(1 for v in group['merge_flags'].values() if v)
        self.log(f"Group {group['group_id']}: {group['status']} | "
                 f"Std={group.get('selected_standard', '?')} | "
                 f"Merge {n_merge}/{len(group['variants'])}")

        # 自动跳到下一个未审核
        self._next_unreviewed()

    def _update_tree_status(self, gid, status):
        """更新Treeview中的状态显示"""
        status_text = {'unreviewed': '--', 'confirmed': 'OK', 'kept': 'KEEP', 'custom': 'EDIT'}.get(status, status)
        try:
            self.tree.set(str(gid), 'status', status_text)
        except tk.TclError:
            pass

    def _next_unreviewed(self):
        """跳到下一个未审核的群组"""
        current_idx = next((i for i, g in enumerate(self.groups)
                            if g['group_id'] == self.current_group), 0)
        for i in range(current_idx + 1, len(self.groups)):
            if self.groups[i]['has_variants'] and self.groups[i]['status'] == 'unreviewed':
                gid = self.groups[i]['group_id']
                # 选中Treeview
                try:
                    self.tree.selection_set(str(gid))
                    self.tree.see(str(gid))
                except tk.TclError:
                    pass
                self._show_group_detail(gid)
                return
        self.log("All variant groups reviewed!")

    def _prev_group(self):
        """上一个群组"""
        current_idx = next((i for i, g in enumerate(self.groups)
                            if g['group_id'] == self.current_group), 0)
        if current_idx > 0:
            gid = self.groups[current_idx - 1]['group_id']
            try:
                self.tree.selection_set(str(gid))
                self.tree.see(str(gid))
            except tk.TclError:
                pass
            self._show_group_detail(gid)

    def _next_group(self):
        """下一个群组"""
        current_idx = next((i for i, g in enumerate(self.groups)
                            if g['group_id'] == self.current_group), 0)
        if current_idx < len(self.groups) - 1:
            gid = self.groups[current_idx + 1]['group_id']
            try:
                self.tree.selection_set(str(gid))
                self.tree.see(str(gid))
            except tk.TclError:
                pass
            self._show_group_detail(gid)

    # ============================================================
    # 导出
    # ============================================================

    def _build_mapping(self):
        """从审核结果构建映射表（支持逐变体选择性合并）"""
        mapping = {}
        for g in self.groups:
            if not g['has_variants']:
                continue
            if g['action'] == 'keep':
                continue  # 保留所有变体，不合并
            standard = g.get('selected_standard', g['suggested_standard'])
            if not standard:
                continue
            merge_flags = g.get('merge_flags', {})
            for v in g['variants']:
                kw = v['keyword']
                # 只合并勾选了Merge的变体
                if kw != standard and merge_flags.get(kw, True):
                    mapping[kw] = standard
        return mapping

    def _confirm_all(self):
        """一键确认所有未审核群组（使用建议标准词，全部合并）"""
        n = 0
        for g in self.groups:
            if g['has_variants'] and g['status'] == 'unreviewed':
                g['action'] = 'confirm'
                g['status'] = 'confirmed'
                g['selected_standard'] = g['suggested_standard']
                g['merge_flags'] = {v['keyword']: (v['keyword'] != g['suggested_standard'])
                                    for v in g['variants']}
                self._update_tree_status(g['group_id'], 'confirmed')
                n += 1
        self.log(f"Auto-confirmed {n} groups with suggested standards")

    def _export_mapping(self):
        """导出映射表（包含本次审核结果+全局映射表中已有的映射）"""
        mapping = self._build_mapping()

        # 合并全局映射表
        if self.global_mapping:
            merged = dict(self.global_mapping)
            merged.update(mapping)  # 本次审核结果优先
            mapping = merged

        if not mapping:
            messagebox.showwarning("Warning", "No mapping to export. Review groups first.")
            return

        filepath = filedialog.asksaveasfilename(
            defaultextension='.csv',
            filetypes=[("CSV files", "*.csv")],
            initialdir=self.input_dir,
            initialfile='映射表_已审核.csv',
            title="Export Mapping Table"
        )
        if not filepath:
            return

        with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=['变体', '标准词'])
            writer.writeheader()
            for variant, standard in sorted(mapping.items()):
                writer.writerow({'变体': variant, '标准词': standard})

        self.log(f"Exported mapping: {filepath} ({len(mapping)} entries)")

        # 询问是否同时更新全局映射表
        if self.global_mapping is not None or messagebox.askyesno(
                "Global Mapping",
                "Update global mapping table with these results?\n\n"
                "This allows future journals to reuse these mappings."):
            self._save_to_global_mapping(mapping)

    def _apply_and_export(self):
        """应用映射表并导出最终结果"""
        mapping = self._build_mapping()
        if not mapping and not self.global_mapping:
            messagebox.showwarning("Warning", "No mapping to apply.")
            return

        # 合并全局映射表中的映射（策略0自动确认的词对也需标准化）
        if self.global_mapping:
            merged = dict(self.global_mapping)
            merged.update(mapping)  # 本次审核结果优先
            mapping = merged

        output_dir = filedialog.askdirectory(
            initialdir=self.input_dir,
            title="Select Output Directory"
        )
        if not output_dir:
            return

        # 调用引擎的apply模式
        # 先保存映射表
        mapping_path = os.path.join(output_dir, '_temp_mapping.csv')
        with open(mapping_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=['变体', '标准词'])
            writer.writeheader()
            for variant, standard in sorted(mapping.items()):
                writer.writerow({'变体': variant, '标准词': standard})

        # 查找输入路径
        # 从输出目录推断输入目录
        input_path = self.input_dir
        # 如果input_dir本身就是output，往上一级找
        parent = os.path.dirname(input_path)
        if os.path.isdir(parent):
            xml_files = [f for f in os.listdir(parent) if f.lower().endswith('.xml')]
            if xml_files:
                input_path = parent

        cmd = [PYTHON, '-u', ENGINE_SCRIPT,
               '--input', input_path,
               '--mapping', mapping_path,
               '--output', output_dir]

        self.log(f"Running: {' '.join(cmd)}")

        try:
            env = os.environ.copy()
            env['PYTHONUNBUFFERED'] = '1'
            env['PYTHONIOENCODING'] = 'utf-8'
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace',
                                    env=env, timeout=300)
            if result.stdout:
                self.log(result.stdout[-500:])
            if result.returncode != 0:
                self.log(f"Error: {result.stderr[:300]}")
                messagebox.showerror("Error", f"Analysis failed:\n{result.stderr[:300]}")
                return

            self.log("Final export complete!")
            messagebox.showinfo("Success", f"Final results exported to:\n{output_dir}")

            # 打开输出目录
            if platform.system() == 'Windows':
                os.startfile(output_dir)
            elif platform.system() == 'Darwin':
                subprocess.run(['open', output_dir])
            else:
                subprocess.run(['xdg-open', output_dir])

        except subprocess.TimeoutExpired:
            self.log("Timeout - analysis took too long")
            messagebox.showerror("Error", "Analysis timed out")
        except Exception as e:
            self.log(f"Error: {e}")
            messagebox.showerror("Error", str(e))

    # ============================================================
    # 全局映射表管理（merge + keep）
    # ============================================================

    def _load_global_mapping(self):
        """加载全局映射表CSV文件（merge决策），同时尝试加载对应的keep文件"""
        filepath = filedialog.askopenfilename(
            title="Load Global Mapping CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=self.input_dir if self.input_dir else os.path.expanduser("~")
        )
        if not filepath:
            return

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
            messagebox.showerror("Error", f"Failed to load global mapping:\n{e}")
            return

        self.global_mapping = mapping
        self.global_mapping_path = filepath

        # 尝试自动加载同目录下的 keep 文件
        keep_path = self._auto_find_keep_file(filepath)
        if keep_path:
            self._load_keep_file(keep_path)

        self._update_global_map_label()
        self.log(f"Global mapping loaded: {len(mapping)} merges from {os.path.basename(filepath)}"
                 + (f", {len(self.global_keeps)} keep groups" if self.global_keeps else ""))

        # 加载后自动确认已知群组
        n_merge, n_keep = self._auto_confirm_from_global()
        if n_merge > 0 or n_keep > 0:
            self._populate_tree()
            n_with = sum(1 for g in self.groups if g['has_variants'])
            n_reviewed = sum(1 for g in self.groups if g['has_variants'] and g['status'] != 'unreviewed')
            n_remaining = n_with - n_reviewed
            parts = []
            if n_merge: parts.append(f"{n_merge} auto-merged")
            if n_keep: parts.append(f"{n_keep} auto-kept")
            self.log(f"Auto-confirmed: {', '.join(parts)} ({n_remaining} remaining to review)")
            self.status_var.set(f"{n_with} with variants, {n_reviewed} reviewed, {n_remaining} remaining")

    def _auto_find_keep_file(self, mapping_path):
        """根据mapping文件路径自动查找keep文件
        规则：同名但后缀为 _keep.csv 或 .keep.csv
        例如 global_mapping.csv → global_mapping_keep.csv
        """
        base, ext = os.path.splitext(mapping_path)
        candidates = [
            base + '_keep' + ext,
            base + '.keep' + ext,
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    def _load_keep_file(self, filepath):
        """加载keep文件，每行一个KEEP群组，逗号分隔的词列表"""
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
            self.log(f"Warning: Failed to load keep file: {e}")
            return
        self.global_keeps = keeps
        self.global_keep_path = filepath
        self.log(f"Keep file loaded: {len(keeps)} groups from {os.path.basename(filepath)}")

    def _auto_confirm_from_global(self):
        """根据全局映射表和keep列表自动确认已知变体群组
        返回 (n_merge_confirmed, n_keep_confirmed)
        """
        n_merge = 0
        n_keep = 0

        if not self.global_mapping and not self.global_keeps:
            return (0, 0)

        # 构建全局映射中所有出现的词（变体+标准词）
        all_mapped_words = set()
        if self.global_mapping:
            all_mapped_words = set(self.global_mapping.keys()) | set(self.global_mapping.values())

        for g in self.groups:
            if not g['has_variants'] or g['status'] != 'unreviewed':
                continue

            kws = {v['keyword'] for v in g['variants']}

            # ---- 检查1：是否匹配KEEP群组 ----
            # 群组所有词都在某个keep群组中 → 自动KEEP
            kept = False
            for keep_set in self.global_keeps:
                if kws.issubset(keep_set):
                    g['status'] = 'confirmed'
                    g['action'] = 'keep'
                    g['selected_standard'] = ''
                    g['merge_flags'] = {v['keyword']: False for v in g['variants']}
                    n_keep += 1
                    kept = True
                    break

            if kept:
                continue

            # ---- 检查2：是否匹配merge映射 ----
            # 群组中每个词都在全局映射中有记录（变体或标准词），且变体的标准词也在群组中
            if not self.global_mapping:
                continue

            all_known = True
            for kw in kws:
                if kw in self.global_mapping:
                    if self.global_mapping[kw] not in kws:
                        all_known = False
                        break
                elif kw in all_mapped_words:
                    pass
                else:
                    all_known = False
                    break

            if all_known:
                g['status'] = 'confirmed'
                g['action'] = 'confirm'
                g['selected_standard'] = g['suggested_standard']
                g['merge_flags'] = {v['keyword']: (v['keyword'] != g['suggested_standard'])
                                    for v in g['variants']}
                n_merge += 1

        return (n_merge, n_keep)

    def _update_global_map_label(self):
        """更新全局映射状态标签"""
        parts = []
        if self.global_mapping:
            parts.append(f"{len(self.global_mapping)} merges")
        if self.global_keeps:
            parts.append(f"{len(self.global_keeps)} keeps")
        text = f"({', '.join(parts)})" if parts else "(none)"
        self.global_map_label.config(text=text)

    def _save_to_global_mapping(self, new_mapping):
        """将新映射追加到全局映射表文件，同时保存KEEP群组到keep文件"""
        if not self.global_mapping_path:
            filepath = filedialog.asksaveasfilename(
                title="Save Global Mapping",
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv")],
                initialfile="global_mapping.csv",
                initialdir=self.input_dir if self.input_dir else os.path.expanduser("~")
            )
            if not filepath:
                return
            self.global_mapping_path = filepath

        merged = dict(self.global_mapping)
        merged.update(new_mapping)

        # 收集本次审核中的KEEP群组
        new_keeps = []
        for g in self.groups:
            if not g['has_variants']:
                continue
            if g['action'] == 'keep' and g['status'] != 'unreviewed':
                kws = [v['keyword'] for v in g['variants']]
                new_keeps.append(set(kws))

        # 合并到已有keep列表（去重）
        existing_keeps = list(self.global_keeps)
        for nk in new_keeps:
            # 检查是否已被现有keep群组包含
            already_covered = False
            for ek in existing_keeps:
                if nk.issubset(ek):
                    already_covered = True
                    break
            if not already_covered:
                existing_keeps.append(nk)

        # 保存merge映射
        try:
            with open(self.global_mapping_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['变体', '标准词'])
                for variant, standard in sorted(merged.items()):
                    writer.writerow([variant, standard])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save global mapping:\n{e}")
            return

        # 保存keep群组（与mapping同目录，文件名加_keep后缀）
        keep_path = self._get_keep_path()
        try:
            with open(keep_path, 'w', encoding='utf-8-sig', newline='') as f:
                f.write("# KEEP groups: words in each row were decided as NOT variants\n")
                f.write("# These groups will be auto-kept when processing new journals\n")
                for keep_set in existing_keeps:
                    f.write(','.join(sorted(keep_set)) + '\n')
        except Exception as e:
            self.log(f"Warning: Failed to save keep file: {e}")

        self.global_mapping = merged
        self.global_keeps = existing_keeps
        self.global_keep_path = keep_path
        self._update_global_map_label()
        self.log(f"Global mapping saved: {len(merged)} merges, {len(existing_keeps)} keep groups")

    def _get_keep_path(self):
        """根据mapping文件路径推导keep文件路径"""
        if self.global_mapping_path:
            base, ext = os.path.splitext(self.global_mapping_path)
            return base + '_keep' + ext
        return os.path.join(self.input_dir, 'global_keep.csv')

    # ============================================================
    # 运行分析
    # ============================================================

    def _run_analysis(self):
        """运行完整分析"""
        # 弹出对话框让用户选择输入（文件或文件夹）
        choice = messagebox.askyesnocancel(
            "Select Input",
            "Select your input method:\n\n"
            "  Yes = Select a single FILE (.xml / .txt)\n"
            "  No  = Select a FOLDER (scans all .xml/.txt inside)\n"
            "  Cancel = Abort"
        )
        if choice is None:
            return

        if choice:  # Yes → select file
            input_path = filedialog.askopenfilename(
                title="Select Data File (any extension, auto-detect XML content)",
                filetypes=[("Data files", "*.xml *.txt *.dat"), ("All files", "*.*")]
            )
        else:  # No → select folder
            input_path = filedialog.askdirectory(
                title="Select Folder with XML/TXT Files")

        if not input_path:
            return

        output_dir = filedialog.askdirectory(title="Select Output Directory")
        if not output_dir:
            return

        cmd = [PYTHON, '-u', ENGINE_SCRIPT,
               '--input', input_path,
               '--output', output_dir]

        # 如果已加载全局映射表，传递给分析脚本
        if self.global_mapping and self.global_mapping_path:
            cmd.extend(['--global-mapping', self.global_mapping_path])
            # 同时传递keep文件（如果存在）
            if self.global_keep_path and os.path.exists(self.global_keep_path):
                cmd.extend(['--global-keep', self.global_keep_path])

        self.log(f"Running: {' '.join(cmd)}")

        def worker():
            try:
                env = os.environ.copy()
                env['PYTHONUNBUFFERED'] = '1'
                env['PYTHONIOENCODING'] = 'utf-8'
                # 使用 Popen 实时读取输出
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        env=env, encoding='utf-8', errors='replace')
                # 实时逐行读取并显示
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        self.root.after(0, lambda l=line: self.log(l))
                proc.wait()

                if proc.returncode != 0:
                    self.root.after(0, lambda: self.log(
                        f"Analysis failed (exit code {proc.returncode})"))
                    self.root.after(0, lambda: messagebox.showerror(
                        "Error", f"Analysis failed. Check log for details."))
                    return

                # 检查输出文件是否生成
                groups_file = None
                for fname in ['3_变体群组_待审核.csv', '3_变体群组.csv']:
                    fpath = os.path.join(output_dir, fname)
                    if os.path.exists(fpath):
                        groups_file = fpath
                        break

                if not groups_file:
                    self.root.after(0, lambda: self.log(
                        "Warning: Analysis completed but no variant groups file found."))
                    self.root.after(0, lambda: self.log(
                        "Possible causes: no XML files detected, or all records have no keywords."))
                    self.root.after(0, lambda: messagebox.showwarning(
                        "Warning", "Analysis completed but no results generated.\n"
                        "Check that the input folder contains valid XML files."))
                    return

                self.input_dir = output_dir
                self.root.after(0, self._load_data)
                self.root.after(0, lambda: self.log("Analysis complete!"))
            except Exception as e:
                self.root.after(0, lambda: self.log(f"Error: {e}"))
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))

        import threading
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    # ============================================================
    # 日志
    # ============================================================

    def log(self, msg):
        self.log_text.configure(state='normal')
        self.log_text.insert(tk.END, msg + '\n')
        self.log_text.see(tk.END)
        self.log_text.configure(state='disabled')


def main():
    parser = argparse.ArgumentParser(description='关键词清洗工具 - GUI审核界面')
    parser.add_argument('--input', '-i', default='.',
                        help='分析输出目录路径')
    parser.add_argument('--mapping', '-m', default=None,
                        help='映射表CSV文件路径')
    args = parser.parse_args()

    root = tk.Tk()
    app = ReviewApp(root, args.input, args.mapping)
    root.mainloop()


if __name__ == '__main__':
    main()
