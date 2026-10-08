# -*- coding: utf-8 -*-
"""生成答题页面.html：在网页上直接填空判分，无需 Jupyter
用法：python 生成答题页面.py [--selftest]
"""
import os
import re
import sys
import json
import base64
import html as htmllib
import importlib.util
import shutil
import datetime

if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

SYSTEM_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(SYSTEM_DIR)

spec = importlib.util.spec_from_file_location('engine', os.path.join(SYSTEM_DIR, '对答案引擎.py'))
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

MAT_DIR = engine.MAT_DIR
ANS_DIR = engine.ANS_DIR
FILES_DIR = engine.FILES_DIR
SNAP_DIR = os.path.join(SYSTEM_DIR, '模板快照')
OUT_PATH = os.path.join(BASE, '答题页面.html')

BLANK_RE = re.compile(r'_{4,}')
DIRTY_RE = re.compile(r'[A-Za-z0-9]+_{4,}|_{4,}[A-Za-z0-9]+')

# ---------------- 模板解析（优先用快照，防用户在Jupyter里改动） ----------------
def ipynb_code_text(path):
    with open(path, encoding='utf-8') as fh:
        nb = json.load(fh)
    parts = []
    for c in nb.get('cells', []):
        if c.get('cell_type') == 'code':
            parts.append(''.join(c.get('source', [])))
    return '\n\n'.join(parts)

def resolve_template(qid):
    """返回干净的模板代码：快照 > checkpoint > 素材"""
    snap = os.path.join(SNAP_DIR, qid + '.ipynb')
    live = os.path.join(MAT_DIR, qid, qid + '.ipynb')
    ckpt = os.path.join(MAT_DIR, qid, '.ipynb_checkpoints', qid + '-checkpoint.ipynb')
    os.makedirs(SNAP_DIR, exist_ok=True)
    if os.path.exists(snap):
        return ipynb_code_text(snap)
    for cand in (live, ckpt):
        if os.path.exists(cand):
            try:
                src = open(cand, encoding='utf-8').read()
                if not DIRTY_RE.search(src):
                    shutil.copy2(cand, snap)
                    return ipynb_code_text(snap)
            except Exception:
                continue
    # 都被改过则原样使用
    if os.path.exists(live):
        return ipynb_code_text(live)
    return ''

# ---------------- 模板/参考代码对齐 ----------------
def strip_comment(s):
    out, in_str, q = [], None, ''
    for ch in s:
        if in_str:
            out.append(ch)
            if ch == q:
                in_str = False
        else:
            if ch in ('"', "'"):
                in_str, q = True, ch
                out.append(ch)
            elif ch == '#':
                break
            else:
                out.append(ch)
    return ''.join(out).rstrip()

def open_brackets(s):
    depth, in_str, q = 0, None, ''
    for ch in s:
        if in_str:
            if ch == q:
                in_str = False
        elif ch in ('"', "'"):
            in_str, q = True, ch
        elif ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
    return depth

def merge_continuations(lines):
    """把以 & , + | \\ 结尾或括号未闭合的行与下一行合并"""
    out, i = [], 0
    while i < len(lines):
        cur = lines[i]
        code = strip_comment(cur)
        while i + 1 < len(lines) and (open_brackets(code) > 0 or code.rstrip().endswith(('&', '+', '|', ',', '\\'))):
            i += 1
            cur = cur.rstrip() + ' ' + lines[i].strip()
            code = strip_comment(cur)
        out.append(cur)
        i += 1
    return out

def build_pattern(line, flex=False):
    parts = BLANK_RE.split(line)
    n = len(parts) - 1
    if n == 0:
        return None, 0
    pat = ''
    for i, p in enumerate(parts):
        if flex:
            # 弹性空格：字符间允许任意空白，忽略模板里的字面空格
            pat += r'\s*'.join(re.escape(ch) for ch in p if ch != ' ')
        else:
            pat += re.escape(p)
        if i < n:
            pat += '(.*?)'
    return pat, n

