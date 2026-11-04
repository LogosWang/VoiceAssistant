"""What an AI can't read as it is gets a version it can, next to the note naming the attachment.

Every client writes attachments the same way, [附件文件：path], so this works for all of them. It uses what
any Mac has (sips, textutil) and the helper built with the app; on Windows, Python packages and FFmpeg. Results stay in ~/.carnot/converted, which
bots may not upload, for 30 days. A file that can't be converted never stops the turn: the note says so.
"""
import contextlib
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

import compat
import shared

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(shared.HOME, "converted")
FRAMES_HELPER = os.path.join(HERE, "videoframes", "videoframes")
MARK = re.compile(r"\[附件(?:文件|图片)：([^\]\n]+)\]")
KEEP_DAYS = 30
FRAMES = 8
MAX_TEXT = 200 * 1024 * 1024
MAX_PDF_PAGES = 1000
MAX_CSV_ARRAYS = 40
MAX_CSV_CELLS = 5_000_000
VERSION = "1"            # new converters: old results don't count

IMAGES = {".heic", ".heif", ".avif", ".tif", ".tiff", ".bmp"}
DOCUMENTS = {".doc", ".docx", ".rtf", ".odt"}
VIDEOS = {".mp4", ".mov", ".m4v", ".3gp"}
SHEETS = {".xlsx", ".xlsm", ".xls"}
TEXTS = {".csv", ".tsv", ".txt", ".m"}  # readable, unless a Windows editor wrote them as GBK or UTF-16


def _run(*cmd):
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).decode("utf-8", "replace").strip()[-200:] or f"exit {r.returncode}")
    return r.stdout


def _stem(path):
    return re.sub(r"[^\w.-]+", "_", os.path.splitext(os.path.basename(path))[0]) or "file"


def image(path, out):
    target = os.path.join(out, _stem(path) + ".jpg")
    if compat.IS_WIN:
        from PIL import Image, ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()
        with Image.open(path) as source:
            picture = ImageOps.exif_transpose(source).convert("RGB")
        picture.thumbnail((2400, 2400))
        picture.save(target, "JPEG", quality=90)
        return f"这种图片格式不能直接读，请读转换后的 JPEG：{target}"
    _run("/usr/bin/sips", "-s", "format", "jpeg", "-Z", "2400", path, "--out", target)
    return f"这种图片格式不能直接读，请读转换后的 JPEG：{target}"


def document(path, out):
    target = os.path.join(out, _stem(path) + ".txt")
    if compat.IS_WIN:
        _document_text(path, out, target)
    else:
        _run("/usr/bin/textutil", "-convert", "txt", "-output", target, path)
    return f"请读提取出的文字：{target}（图片和版式不在其中）"


