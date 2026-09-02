#!/usr/bin/env python3
"""
章PDF + ページ別台本テキスト から、AIナレーション音声+字幕焼き込みのmp4を全自動生成する試作スクリプト。
外部有料APIは使わない（edge-tts=無料/透かしなし、ffmpeg、pdftoppmまたはPyMuPDF）。

使い方:
  python3 make_video.py --pdf <章PDF> --script <台本txt> --out out/ [--voice ja-JP-NanamiNeural] [--rate +0%]

台本フォーマット:
  「【ページN】」という行で区切られたブロック。ブロック内の以降の行（次の【ページ】行まで）が
  そのページのナレーション本文（複数行あればスペースなしで連結）。
"""
import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
YOMI_DICT_PATH = SCRIPT_DIR / "yomi_dict.json"

TARGET_W, TARGET_H = 1920, 1080

PAGE_HEADER_RE = re.compile(r"^【ページ(\d+)】\s*$")


def log(msg: str) -> None:
    print(f"[make_video] {msg}", flush=True)


def load_yomi_dict() -> dict:
    if YOMI_DICT_PATH.exists():
        return json.loads(YOMI_DICT_PATH.read_text(encoding="utf-8"))
    return {}


_FULLWIDTH_DIGIT_TRANS = str.maketrans({chr(0xFF10 + i): str(i) for i in range(10)})


def normalize_fullwidth_digits(text: str) -> str:
    """全角数字(０-９)を半角に正規化する。STEP０１のような表記を正規表現で拾うため先に統一する。"""
    return text.translate(_FULLWIDTH_DIGIT_TRANS)


def build_tts_text(text: str, yomi_dict: dict) -> str:
    """TTSに渡す読み上げ専用の文字列を作る。字幕(SRT)には使わず、ここで変換した結果は
    音声合成の入力にのみ使う（元の台本表記はpages側にそのまま残す）。
    処理順: 全角数字→半角 → yomi_dict.jsonの`_regex`(STEP0N等の正規表現ルール) → 略語などの単純辞書置換。"""
    text = normalize_fullwidth_digits(text)
    for pattern, repl in yomi_dict.get("_regex", []):
        text = re.sub(pattern, repl, text)
    for key, val in yomi_dict.items():
        if key.startswith("_"):
            continue  # 制御キー（_regex, _line_remove_if_containsなど）は置換対象外
        # 意図的簡略化: 英数字のみの語は単語境界(前後が英数字でない位置)を付けて誤爆を防ぐが、
        # 記号のみの語(％、＆、No.等)はそのまま部分一致で置換する簡易実装。
        # 「vs」「X」のような1〜2文字の一般的すぎる語はyomi_dict.jsonに入れない運用とし、
        # 本格的な形態素解析ベースの誤爆防止はここでは行わない。
        if re.fullmatch(r"[A-Za-z0-9.]+", key):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(key) + r"(?![A-Za-z0-9])"
            text = re.sub(pattern, val, text)
        else:
            text = text.replace(key, val)
    text = re.sub(r"、{2,}", "、", text)  # 記号置換で読点が連続したときの圧縮
    return text


def get_line_remove_keywords(yomi_dict: dict) -> list[str]:
    """yomi_dict.jsonの `_line_remove_if_contains` に列挙された語を含む行は丸ごと除去する。
    CONFIDENTIAL等の透かし語対策。単純置換では文が不自然になるため行単位で除去する。"""
    return yomi_dict.get("_line_remove_if_contains", [])


def _line_should_be_removed(line: str, remove_keywords: list[str]) -> bool:
    upper = line.upper()
    return any(kw.upper() in upper for kw in remove_keywords)


def parse_script(script_path: Path, yomi_dict: dict) -> dict[int, str]:
    """台本テキストを {ページ番号: ナレーション本文} に分解する。"""
    pages: dict[int, str] = {}
    current_page = None
    buf: list[str] = []
    remove_keywords = get_line_remove_keywords(yomi_dict)

    def flush():
        if current_page is not None:
            text = "".join(buf).strip()
            pages[current_page] = text  # 元の台本表記のまま保持(TTS変換はbuild_tts_textで別途行う)

    for raw_line in script_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        m = PAGE_HEADER_RE.match(line)
        if m:
            flush()
            current_page = int(m.group(1))
            buf = []
            continue
        if line.startswith("※"):
            continue  # 注釈行は読み上げから除外
        if _line_should_be_removed(line, remove_keywords):
            continue  # CONFIDENTIAL等の透かし語を含む行は除去
        if current_page is not None and line:
            buf.append(line)
    flush()
    return pages