def head_pattern(line):
    """截止到最后一个空之后的第一个锚点（逗号/右括号），应对尾部参数差异"""
    parts = BLANK_RE.split(line)
    if len(parts) < 2:
        return None
    tail = parts[-1].lstrip()
    m = re.match(r'[^,)\]]*[,)\]]', tail)
    anchor = m.group() if m else tail[:8]
    pat = ''
    for p in parts[:-1]:
        pat += re.escape(p) + '(.*?)'
    return pat + re.escape(anchor)

def try_match(line, rline):
    """多级匹配：原样→去注释→前缀→弹性空格→截断锚点"""
    cands = []
    pat0, n = build_pattern(line)
    if not pat0:
        return None
    line_c = strip_comment(line)
    patc, _ = build_pattern(line_c)
    patf, _ = build_pattern(line, flex=True)
    pats = [pat0] + ([patc] if patc and patc != pat0 else [])
    rs = rline.rstrip()
    rs_c = strip_comment(rs)
    # 1) 全行匹配（原样/去注释）
    for p in pats:
        for r in (rs, rs_c):
            m = re.fullmatch(p, r)
            if m:
                return [g.strip() for g in m.groups()]
    # 2) 前缀匹配（原样/去注释）
    for p in pats:
        for r in (rs, rs_c):
            m = re.match(p + r'(?![A-Za-z0-9_])', r)
            if m:
                return [g.strip() for g in m.groups()]
    # 3) 弹性空格（全行/前缀）
    if patf:
        for r in (rs, rs_c):
            m = re.fullmatch(patf, r)
            if m:
                return [g.strip() for g in m.groups()]
        for r in (rs, rs_c):
            m = re.match(patf + r'(?![A-Za-z0-9_])', r)
            if m:
                return [g.strip() for g in m.groups()]
    # 4) 截断锚点前缀（应对尾部多参数）
    ph = head_pattern(line_c if patc else line)
    if ph:
        for r in (rs, rs_c):
            m = re.match(ph, r)
            if m and all(g.strip() for g in m.groups()):
                return [g.strip() for g in m.groups()]
    return None

# 个别参考代码与模板结构不一致时的手工答案（按行内容子串匹配）
MANUAL_ANSWERS = {
    '2.2.3': [('xgb_model = __________.XGB', ['xgb'])],
}

def norm_line(s):
    return re.sub(r'\s+', ' ', strip_comment(s)).strip()

def align_blanks(template_code, ref_code):
    tlines = merge_continuations(template_code.split('\n'))
    rlines = merge_continuations(ref_code.split('\n'))
    used = [False] * len(rlines)
    last = 0
    blanks = []
    unresolved = 0
    for tline in tlines:
        line = tline.rstrip()
        pat, n = build_pattern(line)
        if not pat:
            # 非空行：若与某参考行一致则占用该行，防止后面的空行向上错位匹配
            nl = norm_line(line)
            if not nl:
                continue
            order = list(range(last, len(rlines))) + list(range(0, last))
            for i in order:
                if not used[i] and norm_line(rlines[i]) == nl:
                    used[i] = True
                    last = i
                    break
            continue
        order = list(range(last, len(rlines))) + list(range(0, last))
        found = None
        for i in order:
            if used[i]:
                continue
            got = try_match(line, rlines[i])
            if got is not None:
                found = (i, got)
                break
        if not found:
            # 手工答案表（按行内容子串匹配）
            hit = False
            for qid_keys in MANUAL_ANSWERS.values():
                for sub, mans in qid_keys:
                    if sub in line:
                        blanks.extend(mans)
                        hit = True
                        break
                if hit:
                    break
            if hit:
                continue
            blanks.extend([None] * n)
            unresolved += n
        else:
            i, got = found
            used[i] = True
            last = i
            blanks.extend(got)
    return blanks, unresolved

# ---------------- 题目信息 ----------------
def extract_task(qid):
    try:
        with open(os.path.join(FILES_DIR, qid + '.html'), encoding='utf-8', errors='replace') as fh:
            qhtml = fh.read()
        m = re.search(r'<h3>工作任务</h3>(.*?)<h3', qhtml, re.S)
        if not m:
            return ''
        seg = re.sub(r'</p>', '\n', m.group(1))
        text = re.sub(r'<[^>]+>', '', seg)
        return htmllib.unescape(text).strip()
    except OSError:
        return ''