def _document_text(path, out, target):
    """Windows has no textutil. Old .doc files need a program that reads them, if there is one."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        import docx
        book = docx.Document(path)
        parts = [p.text for p in book.paragraphs]
        for table in book.tables:
            parts += ["\t".join(cell.text for cell in row.cells) for row in table.rows]
        content = "\n".join(parts)
    elif ext == ".rtf":
        from striprtf.striprtf import rtf_to_text
        with open(path, encoding="utf-8", errors="replace") as f:
            content = rtf_to_text(f.read(), errors="ignore")
    elif ext == ".odt":
        import xml.etree.ElementTree as ET
        import zipfile
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("content.xml"))
        text_ns = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
        content = "\n".join("".join(p.itertext()) for p in root.iter() if p.tag in (text_ns + "p", text_ns + "h"))
    else:
        return _old_doc(path, out, target)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)


def _old_doc(path, out, target):
    soffice = shutil.which("soffice") or next(
        (p for p in (os.path.expandvars(r"%ProgramFiles%\LibreOffice\program\soffice.exe"),) if os.path.exists(p)), None)
    if soffice:
        _run(soffice, "--headless", "--convert-to", "txt:Text", "--outdir", out, path)
        made = os.path.join(out, os.path.splitext(os.path.basename(path))[0] + ".txt")
        if made != target:
            os.replace(made, target)
        return
    script = ("$w=New-Object -ComObject Word.Application;$w.Visible=$false;"
              "try{$d=$w.Documents.Open($env:CARNOT_DOC,$false,$true);"
              "[IO.File]::WriteAllText($env:CARNOT_TXT,$d.Content.Text,[Text.Encoding]::UTF8);$d.Close(0)}finally{$w.Quit()}")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=120,
                       env=dict(os.environ, CARNOT_DOC=path, CARNOT_TXT=target), stdin=subprocess.DEVNULL)
    if r.returncode or not os.path.exists(target):
        raise RuntimeError("旧版 .doc 需要这台电脑装有 Word 或 LibreOffice 才能转换")


def _video_ffmpeg(path, out):
    """What the Swift helper reports, from ffprobe and ffmpeg."""
    probe = json.loads(_run(compat.tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path))
    streams = probe.get("streams", [])
    track = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not track:
        raise RuntimeError("no video track")
    duration = float(probe.get("format", {}).get("duration") or track.get("duration") or 0)
    width, height = int(track.get("width") or 0), int(track.get("height") or 0)
    if any(abs(int(d.get("rotation", 0))) == 90 for d in track.get("side_data_list", [])):
        width, height = height, width
    frames = []
    for i in range(FRAMES):
        second = duration * (i + 0.5) / FRAMES
        target = os.path.join(out, f"frame_{i + 1:02d}.jpg")
        fit = "scale='min(1280,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease"
        with contextlib.suppress(RuntimeError):
            _run(compat.tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{second:.3f}", "-i", path, "-frames:v", "1", "-vf", fit, "-q:v", "3", target)
            if os.path.exists(target):
                frames.append({"second": second, "path": target})
    return {"duration": duration, "width": width, "height": height, "codec": track.get("codec_name", ""),
            "audio": any(s.get("codec_type") == "audio" for s in streams), "frames": frames}


def video(path, out):
    if compat.IS_WIN:
        return _video_note(_video_ffmpeg(path, out))
    if not os.path.exists(FRAMES_HELPER):    # a source checkout; the installer ships it built
        _run("swiftc", "-O", "-o", FRAMES_HELPER, os.path.join(HERE, "videoframes", "main.swift"))
    return _video_note(json.loads(_run(FRAMES_HELPER, path, out, str(FRAMES))))


def _video_note(info):
    frames = "；".join(f"{f['second']:.1f} 秒：{f['path']}" for f in info["frames"])
    audio = "有音轨，声音内容没有提取" if info["audio"] else "没有音轨"
    return (f"视频不能直接看。时长 {info['duration']:.1f} 秒，{info['width']}×{info['height']}，编码 {info['codec']}，{audio}。"
            f"请读均匀抽取的 {len(info['frames'])} 帧图片，不要把它们当成完整内容：{frames}")


def _write_rows(target, rows):
    count = 0
    with open(target, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow("" if v is None else v for v in row)
            count += 1
    return count


def sheet(path, out):
    tables = []
    def save(name, rows):
        safe = re.sub(r"[^\w.-]+", "_", str(name)) or "sheet"
        target = os.path.join(out, f"{len(tables) + 1}_{safe}.csv")
        tables.append(f"工作表“{name}”，{_write_rows(target, rows)} 行：{target}")
    if path.lower().endswith(".xls"):
        import xlrd
        book = xlrd.open_workbook(path)
        for s in book.sheets():
            save(s.name, (s.row_values(i) for i in range(s.nrows)))
    else:
        import openpyxl
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for s in book.worksheets:
            save(s.title, s.iter_rows(values_only=True))
        book.close()
    return "表格已导出为 CSV，公式取保存时的计算结果，图表不在其中：" + "；".join(tables)


def pdf(path, out):
    import pypdf
    reader = pypdf.PdfReader(path)
    pages = reader.pages[:MAX_PDF_PAGES]
    parts = [f"--- 第 {i + 1} 页 ---\n{p.extract_text() or ''}" for i, p in enumerate(pages)]
    if sum(len(p) for p in parts) < 20 * len(pages):
        return "这个 PDF 几乎没有文字层，可能是扫描件，请直接读原文件。"
    target = os.path.join(out, _stem(path) + ".txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("\n\n".join(parts))
    more = f"（只提取了前 {MAX_PDF_PAGES} 页，共 {len(reader.pages)} 页）" if len(reader.pages) > MAX_PDF_PAGES else ""
    return f"共 {len(reader.pages)} 页。文字版：{target}{more}；图表和公式要看原文件。"


def text(path, out):
    """Only non-UTF-8 files need anything. None means it is fine as it is."""
    if os.path.getsize(path) > MAX_TEXT:
        return None
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoding = "utf-16"
    else:
        try:
            raw.decode("utf-8")
            return None
        except UnicodeDecodeError:
            encoding = "gb18030"
    try:
        decoded = raw.decode(encoding)
    except UnicodeDecodeError:
        return None
    target = os.path.join(out, os.path.basename(path))
    with open(target, "w", encoding="utf-8") as f:
        f.write(decoded)
    return f"原文件是 {encoding.upper()} 编码，请读转成 UTF-8 的版本：{target}"


class _MatWriter:
    """Writes one line per variable, and a CSV for each numeric array small enough to be useful."""

    def __init__(self, out):
        self.out, self.lines, self.csvs = out, [], 0

    def value(self, name, v, depth=0):
        import numpy as np
        if depth > 8:
            return self.lines.append(f"{name}: （嵌套太深，未展开）")
        if isinstance(v, dict):
            self.lines.append(f"{name}: 结构体，字段 {', '.join(map(str, v))}")
            for k, x in v.items():
                self.value(f"{name}.{k}", x, depth + 1)
        elif isinstance(v, (list, tuple)):
            self.lines.append(f"{name}: 元胞，{len(v)} 项")
            for i, x in enumerate(v[:50]):
                self.value(f"{name}{{{i + 1}}}", x, depth + 1)
        elif isinstance(v, str):
            self.lines.append(f"{name}: 文本 {v[:2000]!r}")
        elif isinstance(v, np.ndarray) and v.dtype == object:
            self.value(name, v.tolist(), depth)
        elif isinstance(v, (np.ndarray, np.generic, int, float, bool, complex)):
            self.array(name, np.asarray(v))
        else:
            self.lines.append(f"{name}: {type(v).__name__} {str(v)[:500]}")

    def array(self, name, a):
        import numpy as np
        line = f"{name}: {a.dtype} 数组，形状 {'×'.join(map(str, a.shape)) or '标量'}"
        if a.dtype.kind in "biuf" and a.size:
            finite = a[np.isfinite(a)] if a.dtype.kind == "f" else a
            if finite.size:
                line += f"，最小 {finite.min():g}，最大 {finite.max():g}，平均 {finite.mean():g}"
            if finite.size != a.size:
                line += f"，{a.size - finite.size} 个非有限值"
        if a.size <= 24:
            line += f"，值 {a.tolist()}"
        elif a.ndim <= 2 and a.size <= MAX_CSV_CELLS and self.csvs < MAX_CSV_ARRAYS and a.dtype.kind in "biufc":
            self.csvs += 1
            target = os.path.join(self.out, re.sub(r"[^\w.-]+", "_", name) + ".csv")
            np.savetxt(target, a if a.ndim == 2 else a.reshape(-1, 1), delimiter=",", fmt="%.10g")
            line += f"，数据：{target}"
        self.lines.append(line)


def _hdf5(item, h5py, np, file):
    """A MATLAB 7.3 value. HDF5 stores arrays with the axes reversed; characters are UTF-16 codes."""
    if isinstance(item, h5py.Group):
        return {k: _hdf5(item[k], h5py, np, file) for k in item if k != "#refs#"}
    kind = item.attrs.get("MATLAB_class", b"")
    kind = kind.decode() if isinstance(kind, bytes) else str(kind)
    if item.attrs.get("MATLAB_empty", 0):
        return np.zeros((0,))
    data = item[()]
    if kind == "char":
        return "".join(map(chr, np.asarray(data).flatten(order="F")))
    if data.dtype == h5py.ref_dtype:
        return [_hdf5(file[r], h5py, np, file) for r in np.asarray(data).flatten(order="F")[:50]]
    return np.asarray(data).T


def mat(path, out):
    import h5py
    writer = _MatWriter(out)
    if h5py.is_hdf5(path):          # what -v7.3 saves
        import numpy as np
        version = "MATLAB 7.3"
        with h5py.File(path, "r") as f:
            data = {k: _hdf5(f[k], h5py, np, f) for k in f if k != "#refs#"}
    else:
        from scipy.io import loadmat
        version = "MATLAB 5/7"
        data = loadmat(path, simplify_cells=True)
    variables = {k: v for k, v in data.items() if not k.startswith("__")}
    for k, v in variables.items():
        writer.value(k, v)
    target = os.path.join(out, _stem(path) + "_变量.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("\n".join(writer.lines) + "\n")
    return f"{version} 数据文件，不能直接读。{len(variables)} 个变量的清单和数值范围：{target}"


def _converter(path):
    ext = os.path.splitext(path)[1].lower()
    for kinds, fn in ((IMAGES, image), (DOCUMENTS, document), (VIDEOS, video), (SHEETS, sheet), (TEXTS, text)):
        if ext in kinds:
            return fn
    return {".pdf": pdf, ".mat": mat}.get(ext)


def targets(text_):
    """The attachments in a prompt that may need converting."""
    found = []
    for path in MARK.findall(text_):
        if os.path.isfile(path) and _converter(path) and path not in found:
            found.append(path)
    return found


def _note(path):
    st = os.stat(path)
    key = hashlib.sha256(f"{VERSION}|{os.path.realpath(path)}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:20]
    out = os.path.join(OUT, key)
    saved = os.path.join(out, "note.json")
    with contextlib.suppress(OSError, ValueError):
        with open(saved, encoding="utf-8") as f:
            return json.load(f)["note"]
    os.makedirs(out, mode=0o700, exist_ok=True)
    note = _converter(path)(path, out)
    with open(saved, "w", encoding="utf-8") as f:
        json.dump({"note": note}, f, ensure_ascii=False)
    return note


def _tidy():
    cutoff = time.time() - KEEP_DAYS * 86400
    with contextlib.suppress(OSError):
        for name in os.listdir(OUT):
            item = os.path.join(OUT, name)
            if os.path.getmtime(item) < cutoff:
                shutil.rmtree(item, ignore_errors=True)


def expand(text_):
    """Returns the prompt with a note after each attachment that needs one, the folder to allow, and a summary."""
    notes = {}
    for path in targets(text_):
        try:
            note = _note(path)
        except Exception as e:      # whatever it is, the turn goes on and the AI hears about it
            note = f"这个文件没能转换（{type(e).__name__}: {str(e)[:160]}），内容可能读不了，如实告诉用户。"
        if note:
            notes[path] = note
    if not notes:
        return text_, [], ""
    _tidy()
    expanded = MARK.sub(lambda m: f"{m.group(0)}（{notes[m.group(1)]}）" if m.group(1) in notes else m.group(0), text_)
    summary = "\n".join(f"{os.path.basename(p)}：{n}" for p, n in notes.items())
    return expanded, [OUT], summary
