# -*- coding: utf-8 -*-
"""
人工智能训练师三级操作题 - 自动对答案系统
用法：双击 对答案.bat，或用 Python 运行本文件。
作答方式：把每道题的结果文件放进 我的作答\题号\ 文件夹（ipynb 需在 Jupyter 中运行并保存）。
"""
import os
import re
import sys
import json
import zipfile
import html as htmllib
import shutil
import subprocess
import hashlib
import datetime
from collections import Counter

if not sys.stdout.isatty():
    # 输出被重定向/管道捕获时用 UTF-8；双击运行时走控制台 API 自动支持中文
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ---------------- 路径 ----------------
SYSTEM_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(SYSTEM_DIR)                      # 题库根目录
ANS_DIR = os.path.join(BASE, '人工智能训练师三级操作题答案最新')
FILES_DIR = os.path.join(BASE, 'files')
MAT_DIR = os.path.join(BASE, 'files', '素材')
MY_DIR = os.path.join(BASE, '我的作答')
CACHE_DIR = os.path.join(SYSTEM_DIR, '缓存')
RUNS_DIR = os.path.join(CACHE_DIR, 'runs')
REPORT_PATH = os.path.join(BASE, '对答案报告.html')
MANIFEST = os.path.join(CACHE_DIR, 'manifest.json')
PY = sys.executable

QIDS = ['%d.%d.%d' % (c, s, q) for c in range(1, 5) for s in (1, 2) for q in range(1, 6)]

# 参考代码补丁（修正答案文档中的笔误/过时写法）
PATCHES = {
    '2.1.4': [
        ("plot(kind='bar', stacked=True, fontproperties=my_font)", "plot(kind='bar', stacked=True)"),
        ("plt.scatter(figsize=(10, 6))", "plt.figure(figsize=(10, 6))"),
    ],
    '2.2.2': [
        ("\nresults_rf_file.write(f'测试集得分: {rf_model.score(X_test, y_test)}\\n')",
         "\n    results_rf_file.write(f'测试集得分: {rf_model.score(X_test, y_test)}\\n')"),
    ],
    '3.2.3': [("Image.ANTIALIAS", "Image.LANCZOS")],
}