def extract_pdf_page_text(pdf_path: Path, page_no: int, yomi_dict: dict) -> str:
    """台本にページブロックが無い場合のフォールバック: PDF本文をそのページ分だけ抜き出す。"""
    remove_keywords = get_line_remove_keywords(yomi_dict)
    if shutil.which("pdftotext"):
        result = subprocess.run(
            ["pdftotext", "-f", str(page_no), "-l", str(page_no), "-layout", str(pdf_path), "-"],
            check=True, capture_output=True, text=True,
        )
        raw_text = result.stdout
    else:
        import fitz  # type: ignore

        doc = fitz.open(str(pdf_path))
        raw_text = doc[page_no - 1].get_text()

    lines = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _line_should_be_removed(line, remove_keywords):
            continue
        lines.append(line)
    return "".join(lines)  # 元のPDF本文表記のまま返す(TTS変換はbuild_tts_textで別途行う)


def pdf_to_images(pdf_path: Path, work_dir: Path) -> list[Path]:
    """章PDFの各ページを1920x1080のPNG（白背景に収める）に変換する。"""
    raw_dir = work_dir / "raw_pages"
    raw_dir.mkdir(parents=True, exist_ok=True)

    if shutil.which("pdftoppm"):
        log("pdftoppmでPDF→画像変換")
        prefix = str(raw_dir / "page")
        subprocess.run(
            ["pdftoppm", "-r", "200", "-png", str(pdf_path), prefix],
            check=True,
        )
        raw_images = sorted(raw_dir.glob("page-*.png"))
    else:
        log("pdftoppmが無いためPyMuPDF(fitz)でPDF→画像変換")
        import fitz  # type: ignore

        doc = fitz.open(str(pdf_path))
        raw_images = []
        zoom = 200 / 72
        mat = fitz.Matrix(zoom, zoom)
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=mat)
            out_path = raw_dir / f"page-{i}.png"
            pix.save(str(out_path))
            raw_images.append(out_path)

    if not raw_images:
        raise RuntimeError("PDFから画像を生成できませんでした")

    # 1920x1080に収める（余白は白）。ffmpegのpad+scaleで統一。
    fitted_dir = work_dir / "pages"
    fitted_dir.mkdir(parents=True, exist_ok=True)
    fitted_images = []
    for idx, img in enumerate(raw_images, start=1):
        out_path = fitted_dir / f"page_{idx:03d}.png"
        vf = (
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
            f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:white"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(img), "-vf", vf, str(out_path)],
            check=True,
        )
        fitted_images.append(out_path)
    return fitted_images