def get_ref_code(qid):
    code = engine.extract_code(engine.docx_text(
        os.path.join(ANS_DIR, '人工智能训练师三级操作题答案%s.docx' % qid)))
    if not code:
        return None
    for old, new in engine.PATCHES.get(qid, []):
        code = code.replace(old, new)
    return code

def load_manifest():
    try:
        with open(engine.MANIFEST, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}

# ---------------- 数据构建 ----------------
def build_data(selftest=False):
    manifest = load_manifest()
    questions = []
    stats = []
    for qid in engine.QIDS:
        ref_code = get_ref_code(qid)
        ans_path = os.path.join(ANS_DIR, '人工智能训练师三级操作题答案%s.docx' % qid)
        title, _ = engine.parse_question_html(qid)
        task = extract_task(qid)
        rubric, total = engine.extract_rubric(engine.docx_tables(ans_path)) if os.path.exists(ans_path) else ([], None)
        q = {'id': qid, 'title': title, 'task': task,
             'full': total if total else (sum(s for _, s, _ in rubric) if rubric else 0)}
        if ref_code:
            tpl = resolve_template(qid)
            blanks, unres = align_blanks(tpl, ref_code)
            # 把模板代码转成带占位符的展示文本
            idx = [0]
            def renum(m):
                v = '\u3010%d\u3011' % idx[0]
                idx[0] += 1
                return v
            display = BLANK_RE.sub(renum, tpl)
            ref_out = (manifest.get(qid) or {}).get('stdout', '')
            q.update({'type': 'code', 'tpl': display, 'answers': blanks,
                      'refCode': ref_code, 'refOut': ref_out.strip()[:2500]})
            stats.append((qid, len(blanks), unres))
        else:
            text = engine.docx_text(ans_path) if os.path.exists(ans_path) else ''
            q.update({'type': 'text',
                      'refText': engine.extract_ref_text(text)[:6000],
                      'keywords': engine.extract_keywords(engine.extract_ref_text(text))})
        questions.append(q)
    if selftest:
        for qid, n, unres in stats:
            print('%s: 空=%d 未对齐=%d' % (qid, n, unres))
    return questions

# ---------------- 页面模板 ----------------
PAGE = None  # 见下方 PAGE_HTML

def b64u(s):
    return base64.b64encode(s.encode('utf-8')).decode('ascii')