# ---------------- docx 解析 ----------------
def docx_text(path):
    xml = zipfile.ZipFile(path).read('word/document.xml').decode('utf-8')
    text = re.sub(r'</w:p>', '\n', xml)
    text = re.sub(r'<w:br/>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    return htmllib.unescape(text)

def docx_tables(path):
    try:
        xml = zipfile.ZipFile(path).read('word/document.xml').decode('utf-8')
    except Exception:
        return []
    tables = []
    for tbl in re.findall(r'<w:tbl>.*?</w:tbl>', xml, re.S):
        rows = []
        for tr in re.findall(r'<w:tr(?:\s[^>]*)?>.*?</w:tr>', tbl, re.S):
            cells = []
            for tc in re.findall(r'<w:tc>.*?</w:tc>', tr, re.S):
                t = re.sub(r'</w:p>', '\n', tc)
                t = re.sub(r'<[^>]+>', '', t)
                cells.append(htmllib.unescape(t).strip())
            rows.append(cells)
        tables.append(rows)
    return tables

STOP_RE = [
    re.compile(r'^[（【「『]'),
    re.compile(r'^[\u4e00-\u9fff]'),
    re.compile(r'^\d+(\.\d+)+[-\d]*\.(jpg|jpeg|png|docx|html|csv|py|ipynb)'),
    re.compile(r'^\d+[、.．]'),
    re.compile(r'^[①②③④⑤⑥⑦⑧⑨⑩]'),
]

def _is_prose(s):
    return any(r.match(s) for r in STOP_RE)

def extract_code(text):
    """从答案 docx 文本中提取参考 Python 代码"""
    m = re.search(r'参考答案[：:]', text)
    if not m:
        return None
    tail = text[m.end():]
    im = re.search(r'(import\s+\w+|from\s+\w+\s+import)', tail)
    if not im:
        return None
    lines = tail[im.start():].split('\n')
    code_lines = []
    for ln in lines:
        s = ln.strip()
        if s != '' and _is_prose(s):
            break
        code_lines.append(ln)
    while code_lines and code_lines[-1].strip() == '':
        code_lines.pop()
    code = '\n'.join(code_lines)
    try:
        compile(code, 'ref', 'exec')
        return code
    except SyntaxError:
        return None

def extract_ref_text(text):
    """提取参考答案全文（写作题）"""
    m = re.search(r'参考答案[：:]', text)
    tail = text[m.end():] if m else text
    lines = []
    for ln in tail.split('\n'):
        s = ln.strip()
        if not s:
            continue
        if any(k in s for k in ('请勿修改', '指定单元格', '在下划线', '答题卷')):
            continue
        lines.append(s)
    return '\n'.join(lines)

def extract_rubric(tables):
    """提取测量分评分表：返回 (M行列表, 合计配分)"""
    rubric, total = [], None
    for rows in tables:
        flat = ' '.join(' '.join(r) for r in rows[:3])
        if '细则编号' not in flat:
            continue
        for r in rows:
            if len(r) >= 3 and re.match(r'^M\d+$', r[0]):
                try:
                    score = float(r[1])
                except (ValueError, IndexError):
                    score = 1
                rubric.append((r[0], score, r[2]))
            if len(r) >= 2 and '合计配分' in r[0]:
                for cell in r[1:]:
                    mm = re.search(r'-?\d+(\.\d+)?', cell)
                    if mm:
                        total = float(mm.group())
                        break
        break
    if total is None and rubric:
        total = sum(s for _, s, _ in rubric)
    return rubric, total

# ---------------- 题目 HTML 解析 ----------------
def parse_question_html(qid):
    path = os.path.join(FILES_DIR, qid + '.html')
    title, deliverables = qid, []
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            content = fh.read()
        mt = re.search(r'<title>([^<]+)</title>', content)
        if mt:
            title = mt.group(1).strip()
        seen = set()
        for name in re.findall(r'上传\s*([A-Za-z0-9_\-\.]{2,})', content):
            if name not in seen:
                seen.add(name)
                deliverables.append(name)
    except OSError:
        pass
    return title, deliverables

# ---------------- 参考代码运行（带缓存） ----------------
def md5_file(path):
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def run_reference(qid, code, force=False):
    """在隔离目录运行参考代码，返回 dict(ok, stdout, stderr, cached)"""
    os.makedirs(RUNS_DIR, exist_ok=True)
    rundir = os.path.join(RUNS_DIR, qid)
    src_dir = os.path.join(MAT_DIR, qid)
    # 计算缓存键：代码 + 素材数据文件
    key_src = [code]
    if os.path.isdir(src_dir):
        for item in sorted(os.listdir(src_dir)):
            p = os.path.join(src_dir, item)
            if os.path.isfile(p) and not item.endswith(('.ipynb', '.docx')):
                key_src.append(item + ':' + md5_file(p))
    key = hashlib.md5('\n'.join(key_src).encode('utf-8')).hexdigest()

    manifest = {}
    if os.path.exists(MANIFEST):
        try:
            with open(MANIFEST, encoding='utf-8') as fh:
                manifest = json.load(fh)
        except Exception:
            manifest = {}
    cached = manifest.get(qid)
    if cached and cached.get('key') == key and not force:
        cached['cached'] = True
        return cached

    # 准备隔离运行目录
    if os.path.exists(rundir):
        shutil.rmtree(rundir)
    os.makedirs(rundir)
    if os.path.isdir(src_dir):
        for item in os.listdir(src_dir):
            s = os.path.join(src_dir, item)
            if os.path.isfile(s):
                shutil.copy2(s, os.path.join(rundir, item))
    script = os.path.join(rundir, '_ref.py')
    with open(script, 'w', encoding='utf-8') as fh:
        fh.write('import matplotlib\nmatplotlib.use("Agg")\n' + code)
    try:
        r = subprocess.run([PY, '-X', 'utf8', script], cwd=rundir,
                           capture_output=True, timeout=300)
        result = {
            'key': key, 'ok': r.returncode == 0,
            'stdout': r.stdout.decode('utf-8', errors='replace'),
            'stderr': r.stderr.decode('utf-8', errors='replace')[-800:],
        }
    except subprocess.TimeoutExpired:
        result = {'key': key, 'ok': False, 'stdout': '', 'stderr': '运行超时（>300秒）'}
    result['cached'] = False
    manifest[qid] = result
    with open(MANIFEST, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    return result

# ---------------- 用户作答读取 ----------------
def read_ipynb(path):
    """读取 ipynb：返回 (stdout文本, display文本, 错误列表, 警告列表, 是否有输出, 是否还有未填空)"""
    import json as _json
    try:
        with open(path, encoding='utf-8') as fh:
            nb = _json.load(fh)
    except Exception as e:
        return '', '', ['ipynb 文件读取失败: %s' % e], [], False, False
    out_stream, out_result, errors, warnings = [], [], [], []
    has_output, blanks_left = False, False
    for cell in nb.get('cells', []):
        if cell.get('cell_type') != 'code':
            continue
        src = ''.join(cell.get('source', []))
        if re.search(r'_{6,}', src):
            blanks_left = True
        for out in cell.get('outputs', []):
            has_output = True
            otype = out.get('output_type')
            if otype == 'stream':
                txt = out.get('text', '')
                if isinstance(txt, list):
                    txt = ''.join(txt)
                if out.get('name') == 'stderr':
                    if ('Warning' in txt or 'warnings.warn' in txt) and 'Traceback' not in txt:
                        warnings.append(txt)
                    else:
                        errors.append(txt)
                else:
                    out_stream.append(txt)
            elif otype in ('execute_result', 'display_data'):
                tp = out.get('data', {}).get('text/plain', '')
                if isinstance(tp, list):
                    tp = ''.join(tp)
                out_result.append(tp)
            elif otype == 'error':
                errors.append('%s: %s' % (out.get('ename', 'Error'), out.get('evalue', '')))
    return '\n'.join(out_stream), '\n'.join(out_result), errors, warnings, has_output, blanks_left

def find_user_ipynb(qid):
    cands = [os.path.join(MY_DIR, qid, qid + '.ipynb'),
             os.path.join(MAT_DIR, qid, qid + '.ipynb')]
    for p in cands:
        if os.path.exists(p):
            return p
    return None

def find_user_docx(qid):
    folder = os.path.join(MY_DIR, qid)
    if not os.path.isdir(folder):
        return None
    preferred = os.path.join(folder, qid + '.docx')
    if os.path.exists(preferred):
        return preferred
    for f in sorted(os.listdir(folder)):
        if f.lower().endswith('.docx') and not f.startswith('~'):
            return os.path.join(folder, f)
    return None

def check_deliverables(qid, deliverables):
    """检查交付文件是否存在于 我的作答\题号\ 或 素材\题号\（排除题目文件本身）"""
    result = []
    for name in deliverables:
        loc = None
        for folder in (os.path.join(MY_DIR, qid), os.path.join(MAT_DIR, qid)):
            if name in (qid + '.docx', qid + '.ipynb'):
                continue  # 素材里的同名 docx/ipynb 是题目文件，不算交付
            if os.path.isfile(os.path.join(folder, name)):
                loc = os.path.relpath(folder, BASE)
                break
        result.append((name, loc))
    return result

# ---------------- 数字比对 ----------------
NUM_RE = re.compile(r'(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')

def meaningful_lines(text):
    keep = []
    for ln in text.splitlines():
        s = ln.rstrip()
        if not s.strip() or 'dtype:' in s or 'dtype=' in s:
            continue
        keep.append(s)
    return keep

def nums_in(line):
    return [float(x) for x in NUM_RE.findall(line)]

def close(a, b):
    return abs(a - b) <= max(1e-9, 1e-6 * max(abs(a), abs(b)))

def compare_outputs(ref_text, user_text):
    """逐行比对：有数字的行按数字匹配，纯文本行按整行匹配。返回 (行状态, 匹配行数, 总行数)"""
    ref_lines = meaningful_lines(ref_text)
    user_lines = meaningful_lines(user_text)
    user_norm = set(re.sub(r'\s+', ' ', l).strip() for l in user_lines)
    pool = [n for l in user_lines for n in nums_in(l)]
    used = [False] * len(pool)
    line_status, matched, total = [], 0, 0
    for ln in ref_lines:
        nums = nums_in(ln)
        total += 1
        if nums:
            ok_all = True
            for n in nums:
                found = False
                for i, u in enumerate(pool):
                    if not used[i] and close(n, u):
                        used[i] = True
                        found = True
                        break
                if not found:
                    ok_all = False
            if ok_all:
                matched += 1
            line_status.append((ln, ok_all))
        else:
            norm = re.sub(r'\s+', ' ', ln).strip()
            ok = norm in user_norm
            if ok:
                matched += 1
            line_status.append((ln, ok))
    return line_status, matched, total

# ---------------- 写作题关键词 ----------------
STOP_WORDS = {'如果', '可以', '通过', '进行', '使用', '以及', '包括', '需要', '根据', '然后',
              '同时', '一个', '不仅', '因此', '为了', '或者', '对于', '能够', '应该', '建议',
              '提供', '采用', '实现', '相关', '不同', '以上', '如下', '例如', '注意', '要求',
              '并且', '从而', '避免', '提升', '增强', '确保', '参考答案', '试题', '考生',
              '答案', '满分', '得分', '评分', '细则', '描述', '正确', '全部', '回答'}

def extract_keywords(text, top=24):
    words = re.findall(r'[\u4e00-\u9fff]{2,6}', text)
    cnt = Counter(w for w in words if w not in STOP_WORDS)
    return [w for w, _ in cnt.most_common(top)]

# ---------------- 报告生成 ----------------
ESC = lambda s: htmllib.escape(str(s))

CSS = """
body{font-family:'Microsoft YaHei',Arial,sans-serif;margin:0;background:#f0f2f5;color:#2c3e50;}
.header{background:linear-gradient(135deg,#2A2F45,#3D405B);color:#E6AF2E;padding:28px 40px;}
.header h1{margin:0;font-size:26px;}
.header .sub{color:#cfd3e4;font-size:13px;margin-top:8px;}
.wrap{max-width:1200px;margin:20px auto;padding:0 20px;}
.cards{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px;}
.card{flex:1;min-width:180px;background:#fff;border-radius:10px;padding:18px 22px;box-shadow:0 2px 6px rgba(0,0,0,.08);}
.card .num{font-size:30px;font-weight:700;}
.card .lbl{font-size:13px;color:#7f8c8d;margin-top:4px;}
.box{background:#fff;border-radius:10px;padding:20px 26px;box-shadow:0 2px 6px rgba(0,0,0,.08);margin-bottom:20px;}
h2{border-left:5px solid #E6AF2E;padding-left:12px;font-size:19px;}
table{border-collapse:collapse;width:100%;font-size:14px;}
th{background:#2A2F45;color:#E6AF2E;padding:9px 12px;text-align:left;}
td{border-bottom:1px solid #ecf0f1;padding:8px 12px;}
tr:hover td{background:#fafbfc;}
.badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;color:#fff;}
.b-ok{background:#27ae60;} .b-warn{background:#e67e22;} .b-bad{background:#c0392b;}
.b-na{background:#95a5a6;} .b-code{background:#2980b9;} .b-text{background:#8e44ad;}
.score{font-weight:700;}
.s-good{color:#27ae60;} .s-mid{color:#e67e22;} .s-bad{color:#c0392b;}
a{color:#2980b9;text-decoration:none;} a:hover{text-decoration:underline;}
details{border:1px solid #e0e4e8;border-radius:8px;margin:10px 0;background:#fff;}
details summary{cursor:pointer;padding:12px 18px;font-size:15px;font-weight:600;background:#fafbfc;border-radius:8px;}
details .inner{padding:6px 20px 18px;}
pre{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:12px;overflow:auto;font-size:12.5px;white-space:pre-wrap;word-break:break-all;max-height:420px;}
.cols{display:flex;gap:14px;flex-wrap:wrap;}
.cols>div{flex:1;min-width:320px;}
.ok-line{color:#27ae60;} .bad-line{color:#c0392b;font-weight:600;}
.chip{display:inline-block;margin:2px;padding:3px 10px;border-radius:12px;font-size:12.5px;}
.chip-hit{background:#d5f5e3;color:#1e8449;} .chip-miss{background:#eaecee;color:#7b7d7d;}
.hint{background:#fef9e7;border:1px solid #f7dc6f;border-radius:6px;padding:10px 14px;font-size:13px;margin:8px 0;}
.err{background:#fdedec;border:1px solid #f5b7b1;border-radius:6px;padding:10px 14px;font-size:13px;}
.filename{font-family:Consolas,monospace;background:#f0f3f5;padding:1px 6px;border-radius:4px;}
.note{font-size:12.5px;color:#909497;}
"""

def badge(text, cls):
    return '<span class="badge %s">%s</span>' % (cls, ESC(text))

def rubric_html(rubric):
    if not rubric:
        return '<p class="note">未在答案文档中找到评分细则表</p>'
    rows = ''.join(
        '<tr><td>%s</td><td>%s</td><td>%s</td></tr>' % (ESC(a), ESC(b if b == int(b) else b), ESC(c))
        for a, b, c in rubric)
    return ('<table><tr><th>细则编号</th><th>配分</th><th>评分细则描述</th></tr>%s</table>'
            % rows)

def details_block(summary, inner, open_=False):
    return '<details%s><summary>%s</summary><div class="inner">%s</div></details>' % (
        ' open' if open_ else '', summary, inner)

def render_question(qid, info):
    d = []
    title_line = '<b>%s %s</b> &nbsp;%s %s' % (
        ESC(qid), ESC(info['title']),
        badge('编程题' if info['type'] == 'CODE' else '写作题',
              'b-code' if info['type'] == 'CODE' else 'b-text'),
        badge(info['status_label'], info['status_cls']))
    d.append(title_line)

    # 交付文件
    if info['deliverables']:
        rows = []
        for name, loc in info['deliverables']:
            if loc:
                rows.append('<tr><td><span class="filename">%s</span></td><td>%s</td><td>%s ✓</td></tr>'
                            % (ESC(name), ESC(loc), '已找到'))
            else:
                rows.append('<tr><td><span class="filename">%s</span></td><td>-</td><td style="color:#c0392b">缺失</td></tr>'
                            % ESC(name))
        d.append('<h4>交付文件检查</h4><table><tr><th>文件</th><th>位置</th><th>状态</th></tr>%s</table>'
                 % ''.join(rows))
    else:
        d.append('<p class="note">未能从试题页面解析出交付文件清单</p>')

    if info['type'] == 'CODE':
        # 作答状态提示
        if info.get('blanks_left'):
            d.append('<div class="hint">⚠ 检测到 ipynb 中仍有未填写的空（下划线），请补全后再运行。</div>')
        if not info.get('attempted'):
            d.append('<div class="hint">尚未检测到运行输出。做法：在 Jupyter 中补全代码、运行全部单元格、'
                     'Ctrl+S 保存，然后把 ipynb（及截图等交付文件）复制到 <span class="filename">我的作答\\%s\\</span>。'
                     '（直接在素材文件夹里作答也可以被检测到）</div>' % ESC(qid))
        if info.get('errors'):
            d.append('<div class="err"><b>运行报错：</b><br>%s</div>' %
                     ESC('<br>'.join(e[:300] for e in info['errors'][:5])))
        if info.get('warnings'):
            d.append('<div class="hint"><b>运行警告（不影响判分，可忽略）：</b><br>%s</div>' %
                     ESC('<br>'.join(w[:200] for w in info['warnings'][:3])))
        if info.get('attempted'):
            # 得分估算
            sc = info['score']
            cls = 's-good' if sc >= 0.8 * info['full'] else ('s-mid' if sc >= 0.5 * info['full'] else 's-bad')
            if info.get('blanks_left'):
                d.append('<h4>自动比对结果：<span class="score s-bad">0</span>（有空未填，不计分）</h4>'
                         '<p class="note">检测到 ipynb 中仍有未填写的空，请补全并重新运行后再对答案。</p>')
            else:
                d.append('<h4>自动比对结果：<span class="score %s">%.1f</span> / 满分 %.0f 分（估算）</h4>'
                         % (cls, sc, info['full']))
                d.append('<p class="note">估算方式：你的输出匹配到 %d / %d 行参考输出。'
                         '评分细则中的文件保存项需按上方清单自查。</p>' % (info['matched'], info['total']))
            # 逐行比对
            lines = []
            for ln, ok in info['line_status']:
                lines.append('<span class="%s">%s %s</span>' %
                             ('ok-line' if ok else 'bad-line', '✓' if ok else '✗', ESC(ln)))
            cmp_block = ('<div class="cols"><div><h4>参考输出（逐行核对）</h4><pre>%s</pre></div>'
                         '<div><h4>你的输出</h4><pre>%s</pre></div></div>'
                         % ('\n'.join(lines) if lines else '（无带数字的输出行）',
                            ESC(info['user_out'][:4000]) or '（空）'))
            d.append(cmp_block)
        # 参考代码 + 参考输出
        ref_out = ''
        if info.get('ref_note'):
            ref_out += '<div class="err"><b>参考代码运行失败（不影响你已保存的作答检查）：</b><br>%s</div>' % ESC(info['ref_note'])
        if info.get('ref_stdout'):
            ref_out += '<h4>参考代码运行输出</h4><pre>%s</pre>' % ESC(info['ref_stdout'][:4000])
        d.append(details_block('查看参考代码 / 参考输出',
                               '<h4>参考代码（已从答案文档提取并修正笔误）</h4><pre>%s</pre>%s'
                               % (ESC(info['ref_code']), ref_out)))
    else:
        # 写作题
        if info.get('user_docx_text'):
            d.append('<h4>你的答案（提取自 docx，共 %d 字）</h4><pre>%s</pre>'
                     % (len(info['user_docx_text']), ESC(info['user_docx_text'][:5000])))
            kws = info.get('keywords', [])
            if kws:
                hits = ''.join('<span class="chip chip-hit">%s</span>' % ESC(w) for w in kws if w in info['user_docx_text'])
                miss = ''.join('<span class="chip chip-miss">%s</span>' % ESC(w) for w in kws if w not in info['user_docx_text'])
                d.append('<h4>参考答案关键词覆盖（软指标，仅供参考）</h4>%s%s'
                         '<p class="note">绿色=你的答案已覆盖，灰色=未覆盖。写作题以人工对照评分细则自评为准。</p>'
                         % (hits, miss))
        else:
            d.append('<div class="hint">尚未找到你的答案文件。请把你的 <span class="filename">%s.docx</span> '
                     '放入 <span class="filename">我的作答\\%s\\</span> 文件夹。</div>' % (ESC(qid), ESC(qid)))
        d.append(details_block('查看参考答案（做完再看）',
                               '<pre>%s</pre>' % ESC(info.get('ref_text', '（未提取到）')[:8000])))
    # 评分细则
    d.append(details_block('查看评分细则', rubric_html(info.get('rubric'))))
    return details_block(title_line, ''.join(x for x in d if x), open_=info.get('attempted', False))

# ---------------- 主流程 ----------------
def main():
    os.makedirs(MY_DIR, exist_ok=True)
    for qid in QIDS:
        os.makedirs(os.path.join(MY_DIR, qid), exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    results = []
    code_done = 0
    for qid in QIDS:
        info = {'qid': qid, 'title': qid, 'type': 'TEXT', 'deliverables': [],
                'attempted': False, 'status_label': '未作答', 'status_cls': 'b-na',
                'score': 0.0, 'full': 0, 'matched': 0, 'total': 0, 'line_status': []}
        try:
            ans_path = os.path.join(ANS_DIR, '人工智能训练师三级操作题答案%s.docx' % qid)
            text = docx_text(ans_path) if os.path.exists(ans_path) else ''
            code = extract_code(text) if text else None
            title, deliverables = parse_question_html(qid)
            info['title'], info['deliverables'] = title, check_deliverables(qid, deliverables)
            rubric, total = extract_rubric(docx_tables(ans_path)) if os.path.exists(ans_path) else ([], None)
            info['rubric'] = rubric
            info['full'] = total if total else (sum(s for _, s, _ in rubric) if rubric else 0)
            info['ref_text'] = extract_ref_text(text) if text else ''

            if code:
                info['type'] = 'CODE'
                for old, new in PATCHES.get(qid, []):
                    code = code.replace(old, new)
                info['ref_code'] = code
                code_done += 1
                print('[%s] 运行参考代码 ... ' % qid, end='', flush=True)
                ref = run_reference(qid, code)
                info['ref_stdout'] = ref['stdout']
                print('OK(缓存)' if ref['cached'] else ('OK' if ref['ok'] else '参考代码运行失败'))
                if not ref['ok']:
                    info['ref_note'] = ref['stderr']

                ipynb = find_user_ipynb(qid)
                if ipynb:
                    stdout, disp, errors, warns, has_out, blanks = read_ipynb(ipynb)
                    info['errors'] = errors
                    info['warnings'] = warns
                    info['blanks_left'] = blanks
                    info['user_out'] = stdout + ('\n' + disp if disp else '')
                    info['attempted'] = has_out
                    if has_out:
                        line_status, matched, total_items = compare_outputs(ref['stdout'], info['user_out'])
                        info['line_status'], info['matched'], info['total'] = line_status, matched, total_items
                        full = info['full'] or total_items
                        info['full'] = full
                        if blanks:
                            info['score'] = 0.0
                        else:
                            info['score'] = round(matched / total_items * full, 1) if total_items else 0.0
                        if errors:
                            info['status_label'], info['status_cls'] = '已作答·有报错', 'b-warn'
                        elif blanks:
                            info['status_label'], info['status_cls'] = '已作答·有空未填', 'b-warn'
                        else:
                            info['status_label'], info['status_cls'] = '已作答', 'b-ok'
                    elif not blanks:
                        info['status_label'], info['status_cls'] = '已填码未运行', 'b-warn'
            else:
                # 写作题
                udocx = find_user_docx(qid)
                if udocx:
                    info['user_docx_text'] = docx_text(udocx).strip()
                    info['keywords'] = extract_keywords(info['ref_text'])
                    info['attempted'] = True
                    info['status_label'], info['status_cls'] = '已作答', 'b-ok'
        except Exception as e:
            info['status_label'], info['status_cls'] = '检查出错', 'b-bad'
            info['error_msg'] = str(e)
        results.append(info)

    build_report(results)

def build_report(results):
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    attempted = [r for r in results if r['attempted']]
    code_att = [r for r in attempted if r['type'] == 'CODE']
    text_att = [r for r in attempted if r['type'] == 'TEXT']
    code_score = sum(r['score'] for r in code_att)
    code_full = sum(r['full'] for r in code_att)

    # 汇总表
    rows = []
    for r in results:
        if r['attempted'] and r['type'] == 'CODE':
            sc = ('<span class="score s-good">%.1f</span>' % r['score']) if r['full'] else ''
            sc += (' / %.0f' % r['full']) if r['full'] else ''
            score_html = sc
        elif r['attempted']:
            score_html = '<span class="note">写作题·请自评</span>'
        else:
            score_html = '<span class="note">-</span>'
        rows.append('<tr><td><a href="#q_%s">%s</a></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                    % (r['qid'].replace('.', '_'), ESC(r['qid']), ESC(r['title']),
                       badge('编程' if r['type'] == 'CODE' else '写作',
                             'b-code' if r['type'] == 'CODE' else 'b-text'),
                       badge(r['status_label'], r['status_cls']), score_html))
    summary = ('<table><tr><th>题号</th><th>试题名称</th><th>类型</th><th>状态</th><th>得分估算</th></tr>%s</table>'
               % ''.join(rows))

    cards = """
    <div class="cards">
      <div class="card"><div class="num">%d / 40</div><div class="lbl">已作答题目</div></div>
      <div class="card"><div class="num">%d</div><div class="lbl">编程题已作答（共19题）</div></div>
      <div class="card"><div class="num">%s</div><div class="lbl">编程题估算得分（仅已作答）</div></div>
      <div class="card"><div class="num">%d</div><div class="lbl">写作题已作答（共21题，需自评）</div></div>
    </div>""" % (len(attempted), len(code_att),
                 ('%.1f' % code_score) + (' / %.0f' % code_full if code_full else ''), len(text_att))

    usage = """
    <div class="box"><h2>使用方法</h2>
    <ol style="line-height:1.9">
      <li><b>编程题</b>：在 Jupyter 中打开题目 ipynb → 补全下划线代码 → 运行全部单元格 → Ctrl+S 保存 →
          把 ipynb 复制到 <span class="filename">我的作答\题号\</span>（例如 我的作答\1.1.1\1.1.1.ipynb）。
          直接在 files\素材\题号\ 里作答也可以被检测。</li>
      <li><b>写作题</b>：把你的 docx 答案放入 <span class="filename">我的作答\题号\</span>，文件名建议为 题号.docx。</li>
      <li><b>截图等交付文件</b>（1.1.1-1.jpg 等）：一并放入 <span class="filename">我的作答\题号\</span>。</li>
      <li>双击 <span class="filename">对答案.bat</span> 重新生成本报告。参考代码运行结果已缓存，重复检查很快。</li>
    </ol>
    <p class="note">说明：编程题得分为自动估算（按输出数字比对），写作题请展开题号对照参考答案与评分细则自评。
    估算不含"文件保存规范"类评分项。</p></div>"""

    q_html = []
    for r in results:
        block = render_question(r['qid'], r)
        q_html.append('<div id="q_%s"></div>%s' % (r['qid'].replace('.', '_'), block))

    doc = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>人工智能训练师三级 · 自动对答案报告</title><style>%s</style></head>
<body>
<div class="header"><h1>人工智能训练师三级 · 自动对答案报告</h1>
<div class="sub">生成时间：%s &nbsp;|&nbsp; 编程题自动判分 · 写作题对照自评</div></div>
<div class="wrap">%s %s
<div class="box"><h2>题目总览</h2>%s</div>
<div class="box"><h2>逐题详情</h2>%s</div>
</div></body></html>""" % (CSS, now, usage, cards, summary, '\n'.join(q_html))

    with open(REPORT_PATH, 'w', encoding='utf-8') as fh:
        fh.write(doc)
    print()
    print('=' * 50)
    print('报告已生成：%s' % REPORT_PATH)
    print('已作答 %d/40 题；编程题估算得分 %.1f%s' % (
        len(attempted), code_score, (' / %.0f' % code_full) if code_full else ''))
    try:
        os.startfile(REPORT_PATH)
    except Exception:
        pass

if __name__ == '__main__':
    main()