def srt_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms_total = int(round(seconds * 1000))
    hh, rem = divmod(ms_total, 3600_000)
    mm, rem = divmod(rem, 60_000)
    ss, ms = divmod(rem, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


async def synth_page_audio(text: str, voice: str, rate: str, mp3_path: Path):
    """edge-ttsで音声合成し、WordBoundaryのタイミング情報も返す。"""
    import edge_tts

    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    word_boundaries = []
    with open(mp3_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                word_boundaries.append(chunk)
    return word_boundaries


def split_sentences(text: str) -> list[str]:
    """句点「。」で文を分割する（末尾の空文字は除去）。"""
    parts = [p for p in re.split(r"(?<=。)", text) if p.strip()]
    return parts if parts else [text]


def build_srt_for_page(text: str, word_boundaries: list, duration_sec: float) -> list[tuple[float, float, str]]:
    """
    ページ内の文ごとの (開始秒, 終了秒, 文字列) リストを作る。
    edge-ttsのWordBoundary（10^-7秒単位のoffset/duration）が取れればそれを使って厳密に、
    取れなければページ全体の長さを文字数比例配分する。
    """
    sentences = split_sentences(text)
    n = len(sentences)

    if word_boundaries:
        # 各単語の開始位置(offset, 100ns単位)を秒に変換
        units = [
            (wb["offset"] / 1e7, (wb["offset"] + wb["duration"]) / 1e7, wb["text"])
            for wb in word_boundaries
        ]
        # 単語を文字位置の累積でソートし、各文の範囲に含まれる単語のstart/endから文の時間域を推定
        # text中の各単語のtext_offsetを使って文境界にマッピングする
        cursor = 0
        sentence_ranges = []
        for s in sentences:
            start_idx = text.find(s, cursor)
            if start_idx == -1:
                start_idx = cursor
            end_idx = start_idx + len(s)
            sentence_ranges.append((start_idx, end_idx))
            cursor = end_idx

        # 単語ごとの文字オフセットが無い版のedge-ttsもあるため、
        # 単語出現順で均等按分するフォールバック方式を採用（word数を文字数比で文に割当）
        total_chars = sum(len(s) for s in sentences) or 1
        word_idx = 0
        results = []
        for s in sentences:
            share = max(1, round(len(units) * (len(s) / total_chars)))
            chunk = units[word_idx: word_idx + share]
            word_idx += share
            if chunk:
                start = chunk[0][0]
                end = chunk[-1][1]
            else:
                start = end = None
            results.append((start, end, s))
        # 端の単語余りを最後の文にまとめる
        if word_idx < len(units) and results:
            leftover = units[word_idx:]
            last_start, last_end, last_text = results[-1]
            new_end = leftover[-1][1]
            results[-1] = (last_start if last_start is not None else leftover[0][0], new_end, last_text)

        # Noneの穴埋め（保険）: 文字数比例配分にフォールバック
        if any(r[0] is None for r in results):
            return _proportional_ranges(sentences, duration_sec)

        # 開始・終了の整合性を保証(単調増加)
        fixed = []
        prev_end = 0.0
        for start, end, s in results:
            start = max(start, prev_end)
            end = max(end, start + 0.3)
            fixed.append((start, end, s))
            prev_end = end
        # 最後の文の終了をページ全体の長さに合わせる
        if fixed:
            last_start, _, last_text = fixed[-1]
            fixed[-1] = (last_start, duration_sec, last_text)
        return fixed

    return _proportional_ranges(sentences, duration_sec)


def _proportional_ranges(sentences: list[str], duration_sec: float) -> list[tuple[float, float, str]]:
    total_chars = sum(len(s) for s in sentences) or 1
    ranges = []
    cursor = 0.0
    for i, s in enumerate(sentences):
        share = duration_sec * (len(s) / total_chars)
        start = cursor
        end = cursor + share if i < len(sentences) - 1 else duration_sec
        ranges.append((start, end, s))
        cursor = end
    return ranges


SUBTITLE_MAX_LINE_CHARS = 22  # 1行の上限（全角換算）
SUBTITLE_MAX_TOTAL_CHARS = SUBTITLE_MAX_LINE_CHARS * 2  # 2行に収まる上限


def split_long_sentence_for_subtitle(sentence: str, max_total: int = SUBTITLE_MAX_TOTAL_CHARS) -> list[str]:
    """句点で分割済みの1文が2行(44文字)を超える場合、読点「、」でも分割して字幕単位を短くする。
    音声ファイル自体は1ページ1本のまま変えず、字幕の表示タイミングだけ細かくするために使う。"""
    if len(sentence) <= max_total:
        return [sentence]
    parts = [p for p in re.split(r"(?<=、)", sentence) if p]
    if not parts:
        parts = [sentence]
    chunks = []
    cur = ""
    for p in parts:
        # 読点で割っても一片がmax_totalを超える場合(英語混じり・読点が無い長文など)は
        # 固定長で強制的に区切る。これが無いと2行の上限を超えたまま画面外にはみ出す。
        while len(p) > max_total:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:max_total])
            p = p[max_total:]
        if cur and len(cur) + len(p) > max_total:
            chunks.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        chunks.append(cur)
    return chunks if chunks else [sentence]