def generate():
    questions = build_data()
    # 答案与参考内容编码，避免翻源码直接看到
    for q in questions:
        if q['type'] == 'code':
            q['answers'] = [b64u(a) if a else None for a in q['answers']]
            q['refCode'] = b64u(q['refCode'])
            q['refOut'] = b64u(q['refOut'])
        else:
            q['refText'] = b64u(q['refText'])
    payload = json.dumps(questions, ensure_ascii=False)
    payload = payload.replace('</', '<\\/')
    html = PAGE.replace('__DATA__', payload)
    html = html.replace('__TIME__', datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))
    with open(OUT_PATH, 'w', encoding='utf-8') as fh:
        fh.write(html)
    print('答题页面已生成：%s' % OUT_PATH)
    print('共 %d 题（编程 %d / 写作 %d）' % (
        len(questions),
        sum(1 for q in questions if q['type'] == 'code'),
        sum(1 for q in questions if q['type'] == 'text')))

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>人工智能训练师三级 · 在线答题</title>
<style>
body{font-family:'Microsoft YaHei',Arial,sans-serif;margin:0;background:#f0f2f5;color:#2c3e50;}
.header{background:linear-gradient(135deg,#2A2F45,#3D405B);color:#E6AF2E;padding:22px 40px;}
.header h1{margin:0;font-size:24px;}
.header .sub{color:#cfd3e4;font-size:13px;margin-top:6px;}
.wrap{max-width:1100px;margin:20px auto;padding:0 18px;}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:16px;}
.card{flex:1;min-width:150px;background:#fff;border-radius:10px;padding:14px 20px;box-shadow:0 2px 6px rgba(0,0,0,.08);}
.card .num{font-size:26px;font-weight:700;}
.card .lbl{font-size:12.5px;color:#7f8c8d;margin-top:3px;}
.filters{margin-bottom:14px;display:flex;gap:8px;flex-wrap:wrap;}
.fbtn{padding:7px 18px;border-radius:18px;border:1px solid #d5dbdb;background:#fff;cursor:pointer;font-size:13.5px;}
.fbtn.on{background:#2A2F45;color:#E6AF2E;border-color:#2A2F45;}
details{border:1px solid #e0e4e8;border-radius:8px;margin:10px 0;background:#fff;}
details summary{cursor:pointer;padding:12px 18px;font-size:15px;font-weight:600;background:#fafbfc;border-radius:8px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
details .inner{padding:6px 20px 18px;}
.badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;color:#fff;}
.b-code{background:#2980b9;} .b-text{background:#8e44ad;}
.b-done{background:#27ae60;} .b-part{background:#e67e22;} .b-na{background:#95a5a6;}
.task{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:10px 14px;font-size:13.5px;max-height:220px;overflow:auto;white-space:pre-wrap;margin:8px 0;}
pre.code{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:12px;overflow:auto;font-size:13.5px;white-space:pre-wrap;font-family:Consolas,'Courier New',monospace;line-height:2.0;margin:8px 0;}
input.blank{font-family:inherit;font-size:13.5px;border:none;border-bottom:2px dashed #2980b9;background:#fffbe6;padding:1px 4px;outline:none;min-width:60px;}
input.blank.ok{border-bottom:2px solid #27ae60;background:#eafaf1;}
input.blank.bad{border-bottom:2px solid #c0392b;background:#fdedec;}
.mark{font-weight:700;margin-left:3px;}
.mark.ok{color:#27ae60;} .mark.bad{color:#c0392b;} .mark.na{color:#95a5a6;}
.btnrow{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0;}
button.act{padding:8px 20px;border-radius:6px;border:none;cursor:pointer;font-size:14px;background:#2A2F45;color:#E6AF2E;}
button.act.ghost{background:#fff;color:#2A2F45;border:1px solid #bdc3c7;}
button.act:hover{opacity:.88;}
.result{padding:10px 14px;border-radius:6px;font-size:14px;margin:8px 0;display:none;}
.r-good{background:#eafaf1;border:1px solid #a9dfbf;color:#1e8449;}
.r-mid{background:#fef5e7;border:1px solid #fad7a0;color:#af601a;}
.r-bad{background:#fdedec;border:1px solid #f5b7b1;color:#943126;}
.anslist{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:10px 14px;font-size:13.5px;margin:8px 0;display:none;}
.anslist b{font-family:Consolas,monospace;}
textarea.ta{width:100%;min-height:160px;border:1px solid #d5dbdb;border-radius:6px;padding:10px;font-size:14px;font-family:inherit;box-sizing:border-box;}
.chip{display:inline-block;margin:2px;padding:3px 10px;border-radius:12px;font-size:12.5px;}
.chip-hit{background:#d5f5e3;color:#1e8449;} .chip-miss{background:#eaecee;color:#7b7d7d;}
.refpre{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:12px;overflow:auto;font-size:12.5px;white-space:pre-wrap;max-height:400px;margin:8px 0;display:none;}
.note{font-size:12.5px;color:#909497;}
.wc{font-size:12.5px;color:#7f8c8d;text-align:right;margin-top:4px;}
h4{margin:14px 0 6px;}
</style>
</head>
<body>
<div class="header"><h1>人工智能训练师三级 · 在线答题练习</h1>
<div class="sub">生成时间：__TIME__ &nbsp;|&nbsp; 编程题直接填空判分（无需 Jupyter） · 写作题对照参考答案</div></div>
<div class="wrap">
<div class="cards">
  <div class="card"><div class="num" id="stAtt">0</div><div class="lbl">已开始作答</div></div>
  <div class="card"><div class="num" id="stJud">0 / <span id="stCodeN">0</span></div><div class="lbl">编程题已判分</div></div>
  <div class="card"><div class="num" id="stAcc">-</div><div class="lbl">填空正确率</div></div>
  <div class="card"><div class="num" id="stEst">0</div><div class="lbl">编程题估算总分</div></div>
</div>
<div class="filters" id="filters"></div>
<div id="qlist"></div>
<p class="note">说明：填空按参考答案比对，可能存在等价写法被判错（如 data.shape[0] 与 len(data)），以参考答案为准；
答案已做编码处理，点击"显示答案"才会展示。作答内容自动保存在本浏览器中。</p>
</div>
<script>
const QS = __DATA__;
function d64(s){return decodeURIComponent(escape(atob(s)));}
function norm(s){
  return (s||'').replace(/[\\s\\u3000]+/g,'')
    .replace(/['"\\u2018\\u2019\\u201c\\u201d]/g,'\\u0001')
    .replace(/\\uff08/g,'(').replace(/\\uff09/g,')');
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function store(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}
function load(k,d){try{var v=localStorage.getItem(k);return v?JSON.parse(v):d;}catch(e){return d;}}
const judged = {};

function makeQ(q){
  const d = document.createElement('details');
  const s = document.createElement('summary');
  const isCode = q.type==='code';
  s.innerHTML = '<span>'+esc(q.id)+'</span><span style="font-weight:400;color:#566573">'+esc(q.title)+'</span>'
    +'<span class="badge '+(isCode?'b-code':'b-text')+'">'+(isCode?'编程题':'写作题')+'</span>'
    +'<span class="badge b-na" id="bdg_'+q.id.replace(/\\./g,'_')+'">未判分</span>';
  d.appendChild(s);
  const inner = document.createElement('div'); inner.className='inner';
  if(q.task) inner.innerHTML += '<h4>工作任务</h4><div class="task">'+esc(q.task)+'</div>';
  if(isCode){
    inner.appendChild(buildCode(q));
  } else {
    inner.appendChild(buildText(q));
  }
  d.appendChild(inner);
  return d;
}

function buildCode(q){
  const wrap = document.createElement('div');
  const pre = document.createElement('pre'); pre.className='code';
  const ans = q.answers;
  let ai = 0;
  const html = esc(q.tpl).replace(/\\u3010(\\d+)\\u3011/g, function(m,n){
    const i = parseInt(n,10);
    return '<input class="blank" id="in_'+q.id+'_'+i+'" data-q="'+q.id+'" data-i="'+i+'" autocomplete="off"><span class="mark" id="mk_'+q.id+'_'+i+'"></span>';
  });
  pre.innerHTML = html;
  wrap.appendChild(pre);
  const row = document.createElement('div'); row.className='btnrow';
  row.innerHTML = '<button class="act" onclick="judge(\\''+q.id+'\\')">判 分</button>'
    +'<button class="act ghost" onclick="showAns(\\''+q.id+'\\')">显示答案</button>'
    +'<button class="act ghost" onclick="toggleRef(\\''+q.id+'\\')">参考输出</button>'
    +'<button class="act ghost" onclick="resetQ(\\''+q.id+'\\')">重置本题</button>';
  wrap.appendChild(row);
  const res = document.createElement('div'); res.className='result'; res.id='res_'+q.id;
  wrap.appendChild(res);
  const al = document.createElement('div'); al.className='anslist'; al.id='al_'+q.id;
  wrap.appendChild(al);
  const rp = document.createElement('pre'); rp.className='refpre'; rp.id='ro_'+q.id;
  wrap.appendChild(rp);
  // 恢复已保存输入（元素尚未插入文档，用 pre 内部查询）
  const saved = load('pg_'+q.id, null);
  const inputs = pre.querySelectorAll('input.blank');
  inputs.forEach(function(el){
    const i = parseInt(el.getAttribute('data-i'), 10);
    if(saved && saved[i]) el.value = saved[i];
    el.addEventListener('input', function(){ saveInputs(q); autoSize(el); updateStats(); });
    autoSize(el);
  });
  return wrap;
}

function buildText(q){
  const wrap = document.createElement('div');
  const ta = document.createElement('textarea'); ta.className='ta'; ta.id='ta_'+q.id;
  ta.placeholder = '在此输入你的答案（自动保存）…';
  ta.value = load('pgt_'+q.id, '') || '';
  ta.addEventListener('input', function(){
    store('pgt_'+q.id, ta.value);
    document.getElementById('wc_'+q.id).textContent = '已写 '+ta.value.length+' 字';
    updateStats();
  });
  wrap.appendChild(ta);
  const wc = document.createElement('div'); wc.className='wc'; wc.id='wc_'+q.id;
  wc.textContent = '已写 '+ta.value.length+' 字';
  wrap.appendChild(wc);
  const row = document.createElement('div'); row.className='btnrow';
  row.innerHTML = '<button class="act ghost" onclick="kwCheck(\\''+q.id+'\\')">关键词对照</button>'
    +'<button class="act ghost" onclick="toggleRefText(\\''+q.id+'\\')">显示参考答案</button>'
    +'<button class="act ghost" onclick="resetQ(\\''+q.id+'\\')">清空</button>';
  wrap.appendChild(row);
  const kw = document.createElement('div'); kw.id='kw_'+q.id;
  wrap.appendChild(kw);
  const rp = document.createElement('pre'); rp.className='refpre'; rp.id='rt_'+q.id;
  wrap.appendChild(rp);
  return wrap;
}

function autoSize(el){
  el.style.width = Math.max(60, (el.value.length+3)*9) + 'px';
}
function saveInputs(q){
  const arr = q.answers.map((a,i)=>{
    const el = document.getElementById('in_'+q.id+'_'+i);
    return el ? el.value : '';
  });
  store('pg_'+q.id, arr);
}
function findQ(id){return QS.find(x=>x.id===id);}

function judge(id){
  const q = findQ(id);
  let ok=0, tot=0, na=0;
  q.answers.forEach((a,i)=>{
    const el = document.getElementById('in_'+id+'_'+i);
    const mk = document.getElementById('mk_'+id+'_'+i);
    if(!a){ mk.textContent='○'; mk.className='mark na'; mk.title='此空无标准答案，不计入'; el.className='blank'; na++; return; }
    tot++;
    const good = norm(el.value) === norm(d64(a)) && el.value.trim()!=='';
    el.className = 'blank ' + (good?'ok':'bad');
    mk.textContent = good?'✓':'✗';
    mk.className = 'mark ' + (good?'ok':'bad');
    if(good) ok++;
  });
  const res = document.getElementById('res_'+id);
  const rate = tot? ok/tot : 0;
  const full = q.full || tot;
  const est = Math.round(rate*full*10)/10;
  judged[id] = {ok:ok, tot:tot, est:est, full:full};
  let cls = rate>=0.8?'r-good':(rate>=0.5?'r-mid':'r-bad');
  res.style.display='block'; res.className='result '+cls;
  res.innerHTML = '填对 <b>'+ok+'</b> / '+tot+' 空'+(na?'（另有 '+na+' 空无标准答案，未计入）':'')
    +(full?'，估算得分 <b>'+est+'</b> / '+full+' 分':'')
    +(rate<1?'。<span class="note">可能存在等价写法，可点"显示答案"核对</span>':' 🎉');
  const bdg = document.getElementById('bdg_'+id.replace(/\\./g,'_'));
  bdg.textContent = ok+'/'+tot;
  bdg.className = 'badge '+(rate>=0.8?'b-done':(rate>0?'b-part':'b-na'));
  updateStats();
}
function showAns(id){
  const q = findQ(id);
  const al = document.getElementById('al_'+id);
  if(al.style.display==='block'){al.style.display='none';return;}
  al.style.display='block';
  al.innerHTML = '<b>参考答案：</b><br>' + q.answers.map((a,i)=>
    '空'+(i+1)+': <b>'+(a?esc(d64(a)):'<i>无标准答案（请参考下方代码）</i>')+'</b>').join('<br>');
  if(q.refCode) al.innerHTML += '<br><br><details><summary style="cursor:pointer;font-size:13.5px">展开完整参考代码</summary><pre class="refpre" style="display:block">'+esc(d64(q.refCode))+'</pre></details>';
}
function toggleRef(id){
  const el = document.getElementById('ro_'+id);
  if(el.style.display==='block'){el.style.display='none';return;}
  const q = findQ(id);
  el.innerHTML = '<b>参考代码运行输出（对答案系统实跑结果）：</b><br>'+esc(d64(q.refOut)||'（无）');
  el.style.display='block';
}
function toggleRefText(id){
  const el = document.getElementById('rt_'+id);
  if(el.style.display==='block'){el.style.display='none';return;}
  const q = findQ(id);
  el.innerHTML = '<b>参考答案（做完再看）：</b><br>'+esc(d64(q.refText)||'（无）');
  el.style.display='block';
}
function kwCheck(id){
  const q = findQ(id);
  const el = document.getElementById('kw_'+id);
  const ta = document.getElementById('ta_'+id);
  const txt = ta.value;
  const hits = q.keywords.filter(w=>txt.includes(w));
  const miss = q.keywords.filter(w=>!txt.includes(w));
  el.innerHTML = '<h4>关键词覆盖：'+hits.length+' / '+q.keywords.length+'</h4>'
    + hits.map(w=>'<span class="chip chip-hit">'+esc(w)+'</span>').join('')
    + miss.map(w=>'<span class="chip chip-miss">'+esc(w)+'</span>').join('')
    + '<p class="note">绿色=已覆盖，灰色=未覆盖。写作题请对照参考答案自评。</p>';
}
function resetQ(id){
  const q = findQ(id);
  if(q.type==='code'){
    q.answers.forEach((a,i)=>{
      const el = document.getElementById('in_'+id+'_'+i);
      el.value=''; el.className='blank'; autoSize(el);
      document.getElementById('mk_'+id+'_'+i).textContent='';
    });
    store('pg_'+id, null);
    document.getElementById('res_'+id).style.display='none';
  } else {
    document.getElementById('ta_'+id).value='';
    store('pgt_'+id,'');
    document.getElementById('wc_'+id).textContent='已写 0 字';
  }
  const bdg = document.getElementById('bdg_'+id.replace(/\\./g,'_'));
  bdg.textContent='未判分'; bdg.className='badge b-na';
  delete judged[id];
  updateStats();
}
function updateStats(){
  let att=0, jud=0, ok=0, tot=0, est=0, codeN=0;
  QS.forEach(q=>{
    if(q.type==='code'){
      codeN++;
      const saved = load('pg_'+q.id, null);
      const has = saved && saved.some(v=>v && v.trim());
      if(has) att++;
      if(judged[q.id]){ jud++; ok+=judged[q.id].ok; tot+=judged[q.id].tot; est+=judged[q.id].est; }
    } else {
      const t = load('pgt_'+q.id,'');
      if(t && t.trim()) att++;
    }
  });
  document.getElementById('stAtt').textContent = att;
  document.getElementById('stJud').textContent = jud;
  document.getElementById('stCodeN').textContent = codeN;
  document.getElementById('stAcc').textContent = tot? Math.round(ok/tot*100)+'%':'-';
  document.getElementById('stEst').textContent = Math.round(est*10)/10;
}

// 章节筛选
const CH = ['全部','第1章','第2章','第3章','第4章'];
let curCh = '全部';
function renderFilter(){
  const f = document.getElementById('filters');
  f.innerHTML='';
  CH.forEach(c=>{
    const b = document.createElement('button');
    b.className = 'fbtn'+(c===curCh?' on':'');
    b.textContent = c;
    b.onclick = ()=>{ curCh=c; renderList(); renderFilter(); };
    f.appendChild(b);
  });
}
function renderList(){
  const list = document.getElementById('qlist');
  list.innerHTML='';
  QS.forEach(q=>{
    if(curCh!=='全部' && !q.id.startsWith(curCh.replace('第','').replace('章','')+'.')) return;
    list.appendChild(makeQ(q));
  });
}
renderFilter(); renderList(); updateStats();
</script>
</body>
</html>"""

if __name__ == '__main__':
    if '--selftest' in sys.argv:
        build_data(selftest=True)
    else:
        generate()