def wrap_subtitle_text(text: str, max_line: int = SUBTITLE_MAX_LINE_CHARS) -> str:
    """1つの字幕チャンク(最大2*max_line文字)を最大2行に折り返す。句読点の直後を優先して\\Nを入れるが、
    2行目がmax_lineを超えて画面外にはみ出さないよう、区切り位置は必ず [len-max_line, max_line] の範囲に収める。"""
    n = len(text)
    if n <= max_line:
        return text
    lo = max(0, n - max_line)
    hi = min(n, max_line)
    break_at = None
    for i, ch in enumerate(text):
        pos = i + 1
        if ch in "、。" and lo <= pos <= hi:
            break_at = pos  # 範囲内で最も後ろの句読点を採用(両行の長さバランスが良くなる)
    if break_at is None:
        break_at = hi
    line1 = text[:break_at]
    line2 = text[break_at:]
    return f"{line1}\\N{line2}"


def expand_ranges_for_subtitles(
    ranges: list[tuple[float, float, str]],
    max_total: int = SUBTITLE_MAX_TOTAL_CHARS,
) -> list[tuple[float, float, str]]:
    """句点単位の(開始,終了,文)を、2行に収まらない長文はさらに読点で分割し、
    区間内で文字数比按分してタイミングを割り直す。表示用テキストは\\N折り返し済みにする。"""
    expanded: list[tuple[float, float, str]] = []
    for start, end, sentence in ranges:
        chunks = split_long_sentence_for_subtitle(sentence, max_total)
        if len(chunks) == 1:
            expanded.append((start, end, wrap_subtitle_text(chunks[0])))
            continue
        total_chars = sum(len(c) for c in chunks) or 1
        duration = end - start
        cursor = start
        for i, chunk in enumerate(chunks):
            share = duration * (len(chunk) / total_chars)
            chunk_start = cursor
            chunk_end = cursor + share if i < len(chunks) - 1 else end
            expanded.append((chunk_start, chunk_end, wrap_subtitle_text(chunk)))
            cursor = chunk_end
    return expanded


def get_audio_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def find_japanese_font() -> str:
    candidates = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/HiraginoSans-W6.ttc",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return candidates[0]


def build_page_video(image_path: Path, mp3_path: Path, duration: float, out_path: Path):
    """1枚の画像+音声から、音声長ぴったりのmp4を作る。"""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-loop", "1", "-i", str(image_path),
            "-i", str(mp3_path),
            "-t", f"{duration}",
            "-vf", f"scale={TARGET_W}:{TARGET_H},format=yuv420p",
            "-r", "25",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k",
            str(out_path),
        ],
        check=True,
    )


def concat_videos(clip_paths: list[Path], out_path: Path, work_dir: Path):
    list_file = work_dir / "concat_list.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in clip_paths:
            f.write(f"file '{p.resolve()}'\n")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(out_path),
        ],
        check=True,
    )


def burn_subtitles(in_video: Path, srt_path: Path, out_video: Path):
    font_dir = str(Path(find_japanese_font()).parent)
    # FontSize=52は依頼値だったが、実測でHiragino Sans+libassでは全角22文字が
    # 1920px幅(MarginL/R=60除いた1800px)を大幅にはみ出すことを確認したため、
    # 22文字が確実に収まる28に調整した(実測: test_size_28.png相当で余白あり)。
    style = (
        "FontName=Hiragino Sans,FontSize=28,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,"
        "Alignment=2,MarginV=60,MarginL=60,MarginR=60"
    )
    # original_sizeを明示しないと、libassがSRTのデフォルトPlayRes(384x288等)を
    # 動画解像度(1920x1080)へ拡大する際にFontSizeも一緒に何倍にも拡大されてしまい、
    # 字幕が画面からはみ出す。1080pの実寸としてFontSizeを効かせるために必須。
    vf = (
        f"subtitles={srt_path}:fontsdir={font_dir}"
        f":original_size={TARGET_W}x{TARGET_H}:force_style='{style}'"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(in_video),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out_video),
        ],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description="章PDF+台本からAIナレーション動画を自動生成")
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--voice", default="ja-JP-NanamiNeural")
    parser.add_argument("--rate", default="+0%")
    parser.add_argument("--max-pages", type=int, default=None, help="試作用: 先頭Nページだけ処理する")
    args = parser.parse_args()

    if not args.pdf.exists():
        sys.exit(f"PDFが見つかりません: {args.pdf}")
    if not args.script.exists():
        sys.exit(f"台本が見つかりません: {args.script}")

    args.out.mkdir(parents=True, exist_ok=True)
    work_dir = args.out / "_work"
    work_dir.mkdir(parents=True, exist_ok=True)

    chapter_name = args.pdf.stem
    yomi_dict = load_yomi_dict()

    log(f"台本パース: {args.script}")
    pages = parse_script(args.script, yomi_dict)
    if not pages:
        sys.exit("台本から【ページN】ブロックを1件も検出できませんでした")
    log(f"検出ページ数: {len(pages)}")

    log(f"PDF→画像変換: {args.pdf}")
    images = pdf_to_images(args.pdf, work_dir)
    log(f"画像枚数: {len(images)}")

    total_pdf_pages = len(images)
    for page_no in range(1, total_pdf_pages + 1):
        if page_no not in pages or not pages[page_no].strip():
            fallback_text = extract_pdf_page_text(args.pdf, page_no, yomi_dict)
            pages[page_no] = fallback_text
            log(f"ページ{page_no}: 台本なし→PDF本文で代用")

    if args.max_pages is not None:
        pages = {p: t for p, t in pages.items() if p <= args.max_pages}
        log(f"--max-pages指定によりページ1〜{args.max_pages}のみ処理")

    audio_dir = work_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    clip_dir = work_dir / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)

    all_page_srt_entries = []  # 全体連結後のSRT用(表示は台本の元表記のまま)
    tts_text_by_page = []  # {chapter_name}_tts_text.txt 出力用(TTSに実際に渡した文字列)
    clip_paths = []
    global_offset = 0.0
    total_pages = max(pages.keys())
    per_page_seconds = []

    for page_no in sorted(pages.keys()):
        raw_text = pages[page_no]
        if not raw_text:
            log(f"ページ{page_no}: 本文が空のためスキップ")
            continue
        if page_no - 1 >= len(images):
            log(f"ページ{page_no}: 対応する画像が無いためスキップ")
            continue
        image_path = images[page_no - 1]
        mp3_path = audio_dir / f"page_{page_no:03d}.mp3"

        tts_text = build_tts_text(raw_text, yomi_dict)
        tts_text_by_page.append((page_no, tts_text))

        log(f"ページ{page_no}: 音声合成中... 「{tts_text[:20]}...」")
        word_boundaries = asyncio.run(
            synth_page_audio(tts_text, args.voice, args.rate, mp3_path)
        )
        duration = get_audio_duration(mp3_path)
        per_page_seconds.append(duration)
        log(f"ページ{page_no}: 音声長 {duration:.2f}秒")

        clip_path = clip_dir / f"clip_{page_no:03d}.mp4"
        build_page_video(image_path, mp3_path, duration, clip_path)
        clip_paths.append(clip_path)

        # 字幕は台本の元表記(raw_text)のまま表示する。TTSに渡した変換後テキストは使わない。
        ranges = build_srt_for_page(raw_text, word_boundaries, duration)
        ranges = expand_ranges_for_subtitles(ranges)
        for start, end, sentence in ranges:
            all_page_srt_entries.append((global_offset + start, global_offset + end, sentence))
        global_offset += duration

    if not clip_paths:
        sys.exit("生成できたページクリップがありませんでした")

    concat_out = work_dir / "concat.mp4"
    log("全ページ連結中...")
    concat_videos(clip_paths, concat_out, work_dir)

    srt_path = args.out / f"{chapter_name}.srt"
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (start, end, sentence) in enumerate(all_page_srt_entries, start=1):
            f.write(f"{i}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{sentence}\n\n")
    log(f"SRT出力: {srt_path}")

    tts_text_path = args.out / f"{chapter_name}_tts_text.txt"
    with open(tts_text_path, "w", encoding="utf-8") as f:
        for page_no, tts_text in tts_text_by_page:
            f.write(f"【ページ{page_no}】\n{tts_text}\n\n")
    log(f"TTS変換後テキスト出力: {tts_text_path}")

    final_out = args.out / f"{chapter_name}.mp4"
    log("字幕焼き込み中...")
    burn_subtitles(concat_out, srt_path, final_out)

    total_duration = get_audio_duration(final_out)
    log(f"完成: {final_out}")
    log(f"合計尺: {total_duration:.2f}秒 / ページ数: {len(clip_paths)} / 1ページ平均: {total_duration/len(clip_paths):.2f}秒")


if __name__ == "__main__":
    main()
